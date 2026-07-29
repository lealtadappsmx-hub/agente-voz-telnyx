import json
import base64
import asyncio
import audioop
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
from gemini_key_selector import select_gemini_api_key
from panel_config_client import (
    CallDurationSettings,
    PanelObservationSettings,
    observe_panel_agent,
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

# Contextos efímeros de las llamadas activas. No usan base de datos ni polling.
CALL_CONTEXTS = CallContextStore()

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
):
    """Resuelve una vez el agente y adjunta sus credenciales efímeras."""
    observation = await observe_panel_agent(
        called_number,
        settings=PANEL_OBSERVATION_SETTINGS,
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
):
    """Resuelve el negocio antes de contestar; no usa credenciales globales."""
    observation = await observar_y_guardar_contexto(
        call_control_id,
        called_number,
    )
    if observation is None:
        CALL_CONTEXTS.finish(call_control_id, "configuration_unavailable")
        print("Llamada no contestada: configuración del negocio no disponible.")
        return
    await contestar_y_abrir_audio(call_control_id)


# ---------------------------------------------------------
# WEBHOOK DE TELNYX
# ---------------------------------------------------------

@app.post("/webhooks/telnyx")
async def telnyx_webhook(
    request: Request,
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
                )
            )

        # La llamada terminó antes de los
        # cinco minutos o fue cortada por Telnyx.
        elif event_type == "call.hangup":
            call_control_id = (
                event_payload.get(
                    "call_control_id"
                )
            )

            cancelar_temporizador_llamada(
                call_control_id
            )

            CALL_CONTEXTS.finish(
                call_control_id,
                event_payload.get("hangup_cause"),
            )

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
            if CALL_CONTEXTS.is_closing(call_control_id):
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
        await session.send_realtime_input(
            text=(
                "Cierra la llamada ahora. Pronuncia exactamente el mensaje "
                "autorizado a continuación, sin agregar explicaciones, "
                "preguntas ni datos nuevos; después termina tu turno. "
                f"Mensaje autorizado: {message}"
            )
        )
        print(f"Instrucción de cierre enviada a Gemini: phase={phase}.")


async def procesar_end_call_de_gemini(
    session,
    call_control_id: str,
    settings: EndCallSettings,
    tool_call,
):
    """Valida la única herramienta de cierre y confirma el resultado a Gemini."""
    function_responses = []
    for function_call in tool_call.function_calls or []:
        reason = validate_end_call_request(
            function_call.name,
            function_call.args,
            settings,
        )
        accepted = bool(reason and iniciar_cierre_por_end_call(call_control_id, reason, settings))
        if accepted:
            result = {"action": END_CALL_ACTION, "status": "accepted"}
        else:
            result = {"action": END_CALL_ACTION, "status": "rejected"}
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
                    tool_call,
                )

            contenido = getattr(respuesta, "server_content", None)

            if not contenido:
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
                system_prompt + end_call_runtime_instruction(end_call_settings)
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
        end_call_tool = end_call_tool_declaration(end_call_settings)
        if end_call_tool:
            configuracion["tools"] = [{"function_declarations": [end_call_tool]}]

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

            # El agente inicia hablando.
            await session.send_realtime_input(
                text=(
                    "Inicia la llamada ahora siguiendo exactamente la identidad, "
                    "el saludo entrante y las reglas definidas en tus instrucciones. "
                    "Si no existe un saludo específico, preséntate brevemente y "
                    "pregunta cómo puedes ayudar."
                )
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
                    )
                )
            )

            tarea_cierre = asyncio.create_task(
                enviar_instrucciones_de_cierre_a_gemini(
                    session,
                    call_control_id,
                    end_call_settings,
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
