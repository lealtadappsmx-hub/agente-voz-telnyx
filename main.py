import json
import base64
import asyncio
import audioop
import secrets
import uuid
from time import monotonic

import httpx
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from google import genai
from google.genai import types

from call_context import CallContextStore
from call_duration import closing_deadlines
from end_call import (
    END_CALL_ACTION,
    EndCallSettings,
    end_call_runtime_instruction,
    end_call_tool_declaration,
    select_end_call_settings,
    validate_end_call_request,
)
from handoff import (
    TRANSFER_CALL_ACTION,
    HandoffSettings,
    select_handoff_settings,
    transfer_call_runtime_instruction,
    transfer_call_tool_declaration,
    validate_transfer_call_request,
)
from gemini_key_selector import select_gemini_api_key
from panel_config_client import (
    CallDurationSettings,
    PanelObservationSettings,
    claim_outbound_call,
    observe_panel_agent,
    report_call_event,
    report_outbound_event,
    select_call_duration_settings,
    select_live_session_settings,
    select_system_prompt,
)
from telnyx_key_selector import select_telnyx_api_key


app = FastAPI()


# Dirección pública del WebSocket en EasyPanel
TELNYX_WS_URL = "wss://vozagent.lealtadapps.com/media"

# Modelo de Gemini Live que ya funciona en este proyecto
GEMINI_MODEL = "gemini-3.1-flash-live-preview"

# Duración máxima de cada llamada:
# 180 segundos equivalen a 3 minutos.
MAX_CALL_SECONDS = 180

# Margen acotado para no interrumpir una frase de cierre ya enviada a Gemini.
# No hay polling: cada espera ocurre una vez por etapa de cierre.
CLOSING_AUDIO_TIMEOUT_SECONDS = 12

# Aquí se guardan los temporizadores activos.
TAREAS_CORTE = {}
TAREAS_TRANSFER = {}

# Contextos efímeros de las llamadas activas. No usan base de datos ni polling.
CALL_CONTEXTS = CallContextStore()

# Serializa únicamente la reserva/creación de llamadas salientes. No es un
# worker: se ejecuta por clic explícito o por el webhook call.hangup.
OUTBOUND_START_LOCK = asyncio.Lock()
# Asociación efímera para el raro caso de que el webhook llegue antes de la
# respuesta HTTP de Dial. Se elimina al asociar el control o si falla el dial.
PENDING_OUTBOUND_CALLS = {}
PENDING_TRANSFER_LEGS = {}
PENDING_TRANSFER_ANNOUNCEMENTS = {}

# Configuración inmutable del panel. Cada llamada realiza una sola resolución.
PANEL_OBSERVATION_SETTINGS = PanelObservationSettings.from_environment()


# ---------------------------------------------------------
# PERSONALIDAD DEL AGENTE
# ---------------------------------------------------------

SYSTEM_PROMPT = """
Eres el asistente virtual de ventas de LealtadApps, operando desde
Los Mochis, Sinaloa.

Habla siempre en español mexicano con un tono natural, amable,
carismático y profesional.

Tus respuestas deben ser breves y apropiadas para una llamada
telefónica.

LealtadApps ofrece:

- Tarjetas de sellos digitales.
- Programas de cashback.
- Sistemas CRM.
- Software personalizado para negocios.

Tu objetivo es conocer brevemente el negocio del prospecto,
explicarle cómo LealtadApps puede ayudarle a vender más y retener
clientes, y conseguir una cita o sus datos de contacto.

Haz solamente una pregunta a la vez.

Evita listas largas y monólogos.

Permite que la persona te interrumpa.

Preséntate claramente como asistente virtual de LealtadApps.

Si preguntan por un detalle técnico que no conoces, ofrece registrar
sus datos para que un desarrollador del equipo se comunique con ellos.
"""


def _telnyx_api_key_for_call(call_control_id: str) -> str:
    """Obtiene únicamente la clave efímera del negocio resuelto."""
    context = CALL_CONTEXTS.get(call_control_id=call_control_id)
    if context is None or not context.telnyx_api_key:
        raise RuntimeError("La llamada no tiene credencial Telnyx preparada.")
    return context.telnyx_api_key


async def registrar_evento_llamada(
    context,
    event_type: str,
    *,
    hangup_cause: str | None = None,
    termination_source: str | None = None,
    termination_reason: str | None = None,
) -> None:
    """Registra un hecho puntual sólo si el panel ya resolvió el agente real."""
    observation = context.agent_config if context else None
    if observation is None:
        return
    await report_call_event(
        agent_id=observation.agent_id,
        external_call_id=context.call_control_id,
        call_session_id=context.call_session_id,
        direction=context.direction,
        event_type=event_type,
        from_number=context.from_number,
        to_number=context.to_number,
        hangup_cause=hangup_cause,
        termination_source=termination_source,
        termination_reason=termination_reason,
        settings=PANEL_OBSERVATION_SETTINGS,
    )


# ---------------------------------------------------------
# CONTROL DE DURACIÓN DE LAS LLAMADAS
# ---------------------------------------------------------

async def colgar_llamada_telnyx(
    call_control_id: str,
):
    """
    Envía a Telnyx la orden para terminar
    una llamada activa.
    """

    try:
        telnyx_api_key = _telnyx_api_key_for_call(call_control_id)
    except RuntimeError:
        print("ERROR al intentar colgar la llamada: credencial Telnyx no disponible.")
        return

    url = (
        "https://api.telnyx.com/v2/calls/"
        f"{call_control_id}/actions/hangup"
    )

    headers = {
        "Authorization": (
            f"Bearer {telnyx_api_key}"
        ),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(
            timeout=20.0
        ) as client:

            response = await client.post(
                url,
                headers=headers,
                json={},
            )

        print(
            "Respuesta de Telnyx al colgar: "
            f"HTTP {response.status_code}."
        )

        response.raise_for_status()

    except Exception as error:
        print(
            "ERROR al intentar colgar la llamada: "
            f"{type(error).__name__}"
        )


def _seconds_until(deadline: float) -> float:
    """Calcula una espera única; nunca devuelve un valor negativo."""
    return max(0.0, deadline - monotonic())


async def _wait_for_closing_turn(
    call_control_id: str,
    timeout_seconds: float,
) -> bool:
    """Espera una sola vez el fin del turno de Gemini de una fase de cierre."""
    context = CALL_CONTEXTS.get(call_control_id=call_control_id)
    if context is None:
        return False
    try:
        await asyncio.wait_for(
            context.closure_turn_finished.wait(),
            timeout=max(0.1, timeout_seconds),
        )
        return True
    except asyncio.TimeoutError:
        return False


async def _send_closing_message(
    call_control_id: str,
    phase: str,
    message: str | None,
) -> bool:
    """Encola una frase validada para Gemini sin registrarla en los logs."""
    if not message:
        return False
    queued = CALL_CONTEXTS.request_closure_message(
        call_control_id,
        phase,
        message,
    )
    if queued:
        print(f"Cierre de llamada solicitado: phase={phase}.")
    return queued


async def cortar_llamada_por_tiempo(
    call_control_id: str,
    duration_settings: CallDurationSettings,
    answered_at: float,
):
    """Aplica el cierre configurado, con corte fijo seguro como respaldo."""

    maximum = duration_settings.max_call_seconds
    farewell_before_end = duration_settings.farewell_seconds_before_end
    warning_start, hard_limit = closing_deadlines(
        answered_at=answered_at,
        max_call_seconds=maximum,
        farewell_seconds_before_end=farewell_before_end,
        has_time_warning=bool(duration_settings.time_warning_message),
    )

    try:
        print(
            "Temporizador de llamada preparado: "
            f"max_seconds={maximum} farewell_before_end={farewell_before_end}."
        )

        # El aviso, si existe, se pronuncia antes del límite. Nunca se usa
        # ese margen para adelantar la despedida final: así "90 segundos"
        # realmente significa que el cierre final comienza en el segundo 90.
        if warning_start is not None:
            await asyncio.sleep(_seconds_until(warning_start))
            warning_queued = await _send_closing_message(
                call_control_id,
                "time_warning",
                duration_settings.time_warning_message,
            )
            if warning_queued:
                warning_window = min(
                    CLOSING_AUDIO_TIMEOUT_SECONDS,
                    _seconds_until(hard_limit),
                )
                warning_finished = await _wait_for_closing_turn(
                    call_control_id,
                    warning_window,
                )
                if not warning_finished:
                    print("Cierre de llamada sin confirmación de audio: phase=time_warning.")

        await asyncio.sleep(_seconds_until(hard_limit))

        farewell_queued = await _send_closing_message(
            call_control_id,
            "final_farewell",
            duration_settings.final_farewell,
        )

        if farewell_queued:
            farewell_finished = await _wait_for_closing_turn(
                call_control_id,
                CLOSING_AUDIO_TIMEOUT_SECONDS,
            )
            if not farewell_finished:
                print("Cierre de llamada sin confirmación de audio: phase=final_farewell.")
            else:
                print("Audio de despedida confirmado: phase=final_farewell.")

        print(
            "La llamada alcanzó su límite configurado. "
            "Solicitando corte físico a Telnyx."
        )
        await colgar_llamada_telnyx(call_control_id)
        CALL_CONTEXTS.finish(call_control_id, "max_duration")

    except asyncio.CancelledError:
        CALL_CONTEXTS.set_timer_state(
            call_control_id,
            "cancelled",
        )
        print(
            "Temporizador cancelado porque "
            "la llamada terminó antes del límite."
        )

    finally:
        tarea_actual = asyncio.current_task()

        if (
            TAREAS_CORTE.get(call_control_id)
            is tarea_actual
        ):
            TAREAS_CORTE.pop(
                call_control_id,
                None,
            )


async def cortar_llamada_por_end_call(
    call_control_id: str,
    reason: str,
):
    """Espera una vez el audio final autorizado y ordena el hangup físico."""
    try:
        finished = await _wait_for_closing_turn(
            call_control_id,
            CLOSING_AUDIO_TIMEOUT_SECONDS,
        )
        if finished:
            print(f"Audio de cierre preparado finalizó: reason={reason}.")
        else:
            print(f"Cierre de llamada sin confirmación de audio: reason={reason}.")

        print(f"END_CALL aceptado. Solicitando corte físico a Telnyx: reason={reason}.")
        await colgar_llamada_telnyx(call_control_id)
        CALL_CONTEXTS.finish(call_control_id, reason)
    except asyncio.CancelledError:
        print("Cierre END_CALL cancelado porque la llamada terminó antes.")
    finally:
        tarea_actual = asyncio.current_task()
        if TAREAS_CORTE.get(call_control_id) is tarea_actual:
            TAREAS_CORTE.pop(call_control_id, None)


def iniciar_cierre_por_end_call(
    call_control_id: str,
    reason: str,
    settings: EndCallSettings,
) -> bool:
    """Programa un único cierre físico para el motivo autorizado de esta llamada."""
    context = CALL_CONTEXTS.get(call_control_id=call_control_id)
    if context is None or context.transfer_state != "idle":
        return False
    message = settings.message_for_reason(reason)
    if not message:
        return False

    queued = CALL_CONTEXTS.request_closure_message(
        call_control_id,
        "end_call",
        message,
    )
    if not queued:
        return False

    cancelar_temporizador_llamada(call_control_id)
    TAREAS_CORTE[call_control_id] = asyncio.create_task(
        cortar_llamada_por_end_call(call_control_id, reason)
    )
    print(f"END_CALL preparado: reason={reason}.")
    return True


async def _informar_fallo_transferencia(
    call_control_id: str,
    settings: HandoffSettings,
) -> None:
    """Mantiene o cierra la llamada original según la política ya configurada."""
    context = CALL_CONTEXTS.get(call_control_id=call_control_id)
    if context is None:
        return
    # La transferencia ya no está en curso: Gemini puede comunicar una sola
    # alternativa configurada si la llamada sigue activa.
    CALL_CONTEXTS.set_transfer_state(call_control_id, "idle")
    phase = "transfer_failed_farewell" if settings.failure_mode == "farewell" else "transfer_failed"
    queued = CALL_CONTEXTS.request_closure_message(call_control_id, phase, settings.failure_message or "")
    if not queued:
        CALL_CONTEXTS.set_transfer_state(call_control_id, "idle")
        return
    finished = await _wait_for_closing_turn(call_control_id, CLOSING_AUDIO_TIMEOUT_SECONDS)
    if settings.failure_mode == "farewell":
        if finished:
            await colgar_llamada_telnyx(call_control_id)
        CALL_CONTEXTS.finish(call_control_id, "transfer_unavailable")
    else:
        CALL_CONTEXTS.set_transfer_state(call_control_id, "idle")
    print("Transferencia humana no disponible; se aplicó la política configurada.")


async def _solicitar_transferencia_humana(
    call_control_id: str,
    settings: HandoffSettings,
) -> None:
    """Manda un único comando Telnyx después del aviso confirmado por Telnyx."""
    try:
        context = CALL_CONTEXTS.get(call_control_id=call_control_id)
        if context is None or context.transfer_state != "announcing":
            return
        telnyx_api_key = _telnyx_api_key_for_call(call_control_id)
        target_state = base64.b64encode(secrets.token_bytes(24)).decode("ascii")
        PENDING_TRANSFER_LEGS[target_state] = (call_control_id, settings)
        caller_id = context.from_number if context.direction == "outbound" else context.to_number
        payload = {
            "to": settings.destination,
            "from": caller_id,
            "timeout_secs": settings.timeout_seconds,
            "target_leg_client_state": target_state,
            "command_id": str(uuid.uuid4()),
        }
        async with httpx.AsyncClient(timeout=20.0, limits=httpx.Limits(max_connections=1, max_keepalive_connections=0)) as client:
            response = await client.post(
                f"https://api.telnyx.com/v2/calls/{call_control_id}/actions/transfer",
                headers={"Authorization": f"Bearer {telnyx_api_key}", "Content-Type": "application/json", "Accept": "application/json"},
                json=payload,
            )
        response.raise_for_status()
        CALL_CONTEXTS.set_transfer_state(call_control_id, "dialing")
        await registrar_evento_llamada(context, "transfer_dialing")
        print("Transferencia humana solicitada a Telnyx.")
    except Exception as error:
        if "target_state" in locals():
            PENDING_TRANSFER_LEGS.pop(target_state, None)
        print(f"ERROR solicitando transferencia humana: {type(error).__name__}")
        await _informar_fallo_transferencia(call_control_id, settings)
    finally:
        if TAREAS_TRANSFER.get(call_control_id) is asyncio.current_task():
            TAREAS_TRANSFER.pop(call_control_id, None)


async def _anunciar_transferencia_humana(
    call_control_id: str,
    settings: HandoffSettings,
) -> None:
    """Pide a Telnyx un único aviso y espera su webhook call.speak.ended."""
    announcement_state = base64.b64encode(secrets.token_bytes(24)).decode("ascii")
    try:
        context = CALL_CONTEXTS.get(call_control_id=call_control_id)
        if context is None or context.transfer_state != "announcing":
            return
        PENDING_TRANSFER_ANNOUNCEMENTS[announcement_state] = (call_control_id, settings)
        telnyx_api_key = _telnyx_api_key_for_call(call_control_id)
        payload = {
            "payload": settings.announcement,
            "voice": "AWS.Polly.Mia-Neural",
            "language": "es-MX",
            "client_state": announcement_state,
            "command_id": str(uuid.uuid4()),
        }
        async with httpx.AsyncClient(timeout=20.0, limits=httpx.Limits(max_connections=1, max_keepalive_connections=0)) as client:
            response = await client.post(
                f"https://api.telnyx.com/v2/calls/{call_control_id}/actions/speak",
                headers={"Authorization": f"Bearer {telnyx_api_key}", "Content-Type": "application/json", "Accept": "application/json"},
                json=payload,
            )
        response.raise_for_status()
        print("Aviso de transferencia solicitado a Telnyx una sola vez.")
    except Exception as error:
        PENDING_TRANSFER_ANNOUNCEMENTS.pop(announcement_state, None)
        print(f"ERROR preparando aviso de transferencia: {type(error).__name__}")
        await _informar_fallo_transferencia(call_control_id, settings)
    finally:
        if TAREAS_TRANSFER.get(call_control_id) is asyncio.current_task():
            TAREAS_TRANSFER.pop(call_control_id, None)


async def _detener_ia_despues_de_transferencia(call_control_id: str) -> None:
    """Desconecta el WebSocket de IA, no la llamada puenteada entre las personas."""
    try:
        telnyx_api_key = _telnyx_api_key_for_call(call_control_id)
        async with httpx.AsyncClient(timeout=20.0, limits=httpx.Limits(max_connections=1, max_keepalive_connections=0)) as client:
            response = await client.post(
                f"https://api.telnyx.com/v2/calls/{call_control_id}/actions/streaming_stop",
                headers={"Authorization": f"Bearer {telnyx_api_key}", "Content-Type": "application/json", "Accept": "application/json"},
                json={"command_id": str(uuid.uuid4())},
            )
        response.raise_for_status()
        print("IA desconectada tras transferencia humana; la llamada puenteada continúa.")
    except Exception as error:
        print(f"ERROR deteniendo IA tras transferencia: {type(error).__name__}")


def iniciar_transferencia_humana(call_control_id: str, settings: HandoffSettings) -> bool:
    """Acepta una sola transferencia por llamada y nunca toma destinos desde Gemini."""
    if not settings.enabled or not CALL_CONTEXTS.begin_transfer(call_control_id):
        return False
    TAREAS_TRANSFER[call_control_id] = asyncio.create_task(
        _anunciar_transferencia_humana(call_control_id, settings)
    )
    context = CALL_CONTEXTS.get(call_control_id=call_control_id)
    if context:
        asyncio.create_task(registrar_evento_llamada(context, "transfer_requested"))
    print("Transferencia humana preparada.")
    return True


def cancelar_transferencia(call_control_id: str) -> None:
    task = TAREAS_TRANSFER.pop(call_control_id, None)
    if task and not task.done():
        task.cancel()
    for state, pending in tuple(PENDING_TRANSFER_ANNOUNCEMENTS.items()):
        if pending[0] == call_control_id:
            PENDING_TRANSFER_ANNOUNCEMENTS.pop(state, None)
    for state, pending in tuple(PENDING_TRANSFER_LEGS.items()):
        if pending[0] == call_control_id:
            PENDING_TRANSFER_LEGS.pop(state, None)


def cancelar_temporizador_llamada(
    call_control_id: str,
):
    """
    Cancela y elimina el temporizador
    correspondiente a una llamada.
    """

    if not call_control_id:
        return

    tarea = TAREAS_CORTE.pop(
        call_control_id,
        None,
    )

    if tarea and not tarea.done():
        tarea.cancel()


def iniciar_temporizador_llamada(
    call_control_id: str,
    duration_settings: CallDurationSettings,
):
    """Mantiene un solo temporizador por llamada y conserva el tiempo ya transcurrido."""
    context = CALL_CONTEXTS.get(call_control_id=call_control_id)
    if context is None or context.answered_at_monotonic is None:
        return False

    cancelar_temporizador_llamada(call_control_id)
    TAREAS_CORTE[call_control_id] = asyncio.create_task(
        cortar_llamada_por_tiempo(
            call_control_id,
            duration_settings,
            context.answered_at_monotonic,
        )
    )
    CALL_CONTEXTS.set_timer_state(call_control_id, "active")
    return True


async def observar_y_guardar_contexto(
    call_control_id: str,
    called_number: str,
    webhook_token: str | None = None,
):
    """Resuelve una vez el agente y adjunta sus credenciales efímeras."""
    observation = await observe_panel_agent(
        called_number,
        settings=PANEL_OBSERVATION_SETTINGS,
        webhook_token=webhook_token,
    )
    if observation is None:
        CALL_CONTEXTS.mark_configuration_ready(call_control_id, False)
        return None

    try:
        telnyx_api_key = select_telnyx_api_key(
            observation,
            shared_secret=PANEL_OBSERVATION_SETTINGS.shared_secret,
        )
    except RuntimeError:
        CALL_CONTEXTS.mark_configuration_ready(call_control_id, False)
        return None

    attached = CALL_CONTEXTS.set_agent_config(call_control_id, observation)
    key_attached = CALL_CONTEXTS.set_telnyx_api_key(call_control_id, telnyx_api_key)
    if not attached or not key_attached:
        CALL_CONTEXTS.mark_configuration_ready(call_control_id, False)
        return None

    CALL_CONTEXTS.mark_configuration_ready(call_control_id, True)
    context = CALL_CONTEXTS.get(call_control_id=call_control_id)
    await registrar_evento_llamada(context, "initiated")
    print("Configuración dinámica y credencial Telnyx adjuntadas al contexto.")
    return observation


async def recibir_inicio_telnyx(websocket: WebSocket):
    """Espera el evento inicial para asociar el WebSocket con su llamada."""
    while True:
        texto = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
        mensaje = json.loads(texto)
        evento = mensaje.get("event")
        if evento == "connected":
            print("Telnyx conectó el WebSocket.")
            continue
        if evento != "start":
            continue
        start_data = mensaje.get("start", {})
        context = CALL_CONTEXTS.link_session(
            call_control_id=start_data.get("call_control_id"),
            call_session_id=start_data.get("call_session_id"),
        )
        if context is None:
            raise RuntimeError("No se encontró el contexto de la llamada.")
        print("Telnyx inició el stream de audio.")
        return context


# ---------------------------------------------------------
# CONTESTAR LA LLAMADA Y ABRIR EL STREAM DE AUDIO
# ---------------------------------------------------------

async def contestar_y_abrir_audio(
    call_control_id: str,
):
    """
    Contesta la llamada y ordena a Telnyx
    abrir un WebSocket bidireccional.
    """

    telnyx_api_key = _telnyx_api_key_for_call(call_control_id)

    url = (
        "https://api.telnyx.com/v2/calls/"
        f"{call_control_id}/actions/answer"
    )

    headers = {
        "Authorization": (
            f"Bearer {telnyx_api_key}"
        ),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload = {
        "stream_url": TELNYX_WS_URL,
        "stream_track": "inbound_track",
        "stream_codec": "PCMU",
        "stream_bidirectional_mode": "rtp",
        "stream_bidirectional_codec": "PCMU",
        "send_silence_when_idle": True,
    }

    try:
        async with httpx.AsyncClient(
            timeout=20.0
        ) as client:

            response = await client.post(
                url,
                headers=headers,
                json=payload,
            )

        print(
            "Respuesta de Telnyx al contestar: "
            f"HTTP {response.status_code}."
        )

        response.raise_for_status()

        if CALL_CONTEXTS.mark_answered(call_control_id):
            context = CALL_CONTEXTS.get(call_control_id=call_control_id)
            duration_settings = select_call_duration_settings(
                context.agent_config if context else None
            )
            iniciar_temporizador_llamada(
                call_control_id,
                duration_settings,
            )
            print(
                "Configuración de duración preparada: "
                f"max_seconds={duration_settings.max_call_seconds} "
                f"warning={'enabled' if duration_settings.time_warning_message else 'disabled'} "
                f"farewell={'enabled' if duration_settings.final_farewell else 'disabled'}."
            )

    except Exception as error:
        CALL_CONTEXTS.finish(
            call_control_id,
            "answer_failed",
        )
        print(
            "ERROR contestando la llamada en Telnyx: "
            f"{type(error).__name__}"
        )


async def preparar_y_contestar_llamada(
    call_control_id: str,
    called_number: str,
    webhook_token: str | None = None,
):
    """Resuelve el negocio antes de contestar; no usa credenciales globales."""
    observation = await observar_y_guardar_contexto(
        call_control_id,
        called_number,
        webhook_token,
    )
    if observation is None:
        CALL_CONTEXTS.finish(call_control_id, "configuration_unavailable")
        print("Llamada no contestada: configuración del negocio no disponible.")
        return
    await contestar_y_abrir_audio(call_control_id)


async def _start_one_outbound_call(campaign_id: int) -> bool:
    """Reclama y marca una llamada. Toda continuación ocurre por eventos Telnyx."""
    async with OUTBOUND_START_LOCK:
        claim = await claim_outbound_call(campaign_id, settings=PANEL_OBSERVATION_SETTINGS)
        if claim is None:
            return False
        try:
            telnyx_api_key = select_telnyx_api_key(
                claim.observation, shared_secret=PANEL_OBSERVATION_SETTINGS.shared_secret,
            )
            # Validamos Gemini antes de marcar para no crear una llamada muda.
            select_gemini_api_key(claim.observation, shared_secret=PANEL_OBSERVATION_SETTINGS.shared_secret)
            headers = {"Authorization": f"Bearer {telnyx_api_key}", "Content-Type": "application/json", "Accept": "application/json"}
            client_state = base64.b64encode(secrets.token_bytes(24)).decode("ascii")
            PENDING_OUTBOUND_CALLS[client_state] = (claim, telnyx_api_key)
            payload = {
                "connection_id": claim.connection_id,
                "from": claim.from_number,
                "to": claim.to_number,
                "stream_url": TELNYX_WS_URL,
                "stream_track": "inbound_track",
                "stream_codec": "PCMU",
                "stream_bidirectional_mode": "rtp",
                "stream_bidirectional_codec": "PCMU",
                "send_silence_when_idle": True,
                # Telnyx también aplica un límite físico, además del cierre del puente.
                "timeout_secs": 30,
                "time_limit_secs": claim.observation.max_call_seconds,
                "client_state": client_state,
            }
            async with httpx.AsyncClient(timeout=20.0, limits=httpx.Limits(max_connections=1, max_keepalive_connections=0)) as client:
                response = await client.post("https://api.telnyx.com/v2/calls", headers=headers, json=payload)
            response.raise_for_status()
            call_control_id = response.json().get("data", {}).get("call_control_id")
            if not isinstance(call_control_id, str) or not call_control_id.strip():
                raise RuntimeError("Telnyx no devolvió el control de la llamada")
            PENDING_OUTBOUND_CALLS.pop(client_state, None)
            if CALL_CONTEXTS.get(call_control_id=call_control_id) is None:
                CALL_CONTEXTS.register(
                    call_control_id=call_control_id, call_session_id=None,
                    from_number=claim.from_number, to_number=claim.to_number, direction="outbound",
                    outbound_campaign_id=claim.campaign_id, outbound_recipient_id=claim.recipient_id,
                )
                CALL_CONTEXTS.set_agent_config(call_control_id, claim.observation)
                CALL_CONTEXTS.set_telnyx_api_key(call_control_id, telnyx_api_key)
                CALL_CONTEXTS.mark_configuration_ready(call_control_id, True)
            await registrar_evento_llamada(CALL_CONTEXTS.get(call_control_id=call_control_id), "initiated")
            print("Llamada saliente solicitada a Telnyx: campaign=active.")
            return True
        except Exception as error:
            if "client_state" in locals():
                PENDING_OUTBOUND_CALLS.pop(client_state, None)
            await report_outbound_event(
                campaign_id=claim.campaign_id, recipient_id=claim.recipient_id, result="failed",
                settings=PANEL_OBSERVATION_SETTINGS,
            )
            print(f"ERROR iniciando llamada saliente: {type(error).__name__}")
            return False


async def start_outbound_campaign(campaign_id: int) -> int:
    """Llena hasta tres lugares permitidos mediante reclamaciones transaccionales puntuales."""
    started = 0
    for _ in range(3):
        if not await _start_one_outbound_call(campaign_id):
            break
        started += 1
    return started


@app.post("/internal/v1/outbound/start")
async def start_outbound(request: Request):
    """Entrada privada invocada por el botón del panel, nunca por el navegador."""
    shared_secret = PANEL_OBSERVATION_SETTINGS.shared_secret
    supplied_secret = request.headers.get("X-Voice-Service-Key", "")
    if not shared_secret or not secrets.compare_digest(supplied_secret, shared_secret):
        return JSONResponse({"status": "error"}, status_code=401)
    try:
        payload = await request.json()
        campaign_id = payload.get("campaign_id") if isinstance(payload, dict) else None
        if type(campaign_id) is not int or campaign_id < 1:
            raise ValueError("invalid campaign")
    except (ValueError, TypeError, json.JSONDecodeError):
        return JSONResponse({"status": "error"}, status_code=400)
    started = await start_outbound_campaign(campaign_id)
    return JSONResponse({"status": "ok", "started": started})


# ---------------------------------------------------------
# WEBHOOK DE TELNYX
# ---------------------------------------------------------

@app.post("/webhooks/telnyx")
@app.post("/webhooks/telnyx/{webhook_token}")
async def telnyx_webhook(
    request: Request,
    webhook_token: str | None = None,
):
    """
    Recibe los eventos enviados por Telnyx.
    """

    try:
        payload = await request.json()

        data = payload.get(
            "data",
            {},
        )

        event_type = data.get(
            "event_type"
        )

        event_payload = data.get(
            "payload",
            {},
        )

        print(
            "Evento recibido de Telnyx: "
            f"{event_type}"
        )

        # Nueva llamada entrante.
        if event_type == "call.initiated":
            call_control_id = (
                event_payload.get(
                    "call_control_id"
                )
            )

            if not call_control_id:
                raise ValueError(
                    "Telnyx no envió "
                    "call_control_id."
                )

            called_number = event_payload.get("to", "")

            existing_context = CALL_CONTEXTS.get(call_control_id=call_control_id)
            if existing_context and existing_context.direction == "outbound":
                CALL_CONTEXTS.link_session(
                    call_control_id=call_control_id,
                    call_session_id=event_payload.get("call_session_id"),
                )
                print("Llamada saliente iniciada por Telnyx.")
                return JSONResponse(content={"status": "ok"}, status_code=200)

            # La nueva pierna hacia el asesor no es una llamada entrante: se
            # identifica con estado efímero y no abre Gemini ni otro contexto.
            if event_payload.get("client_state") in PENDING_TRANSFER_LEGS:
                print("Pierna de transferencia humana iniciada por Telnyx.")
                return JSONResponse(content={"status": "ok"}, status_code=200)

            pending_outbound = PENDING_OUTBOUND_CALLS.pop(event_payload.get("client_state"), None)
            if pending_outbound:
                claim, telnyx_api_key = pending_outbound
                CALL_CONTEXTS.register(
                    call_control_id=call_control_id,
                    call_session_id=event_payload.get("call_session_id"),
                    from_number=event_payload.get("from"),
                    to_number=called_number,
                    direction="outbound",
                    outbound_campaign_id=claim.campaign_id,
                    outbound_recipient_id=claim.recipient_id,
                )
                CALL_CONTEXTS.set_agent_config(call_control_id, claim.observation)
                CALL_CONTEXTS.set_telnyx_api_key(call_control_id, telnyx_api_key)
                CALL_CONTEXTS.mark_configuration_ready(call_control_id, True)
                print("Llamada saliente iniciada por Telnyx.")
                return JSONResponse(content={"status": "ok"}, status_code=200)

            CALL_CONTEXTS.register(
                call_control_id=call_control_id,
                call_session_id=event_payload.get("call_session_id"),
                from_number=event_payload.get("from"),
                to_number=called_number,
            )

            print(
                "Contexto de llamada creado. "
                f"Contextos activos: {CALL_CONTEXTS.active_count}."
            )

            print("Llamada entrante detectada.")

            asyncio.create_task(
                preparar_y_contestar_llamada(
                    call_control_id,
                    called_number,
                    webhook_token,
                )
            )

        elif event_type == "call.bridged":
            call_control_id = event_payload.get("call_control_id")
            context = CALL_CONTEXTS.get(call_control_id=call_control_id)
            if context and context.transfer_state == "dialing":
                CALL_CONTEXTS.set_transfer_state(call_control_id, "bridged")
                CALL_CONTEXTS.mark_runtime_ready(call_control_id, False)
                cancelar_temporizador_llamada(call_control_id)
                for token, pending in tuple(PENDING_TRANSFER_LEGS.items()):
                    if pending[0] == call_control_id:
                        PENDING_TRANSFER_LEGS.pop(token, None)
                asyncio.create_task(_detener_ia_despues_de_transferencia(call_control_id))
                await registrar_evento_llamada(context, "transfer_bridged")
                print("Transferencia humana conectada.")

        elif event_type == "call.speak.ended":
            pending_announcement = PENDING_TRANSFER_ANNOUNCEMENTS.pop(
                event_payload.get("client_state"), None
            )
            if pending_announcement:
                original_call_control_id, handoff_settings = pending_announcement
                context = CALL_CONTEXTS.get(call_control_id=original_call_control_id)
                if context and context.transfer_state == "announcing":
                    TAREAS_TRANSFER[original_call_control_id] = asyncio.create_task(
                        _solicitar_transferencia_humana(original_call_control_id, handoff_settings)
                    )
                    print("Aviso de transferencia finalizado; solicitando enlace humano.")

        elif event_type == "call.answered":
            call_control_id = event_payload.get("call_control_id")
            context = CALL_CONTEXTS.get(call_control_id=call_control_id)
            if context and CALL_CONTEXTS.mark_answered(call_control_id):
                iniciar_temporizador_llamada(
                    call_control_id,
                    select_call_duration_settings(context.agent_config),
                )
                await registrar_evento_llamada(context, "answered")
                print("Llamada contestada.")

        # La llamada terminó antes de los
        # cinco minutos o fue cortada por Telnyx.
        elif event_type == "call.hangup":
            call_control_id = (
                event_payload.get(
                    "call_control_id"
                )
            )

            pending_transfer = PENDING_TRANSFER_LEGS.pop(event_payload.get("client_state"), None)
            if pending_transfer:
                original_call_control_id, handoff_settings = pending_transfer
                original_context = CALL_CONTEXTS.get(call_control_id=original_call_control_id)
                if original_context and original_context.transfer_state == "dialing":
                    await _informar_fallo_transferencia(original_call_control_id, handoff_settings)
                return JSONResponse(content={"status": "ok"}, status_code=200)

            cancelar_temporizador_llamada(
                call_control_id
            )
            cancelar_transferencia(call_control_id)

            finished_context = CALL_CONTEXTS.finish(
                call_control_id,
                event_payload.get("hangup_cause"),
            )

            if finished_context:
                await registrar_evento_llamada(
                    finished_context,
                    "hangup",
                    hangup_cause=event_payload.get("hangup_cause"),
                    termination_source="provider",
                    termination_reason=finished_context.hangup_reason,
                )

            if (
                finished_context
                and finished_context.direction == "outbound"
                and finished_context.outbound_campaign_id
                and finished_context.outbound_recipient_id
            ):
                outcome = "completed" if finished_context.answered_at_monotonic is not None else "failed"
                await report_outbound_event(
                    campaign_id=finished_context.outbound_campaign_id,
                    recipient_id=finished_context.outbound_recipient_id,
                    result=outcome,
                    settings=PANEL_OBSERVATION_SETTINGS,
                )
                # Cada hangup llena sólo un lugar liberado, sin bucles de consulta.
                asyncio.create_task(start_outbound_campaign(finished_context.outbound_campaign_id))

            print(
                "Llamada terminada. Temporizador y contexto eliminados. "
                f"Contextos activos: {CALL_CONTEXTS.active_count}."
            )

        return JSONResponse(
            content={
                "status": "ok"
            },
            status_code=200,
        )

    except Exception as error:
        print(
            "ERROR procesando el webhook de Telnyx: "
            f"{type(error).__name__}"
        )

        return JSONResponse(
            content={
                "status": "error",
                "detail": str(error),
            },
            status_code=500,
        )


# ---------------------------------------------------------
# ENVIAR AUDIO DE TELNYX HACIA GEMINI
# ---------------------------------------------------------

async def enviar_audio_telnyx_a_gemini(
    websocket: WebSocket,
    session,
    call_control_id: str,
):
    """
    Recibe audio PCMU de Telnyx,
    lo convierte a PCM y lo envía
    a Gemini Live.
    """

    paquetes = 0

    while True:
        texto = (
            await websocket.receive_text()
        )

        mensaje = json.loads(
            texto
        )

        evento = mensaje.get(
            "event"
        )

        if evento == "connected":
            print(
                "Telnyx conectó el WebSocket."
            )

        elif evento == "start":
            start_data = mensaje.get("start", {})
            CALL_CONTEXTS.link_session(
                call_control_id=start_data.get("call_control_id"),
                call_session_id=start_data.get("call_session_id"),
            )
            print(
                "Telnyx inició el stream "
                "de audio."
            )

        elif evento == "media":
            # Durante el aviso o la despedida el audio de la persona no abre
            # otro turno: se prioriza terminar la frase de cierre sin cortes.
            context = CALL_CONTEXTS.get(call_control_id=call_control_id)
            if CALL_CONTEXTS.is_closing(call_control_id) or (
                context is not None and context.transfer_state != "idle"
            ):
                continue

            audio_base64 = (
                mensaje.get(
                    "media",
                    {},
                ).get(
                    "payload"
                )
            )

            if not audio_base64:
                continue

            # Audio PCMU recibido desde Telnyx.
            audio_pcmu = (
                base64.b64decode(
                    audio_base64
                )
            )

            # Convertir PCMU a PCM de 16 bits.
            audio_pcm_8khz = (
                audioop.ulaw2lin(
                    audio_pcmu,
                    2,
                )
            )

            # Enviar audio a Gemini Live.
            await session.send_realtime_input(
                audio=types.Blob(
                    data=audio_pcm_8khz,
                    mime_type=(
                        "audio/pcm;rate=8000"
                    ),
                )
            )

            paquetes += 1

            if (
                paquetes == 1
                or paquetes % 100 == 0
            ):
                print(
                    "Audio enviado a Gemini: "
                    f"{paquetes} paquetes"
                )

        elif evento == "stop":
            print(
                "Telnyx detuvo el stream "
                "de audio."
            )

            try:
                await (
                    session.send_realtime_input(
                        audio_stream_end=True
                    )
                )

            except Exception:
                pass

            return

        elif evento == "error":
            print("ERROR enviado por Telnyx durante el stream.")

        elif evento == "dtmf":
            tecla = (
                mensaje.get(
                    "dtmf",
                    {},
                ).get(
                    "digit"
                )
            )

            print(
                f"Tecla recibida: {tecla}"
            )


# ---------------------------------------------------------
# ENVIAR AUDIO DE GEMINI HACIA TELNYX
# ---------------------------------------------------------

async def enviar_instrucciones_de_cierre_a_gemini(
    session,
    call_control_id: str,
):
    """Entrega a Gemini sólo mensajes de cierre autorizados por el panel."""
    context = CALL_CONTEXTS.get(call_control_id=call_control_id)
    if context is None:
        return

    while True:
        phase, message = await context.closure_queue.get()
        if phase == "transfer_announcement":
            instruction = (
                "Comunica ahora exactamente el mensaje autorizado para transferir, sin agregar "
                "preguntas, explicaciones ni datos nuevos; después termina tu turno. "
                f"Mensaje autorizado: {message}"
            )
        elif phase == "transfer_failed":
            instruction = (
                "La transferencia no fue posible. Comunica exactamente el mensaje autorizado, "
                "sin mencionar datos técnicos, y continúa atendiendo normalmente. "
                f"Mensaje autorizado: {message}"
            )
        else:
            instruction = (
                "Cierra la llamada ahora. Pronuncia exactamente el mensaje autorizado a continuación, "
                "sin agregar explicaciones, preguntas ni datos nuevos; después termina tu turno. "
                f"Mensaje autorizado: {message}"
            )
        await session.send_realtime_input(
            text=instruction
        )
        print(f"Instrucción controlada enviada a Gemini: phase={phase}.")


async def procesar_end_call_de_gemini(
    session,
    call_control_id: str,
    settings: EndCallSettings,
    handoff_settings: HandoffSettings,
    tool_call,
):
    """Valida sólo las acciones físicas cerradas y confirma el resultado a Gemini."""
    context = CALL_CONTEXTS.get(call_control_id=call_control_id)
    if context is None or context.transfer_state != "idle":
        return
    function_responses = []
    for function_call in tool_call.function_calls or []:
        reason = validate_end_call_request(
            function_call.name,
            function_call.args,
            settings,
        )
        if reason:
            accepted = iniciar_cierre_por_end_call(call_control_id, reason, settings)
            result = {"action": END_CALL_ACTION, "status": "accepted" if accepted else "rejected"}
        elif validate_transfer_call_request(function_call.name, function_call.args, handoff_settings):
            accepted = iniciar_transferencia_humana(call_control_id, handoff_settings)
            result = {"action": TRANSFER_CALL_ACTION, "status": "accepted" if accepted else "rejected"}
        else:
            result = {"action": "CONTROLLED_ACTION", "status": "rejected"}
        function_responses.append(
            types.FunctionResponse(
                id=function_call.id,
                name=function_call.name,
                response=result,
            )
        )

    if function_responses:
        await session.send_tool_response(function_responses=function_responses)


async def enviar_audio_gemini_a_telnyx(
    websocket: WebSocket,
    session,
    call_control_id: str,
    end_call_settings: EndCallSettings,
    handoff_settings: HandoffSettings,
):
    """
    Recibe continuamente el audio PCM
    de Gemini, lo convierte a PCMU y
    lo reproduce en Telnyx.
    """

    estado_resampleo = None
    buffer_pcmu = bytearray()

    # 160 bytes equivalen a 20 milisegundos
    # de audio PCMU a 8 kHz.
    tamano_paquete = 160

    # Este ciclo permite múltiples turnos
    # de conversación.
    while True:
        async for respuesta in (
            session.receive()
        ):
            tool_call = getattr(respuesta, "tool_call", None)
            if tool_call:
                await procesar_end_call_de_gemini(
                    session,
                    call_control_id,
                    end_call_settings,
                    handoff_settings,
                    tool_call,
                )

            contenido = getattr(respuesta, "server_content", None)

            if not contenido:
                continue

            context = CALL_CONTEXTS.get(call_control_id=call_control_id)
            if context is None or context.transfer_state != "idle":
                # Después de pedir la transferencia Gemini deja de hablar y
                # de controlar la llamada; Telnyx se encarga del aviso y del
                # puente entre la persona y el asesor.
                buffer_pcmu.clear()
                estado_resampleo = None
                if CALL_CONTEXTS.consume_media_clear(call_control_id):
                    await websocket.send_json({"event": "clear"})
                    print("Audio pendiente de Gemini limpiado para transferencia.")
                continue

            # Permitir que la persona
            # interrumpa al agente.
            if contenido.interrupted:
                buffer_pcmu.clear()
                estado_resampleo = None

                await websocket.send_json(
                    {
                        "event": "clear"
                    }
                )

                print(
                    "La persona interrumpió "
                    "al agente. Audio pendiente "
                    "cancelado."
                )

            turno = contenido.model_turn

            if turno and turno.parts:
                for parte in turno.parts:
                    datos = parte.inline_data

                    if (
                        not datos
                        or not datos.data
                    ):
                        continue

                    # Gemini entrega audio PCM
                    # a 24 kHz.
                    audio_pcm_24khz = (
                        datos.data
                    )

                    # Convertir de 24 kHz
                    # a 8 kHz.
                    (
                        audio_pcm_8khz,
                        estado_resampleo,
                    ) = audioop.ratecv(
                        audio_pcm_24khz,
                        2,
                        1,
                        24000,
                        8000,
                        estado_resampleo,
                    )

                    # Convertir PCM a PCMU.
                    audio_pcmu = (
                        audioop.lin2ulaw(
                            audio_pcm_8khz,
                            2,
                        )
                    )

                    buffer_pcmu.extend(
                        audio_pcmu
                    )

                    while (
                        len(buffer_pcmu)
                        >= tamano_paquete
                    ):
                        paquete = bytes(
                            buffer_pcmu[
                                :tamano_paquete
                            ]
                        )

                        del buffer_pcmu[
                            :tamano_paquete
                        ]

                        paquete_base64 = (
                            base64.b64encode(
                                paquete
                            ).decode(
                                "ascii"
                            )
                        )

                        await (
                            websocket.send_json(
                                {
                                    "event": "media",
                                    "media": {
                                        "payload": (
                                            paquete_base64
                                        )
                                    },
                                }
                            )
                        )

            # Mandar el audio que quede
            # cuando Gemini termine el turno.
            if contenido.turn_complete:
                if buffer_pcmu:
                    faltan = tamano_paquete - len(buffer_pcmu)

                    if faltan > 0:
                        buffer_pcmu.extend(b"\xff" * faltan)

                    paquete_base64 = base64.b64encode(
                        bytes(buffer_pcmu)
                    ).decode("ascii")

                    await websocket.send_json(
                        {
                            "event": "media",
                            "media": {
                                "payload": paquete_base64,
                            },
                        }
                    )
                    buffer_pcmu.clear()

                closing_phase = CALL_CONTEXTS.complete_closure_turn(
                    call_control_id
                )
                if closing_phase:
                    print(
                        "Audio de cierre preparado finalizó: "
                        f"phase={closing_phase}."
                    )
                else:
                    print(
                        "Gemini terminó un turno. "
                        "Esperando la siguiente respuesta."
                    )


# ---------------------------------------------------------
# WEBSOCKET: PUENTE TELNYX ↔ GEMINI
# ---------------------------------------------------------

@app.websocket("/media")
async def websocket_audio_telnyx(
    websocket: WebSocket,
):
    """
    Conecta el audio de Telnyx con
    Gemini Live en ambas direcciones.
    """

    await websocket.accept()

    print(
        "TELNYX ABRIÓ EL WEBSOCKET /media"
    )

    call_control_id = None

    try:
        call_context = await recibir_inicio_telnyx(websocket)
        call_control_id = call_context.call_control_id
        if call_context.configuration_failed or call_context.agent_config is None:
            raise RuntimeError("La configuración del negocio no está disponible.")

        system_prompt, prompt_source = select_system_prompt(
            call_context.agent_config,
            SYSTEM_PROMPT,
            PANEL_OBSERVATION_SETTINGS,
        )
        end_call_settings = select_end_call_settings(call_context.agent_config.end_call)
        handoff_settings = select_handoff_settings(call_context.agent_config.handoff)
        voice_name, thinking_level = select_live_session_settings(call_context.agent_config)
        print(f"Prompt de llamada preparado: source={prompt_source}.")
        print(
            "Configuración de Gemini Live preparada: "
            f"voice={voice_name} thinking={thinking_level}."
        )

        gemini_api_key = select_gemini_api_key(
            call_context.agent_config,
            shared_secret=PANEL_OBSERVATION_SETTINGS.shared_secret,
        )
        print("Credencial Gemini preparada: source=negocio.")

        cliente_gemini = genai.Client(
            api_key=gemini_api_key
        )

        configuracion = {
            "response_modalities": [
                "AUDIO"
            ],
            "system_instruction": (
                system_prompt
                + end_call_runtime_instruction(end_call_settings)
                + transfer_call_runtime_instruction(handoff_settings)
            ),
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {
                        "voice_name": voice_name
                    }
                }
            },
            "thinking_config": {
                "thinking_level": thinking_level
            },
        }
        function_declarations = [tool for tool in (
            end_call_tool_declaration(end_call_settings),
            transfer_call_tool_declaration(handoff_settings),
        ) if tool]
        if function_declarations:
            configuracion["tools"] = [{"function_declarations": function_declarations}]

        async with (
            cliente_gemini.aio.live.connect(
                model=GEMINI_MODEL,
                config=configuracion,
            )
        ) as session:

            print(
                "Sesión de Gemini Live "
                "conectada."
            )
            CALL_CONTEXTS.mark_runtime_ready(call_control_id, True)

            # El agente inicia hablando; en salida usa el saludo de prospección.
            opening_instruction = (
                "Inicia la llamada ahora siguiendo exactamente la identidad, el saludo saliente "
                "y las reglas definidas en tus instrucciones. Preséntate brevemente, confirma si es "
                "buen momento para hablar y no asumas interés previo."
                if call_context.direction == "outbound"
                else "Inicia la llamada ahora siguiendo exactamente la identidad, el saludo entrante y las "
                "reglas definidas en tus instrucciones. Si no existe un saludo específico, preséntate "
                "brevemente y pregunta cómo puedes ayudar."
            )
            await session.send_realtime_input(
                text=opening_instruction
            )

            tarea_entrada = (
                asyncio.create_task(
                    enviar_audio_telnyx_a_gemini(
                        websocket,
                        session,
                        call_control_id,
                    )
                )
            )

            tarea_salida = (
                asyncio.create_task(
                    enviar_audio_gemini_a_telnyx(
                        websocket,
                        session,
                        call_control_id,
                        end_call_settings,
                        handoff_settings,
                    )
                )
            )

            tarea_cierre = asyncio.create_task(
                enviar_instrucciones_de_cierre_a_gemini(
                    session,
                    call_control_id,
                )
            )

            terminadas, pendientes = (
                await asyncio.wait(
                    {
                        tarea_entrada,
                        tarea_salida,
                        tarea_cierre,
                    },
                    return_when=(
                        asyncio.FIRST_COMPLETED
                    ),
                )
            )

            for tarea in pendientes:
                tarea.cancel()

            await asyncio.gather(
                *pendientes,
                return_exceptions=True,
            )

            for tarea in terminadas:
                error = tarea.exception()

                if error:
                    raise error

    except WebSocketDisconnect:
        print(
            "La llamada desconectó "
            "el WebSocket."
        )

    except Exception as error:
        print(
            "ERROR en el puente Telnyx-Gemini: "
            f"{type(error).__name__}"
        )

    finally:
        if call_control_id:
            CALL_CONTEXTS.mark_runtime_ready(call_control_id, False)
        try:
            await websocket.close()

        except Exception:
            pass


# ---------------------------------------------------------
# RUTA PARA COMPROBAR EL SERVIDOR
# ---------------------------------------------------------

@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": (
            "Agente de voz LealtadApps"
        ),
        "websocket": "/media",
        "model": GEMINI_MODEL,
        "max_call_seconds": (
            MAX_CALL_SECONDS
        ),
    }


# ---------------------------------------------------------
# ARRANQUE DEL SERVIDOR
# ---------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
    )
