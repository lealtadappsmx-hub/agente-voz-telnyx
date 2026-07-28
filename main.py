import os
import json
import base64
import asyncio
import audioop

import httpx
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from google import genai
from google.genai import types

from call_context import CallContextStore
from panel_config_client import (
    PanelObservationSettings,
    observe_panel_agent,
    select_live_session_settings,
    select_system_prompt,
)


app = FastAPI()


# Dirección pública del WebSocket en EasyPanel
TELNYX_WS_URL = "wss://vozagent.lealtadapps.com/media"

# Modelo de Gemini Live que ya funciona en este proyecto
GEMINI_MODEL = "gemini-3.1-flash-live-preview"

# Duración máxima de cada llamada:
# 180 segundos equivalen a 3 minutos.
MAX_CALL_SECONDS = 180

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


# ---------------------------------------------------------
# COMPROBAR LAS VARIABLES DE EASYPANEL
# ---------------------------------------------------------

def revisar_variables():
    telnyx_api_key = os.getenv(
        "TELNYX_API_KEY",
        "",
    ).strip()

    gemini_api_key = os.getenv(
        "GEMINI_API_KEY",
        "",
    ).strip()

    if not telnyx_api_key:
        raise RuntimeError(
            "Falta la variable TELNYX_API_KEY en EasyPanel."
        )

    if not gemini_api_key:
        raise RuntimeError(
            "Falta la variable GEMINI_API_KEY en EasyPanel."
        )

    return telnyx_api_key, gemini_api_key


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

    telnyx_api_key, _ = revisar_variables()

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


async def cortar_llamada_por_tiempo(
    call_control_id: str,
):
    """
    Espera cinco minutos y después termina
    la llamada automáticamente.

    Si la persona cuelga antes, este
    temporizador se cancela.
    """

    try:
        print(
            "Temporizador de llamada iniciado: "
            f"{MAX_CALL_SECONDS} segundos."
        )

        await asyncio.sleep(
            MAX_CALL_SECONDS
        )

        print(
            "La llamada alcanzó el límite de "
            f"{MAX_CALL_SECONDS} segundos."
        )

        await colgar_llamada_telnyx(
            call_control_id
        )

        CALL_CONTEXTS.finish(
            call_control_id,
            "max_duration",
        )

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


async def observar_y_guardar_contexto(
    call_control_id: str,
    called_number: str,
):
    """Adjunta al contexto la configuración dinámica de esta llamada."""
    observation = await observe_panel_agent(
        called_number,
        settings=PANEL_OBSERVATION_SETTINGS,
    )
    if observation is not None:
        attached = CALL_CONTEXTS.set_agent_config(
            call_control_id,
            observation,
        )
        if attached:
            print("Configuración dinámica adjuntada al contexto.")


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

    telnyx_api_key, _ = revisar_variables()

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

        # Evitar temporizadores duplicados.
        cancelar_temporizador_llamada(
            call_control_id
        )

        # Crear el temporizador de cinco minutos.
        TAREAS_CORTE[call_control_id] = (
            asyncio.create_task(
                cortar_llamada_por_tiempo(
                    call_control_id
                )
            )
        )
        CALL_CONTEXTS.set_timer_state(
            call_control_id,
            "active",
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
                contestar_y_abrir_audio(
                    call_control_id
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

async def enviar_audio_gemini_a_telnyx(
    websocket: WebSocket,
    session,
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
            contenido = (
                respuesta.server_content
            )

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
            if (
                contenido.turn_complete
                and buffer_pcmu
            ):
                faltan = (
                    tamano_paquete
                    - len(buffer_pcmu)
                )

                if faltan > 0:
                    buffer_pcmu.extend(
                        b"\xff" * faltan
                    )

                paquete_base64 = (
                    base64.b64encode(
                        bytes(buffer_pcmu)
                    ).decode(
                        "ascii"
                    )
                )

                await websocket.send_json(
                    {
                        "event": "media",
                        "media": {
                            "payload": (
                                paquete_base64
                            )
                        },
                    }
                )

                buffer_pcmu.clear()

                print(
                    "Gemini terminó un turno. "
                    "Esperando la siguiente "
                    "respuesta."
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

    try:
        call_context = await recibir_inicio_telnyx(websocket)
        if PANEL_OBSERVATION_SETTINGS.enabled and call_context.agent_config is None:
            await observar_y_guardar_contexto(
                call_context.call_control_id,
                call_context.to_number,
            )
            call_context = CALL_CONTEXTS.get(call_control_id=call_context.call_control_id) or call_context

        system_prompt, prompt_source = select_system_prompt(
            call_context.agent_config,
            SYSTEM_PROMPT,
            PANEL_OBSERVATION_SETTINGS,
        )
        voice_name, thinking_level = select_live_session_settings(call_context.agent_config)
        print(f"Prompt de llamada preparado: source={prompt_source}.")
        print(
            "Configuración de Gemini Live preparada: "
            f"voice={voice_name} thinking={thinking_level}."
        )

        _, gemini_api_key = (
            revisar_variables()
        )

        cliente_gemini = genai.Client(
            api_key=gemini_api_key
        )

        configuracion = {
            "response_modalities": [
                "AUDIO"
            ],
            "system_instruction": (
                system_prompt
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
                    )
                )
            )

            tarea_salida = (
                asyncio.create_task(
                    enviar_audio_gemini_a_telnyx(
                        websocket,
                        session,
                    )
                )
            )

            terminadas, pendientes = (
                await asyncio.wait(
                    {
                        tarea_entrada,
                        tarea_salida,
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
