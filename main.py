import os
import uvicorn
import httpx
from fastapi import FastAPI, Request, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

app = FastAPI()

# --- LA PERSONALIDAD DE TU AGENTE (SYSTEM PROMPT) ---
SYSTEM_PROMPT = """Eres el cerrador de ventas estrella y representante de LealtadApps, operando desde Los Mochis, Sinaloa. 
Tu objetivo principal es hacer ventas en frío, captar el interés de los negocios y cerrar tratos por teléfono.
IMPORTANTE: Habla SIEMPRE en español con un acento mexicano muy natural. Sé extremadamente carismático, persuasivo, amigable y empático. Utiliza un tono conversacional de México (ej. "claro que sí", "qué tal", "con muchísimo gusto").
Ofrecemos soluciones de software que hacen crecer a los negocios: tarjetas de sellos digitales, aplicaciones de cashback y CRMs a la medida. Tu meta es convencerlos de que estas herramientas les harán ganar más dinero y retener a sus clientes.
REGLA DE ORO SOBRE TU IDENTIDAD: Si el prospecto te pregunta si eres un robot, bot o inteligencia artificial, NUNCA des una respuesta robótica. En su lugar, bromea de forma ligera y amigable. Diles algo como que eres un "Ejecutivo de Ventas Inteligente" de nueva generación, y que la ventaja es que te pueden marcar a cualquier hora porque no tomas vacaciones ni pausas para el café. Haz que suene simpático y humano.
Tus respuestas deben ser siempre muy breves, directas al punto y diseñadas para una llamada telefónica fluida (evita listas o monólogos largos). Mantén el control de la conversación haciendo preguntas cortas para entender las necesidades de su negocio.
Si preguntan por detalles técnicos de programación que no sepas, ofrece tomar sus datos para que un desarrollador humano de tu equipo los contacte de inmediato."""


async def contestar_llamada_telnyx(call_control_id: str):
    """Ordena a Telnyx que conteste la llamada entrante usando la API oficial"""
    telnyx_api_key = os.getenv("TELNYX_API_KEY")
    url = f"https://api.telnyx.com/v2/calls/{call_control_id}/actions/answer"
    headers = {
        "Authorization": f"Bearer {telnyx_api_key}",
        "Content-Type": "application/json"
    }
        async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                url,
                headers=headers,
                json={
                    "stream_url": "wss://vozagent.lealtadapps.com/media",
                    "stream_track": "inbound_track",
                    "stream_codec": "PCMU",
                    "stream_bidirectional_mode": "rtp",
                    "stream_bidirectional_codec": "PCMU"
                }
            )

            print(
                f"Respuesta de Telnyx al contestar la llamada: "
                f"{response.status_code} - {response.text}"
            )

        except Exception as e:
            print(f"Error al conectar con la API de Telnyx: {e}")


# --- LA PUERTA DE ENTRADA (EL WEBHOOK) ---
@app.post("/webhooks/telnyx")
async def telnyx_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Ruta exacta configurada en Telnyx para recibir las llamadas entrantes.
    """
    try:
        payload = await request.json()
        event_type = payload.get("data", {}).get("event_type")
        
        print(f"Evento recibido de Telnyx: {event_type}")
        
        # Si el evento es una llamada entrante...
        if event_type == "call.initiated":
            call_control_id = payload["data"]["payload"]["call_control_id"]
            print(f"¡Llamada entrante detectada! Contestando ID: {call_control_id}")
            
            # Ejecutamos la acción de contestar en segundo plano
            background_tasks.add_task(contestar_llamada_telnyx, call_control_id)
            
        return JSONResponse(content={"status": "ok"}, status_code=200)

    except Exception as e:
        print(f"Error procesando el webhook de Telnyx: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)

@app.websocket("/media")
async def websocket_audio_telnyx(websocket: WebSocket):
    await websocket.accept()

    print("TELNYX ABRIO EL WEBSOCKET /media")

    paquetes_audio = 0

    try:
        while True:
            mensaje = await websocket.receive_json()
            evento = mensaje.get("event")

            if evento == "connected":
                print("Evento WebSocket recibido: connected")

            elif evento == "start":
                datos_inicio = mensaje.get("start", {})
                formato = datos_inicio.get("media_format", {})

                print("Evento WebSocket recibido: start")
                print(f"Formato de audio recibido: {formato}")

            elif evento == "media":
                paquetes_audio += 1

                if paquetes_audio == 1 or paquetes_audio % 50 == 0:
                    print(
                        f"Audio recibido desde Telnyx: "
                        f"{paquetes_audio} paquetes"
                    )

            elif evento == "stop":
                print("Telnyx detuvo el streaming de audio")
                break

            elif evento == "error":
                print(f"Error WebSocket de Telnyx: {mensaje}")

    except WebSocketDisconnect:
        print("WebSocket de Telnyx desconectado")

    except Exception as error:
        print(f"Error procesando audio de Telnyx: {error}")
@app.get("/")
async def root():
    return {"status": "Servidor de LealtadApps activo y operativo"}


# --- ARRANQUE DEL SERVIDOR ---
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
