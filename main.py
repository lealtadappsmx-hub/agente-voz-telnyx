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


app = FastAPI()


# Dirección pública del WebSocket en EasyPanel
TELNYX_WS_URL = "wss://vozagent.lealtadapps.com/media"

# Modelo actual de Gemini Live
GEMINI_MODEL = "gemini-3.1-flash-live-preview"


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
    telnyx_api_key = os.getenv("TELNYX_API_KEY", "").strip()
    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()

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
# CONTESTAR LA LLAMADA Y ABRIR EL STREAM DE AUDIO
# ---------------------------------------------------------

async def contestar_y_abrir_audio(call_control_id):
    """
    Contesta la llamada y ordena a Telnyx abrir un WebSocket
    bidireccional hacia nuestro servidor.
    """

    telnyx_api_key, _ = revisar_variables()

    url = (
        f"https://api.telnyx.com/v2/calls/"
        f"{call_control_id}/actions/answer"
    )

    headers = {
        "Authorization": f"Bearer {telnyx_api_key}",
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
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                url,
                headers=headers,
                json=payload,
            )

        print(
            "Respuesta de Telnyx al contestar: "
            f"{response.status_code} - {response.text}"
        )

        response.raise_for_status()

    except Exception as error:
        print(
            "ERROR contestando la llamada en Telnyx: "
            f"{error}"
        )


# ---------------------------------------------------------
# WEBHOOK DE TELNYX
# ---------------------------------------------------------

@app.post("/webhooks/telnyx")
async def telnyx_webhook(request: Request):
    """
    Recibe los eventos enviados por Telnyx.
    """

    try:
        payload = await request.json()

        event_type = payload.get(
            "data", {}
        ).get(
            "event_type"
        )

        print(
            f"Evento recibido de Telnyx: {event_type}"
        )

        if event_type == "call.initiated":
            call_control_id = payload[
                "data"
            ][
                "payload"
            ][
                "call_control_id"
            ]

            print(
                "Llamada entrante detectada: "
                f"{call_control_id}"
            )

            asyncio.create_task(
                contestar_y_abrir_audio(
                    call_control_id
                )
            )

        return JSONResponse(
            content={"status": "ok"},
            status_code=200,
        )

    except Exception as error:
        print(
            "ERROR procesando el webhook de Telnyx: "
            f"{error}"
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
    websocket,
    session,
):
    """
    Recibe audio PCMU de Telnyx, lo convierte a PCM
    y lo envía a Gemini Live.
    """

    paquetes = 0

    while True:
        texto = await websocket.receive_text()

        mensaje = json.loads(texto)

        evento = mensaje.get("event")

        if evento == "connected":
            print(
                "Telnyx conectó el WebSocket."
            )

        elif evento == "start":
            print(
                "Telnyx inició el stream de audio."
            )

            print(
                "Datos de inicio: "
                f"{mensaje.get('start', {})}"
            )

        elif evento == "media":
            audio_base64 = mensaje.get(
                "media", {}
            ).get(
                "payload"
            )

            if not audio_base64:
                continue

            # Audio PCMU recibido desde Telnyx
            audio_pcmu = base64.b64decode(
                audio_base64
            )

            # Convertir PCMU a PCM de 16 bits
            audio_pcm_8khz = audioop.ulaw2lin(
                audio_pcmu,
                2,
            )

            # Enviar audio a Gemini Live
            await session.send_realtime_input(
                audio=types.Blob(
                    data=audio_pcm_8khz,
                    mime_type="audio/pcm;rate=8000",
                )
            )

            paquetes += 1

            if paquetes == 1 or paquetes % 100 == 0:
                print(
                    "Audio enviado a Gemini: "
                    f"{paquetes} paquetes"
                )

        elif evento == "stop":
            print(
                "Telnyx detuvo el stream de audio."
            )

            try:
                await session.send_realtime_input(
                    audio_stream_end=True
                )
            except Exception:
                pass

            return

        elif evento == "error":
            print(
                "ERROR enviado por Telnyx: "
                f"{mensaje}"
            )

        elif evento == "dtmf":
            tecla = mensaje.get(
                "dtmf", {}
            ).get(
                "digit"
            )

            print(
                f"Tecla recibida: {tecla}"
            )


# ---------------------------------------------------------
# ENVIAR AUDIO DE GEMINI HACIA TELNYX
# ---------------------------------------------------------

async def enviar_audio_gemini_a_telnyx(
    websocket,
    session,
):
    """
    Recibe audio PCM de Gemini, lo convierte a PCMU
    y lo reproduce en la llamada de Telnyx.
    """

    estado_resampleo = None

    buffer_pcmu = bytearray()

    # 160 bytes equivalen a 20 milisegundos
    # de audio PCMU a 8 kHz.
    tamano_paquete = 160

    async for respuesta in session.receive():
        contenido = respuesta.server_content

        if not contenido:
            continue

        # Permite que la persona interrumpa al agente.
        if contenido.interrupted:
            buffer_pcmu.clear()

            estado_resampleo = None

            await websocket.send_json(
                {
                    "event": "clear"
                }
            )

            print(
                "La persona interrumpió al agente. "
                "Audio pendiente cancelado."
            )

        turno = contenido.model_turn

        if turno and turno.parts:
            for parte in turno.parts:
                datos = parte.inline_data

                if not datos:
                    continue

                if not datos.data:
                    continue

                # Gemini entrega audio PCM a 24 kHz.
                audio_pcm_24khz = datos.data

                # Convertir de 24 kHz a 8 kHz.
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

                # Convertir PCM a PCMU para Telnyx.
                audio_pcmu = audioop.lin2ulaw(
                    audio_pcm_8khz,
                    2,
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

        # Enviar cualquier audio que quede al finalizar
        # la respuesta de Gemini.
        if contenido.turn_complete and buffer_pcmu:
            faltan = (
                tamano_paquete
                - len(buffer_pcmu)
            )

            if faltan > 0:
                # FF representa silencio en PCMU.
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
                        "payload": paquete_base64
                    },
                }
            )

            buffer_pcmu.clear()


# ---------------------------------------------------------
# WEBSOCKET: PUENTE TELNYX ↔ GEMINI
# ---------------------------------------------------------

@app.websocket("/media")
async def websocket_audio_telnyx(
    websocket: WebSocket
):
    """
    Conecta el audio de Telnyx con Gemini Live
    en ambas direcciones.
    """

    await websocket.accept()

    print(
        "TELNYX ABRIÓ EL WEBSOCKET /media"
    )

    try:
        _, gemini_api_key = revisar_variables()

        cliente_gemini = genai.Client(
            api_key=gemini_api_key
        )

        configuracion = {
            "response_modalities": [
                "AUDIO"
            ],
            "system_instruction": (
                SYSTEM_PROMPT
            ),
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {
                        "voice_name": "Kore"
                    }
                }
            },
            "thinking_config": {
                "thinking_level": "minimal"
            },
        }

        async with (
            cliente_gemini.aio.live.connect(
                model=GEMINI_MODEL,
                config=configuracion,
            )
        ) as session:

            print(
                "Sesión de Gemini Live conectada."
            )

            # Indica a Gemini que debe hablar primero.
            await session.send_realtime_input(
                text=(
                    "Inicia la llamada ahora. "
                    "Saluda brevemente, di que eres "
                    "el asistente virtual de LealtadApps "
                    "y pregunta con quién tienes el gusto."
                )
            )

            tarea_entrada = asyncio.create_task(
                enviar_audio_telnyx_a_gemini(
                    websocket,
                    session,
                )
            )

            tarea_salida = asyncio.create_task(
                enviar_audio_gemini_a_telnyx(
                    websocket,
                    session,
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
            f"{type(error).__name__}: {error}"
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
