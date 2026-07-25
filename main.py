import os
import uvicorn
import asyncio
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

# --- Librerías del Cerebro (Pipecat y Gemini con ruta corregida) ---
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.transports.network.telnyx import TelnyxTransport, TelnyxParams
from pipecat.services.gemini_multimodal_live.gemini import GeminiMultimodalLiveLLMService
from pipecat.processors.aggregators.llm_response import LLMAssistantResponseAggregator
from pipecat.processors.aggregators.llm_user_context import LLMUserContextAggregator

app = FastAPI()

# --- 1. LA PERSONALIDAD DE TU AGENTE (SYSTEM PROMPT) ---
# Respetando fielmente tu prompt de cerrador de ventas experto de Los Mochis
SYSTEM_PROMPT = """Eres el cerrador de ventas estrella y representante de LealtadApps, operando desde Los Mochis, Sinaloa. 
Tu objetivo principal es hacer ventas en frío, captar el interés de los negocios y cerrar tratos por teléfono.
IMPORTANTE: Habla SIEMPRE en español con un acento mexicano muy natural. Sé extremadamente carismático, persuasivo, amigable y empático. Utiliza un tono conversacional de México (ej. "claro que sí", "qué tal", "con muchísimo gusto").
Ofrecemos soluciones de software que hacen crecer a los negocios: tarjetas de sellos digitales, aplicaciones de cashback y CRMs a la medida. Tu meta es convencerlos de que estas herramientas les harán ganar más dinero y retener a sus clientes.
REGLA DE ORO SOBRE TU IDENTIDAD: Si el prospecto te pregunta si eres un robot, bot o inteligencia artificial, NUNCA des una respuesta robótica. En su lugar, bromea de forma ligera y amigable. Diles algo como que eres un "Ejecutivo de Ventas Inteligente" de nueva generación, y que la ventaja es que te pueden marcar a cualquier hora porque no tomas vacaciones ni pausas para el café. Haz que suene simpático y humano.
Tus respuestas deben ser siempre muy breves, directas al punto y diseñadas para una llamada telefónica fluida (evita listas o monólogos largos). Mantén el control de la conversación haciendo preguntas cortas para entender las necesidades de su negocio.
If preguntan por detalles técnicos de programación que no sepas, ofrece tomar sus datos para que un desarrollador humano de tu equipo los contacte de inmediato."""

# --- 2. EL MOTOR DE INTELIGENCIA (PIPELINE) ---
async def iniciar_agente_pipecat(call_control_id: str):
    """Esta función despierta a Gemini y lo conecta a la llamada de Telnyx"""
    try:
        # A. Conectamos el audio de la llamada (Telnyx)
        transport = TelnyxTransport(
            TelnyxParams(
                api_key=os.getenv("TELNYX_API_KEY"),
                call_control_id=call_control_id
            )
        )

        # B. Conectamos el cerebro (Gemini Multimodal Live)
        llm = GeminiMultimodalLiveLLMService(
            api_key=os.getenv("GEMINI_API_KEY"),
            system_instruction=SYSTEM_PROMPT
        )

        # C. Configuramos la memoria de la conversación
        user_context = LLMUserContextAggregator(llm)
        assistant_response = LLMAssistantResponseAggregator(llm)

        # D. Construimos la tubería por donde viaja el audio: 
        # Telnyx -> Memoria -> Gemini -> Respuesta -> Telnyx
        pipeline = Pipeline([
            transport.input(),
            user_context,
            llm,
            assistant_response,
            transport.output()
        ])

        # E. Ejecutamos al agente permitiendo que el humano lo pueda interrumpir al hablar
        task = PipelineTask(pipeline, PipelineParams(allow_interruptions=True))
        runner = PipelineRunner()
        
        await runner.run(task)
        
    except Exception as e:
        print(f"Error en la ejecución del agente: {e}")


# --- 3. LA PUERTA DE ENTRADA (EL WEBHOOK) ---
@app.post("/webhooks/telnyx")
async def telnyx_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Esta es la ruta exacta que configuraste en el portal.
    Telnyx tocará esta puerta cada vez que alguien marque tu número.
    """
    try:
        payload = await request.json()
        event_type = payload.get("data", {}).get("event_type")
        
        # Si el evento es una llamada nueva que está entrando...
        if event_type == "call.initiated":
            call_control_id = payload["data"]["payload"]["call_control_id"]
            print(f"¡Llamada entrante detectada! ID: {call_control_id}")
            
            # Mandamos a contestar al agente en segundo plano para no bloquear el servidor
            background_tasks.add_task(iniciar_agente_pipecat, call_control_id)
            
        return JSONResponse(content={"status": "ok"}, status_code=200)

    except Exception as e:
        print(f"Error procesando el webhook de Telnyx: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)

# --- 4. ARRANQUE DEL SERVIDOR ---
if __name__ == "__main__":
    # El puerto 8000 es vital porque es el que enrutamos en Easypanel
    uvicorn.run(app, host="0.0.0.0", port=8000)
