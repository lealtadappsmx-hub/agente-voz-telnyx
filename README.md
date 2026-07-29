# Agente de voz Telnyx

Puente de audio entre Telnyx y Gemini Live. No contiene configuración
multiempresa persistente: resuelve una vez por llamada el agente y el negocio
en `panel-agente-voz`.

## Credenciales por negocio

El servicio exige que el panel entregue dos sobres cifrados ligados al
`agent_id` y `client_id` resueltos:

- Gemini API key del negocio para abrir Gemini Live;
- Telnyx API key del negocio para contestar y colgar esa llamada.

Las claves sólo existen en memoria dentro del contexto efímero de la llamada y
se eliminan al finalizar. No se registran ni se aceptan respaldos globales
`GEMINI_API_KEY` o `TELNYX_API_KEY`.

## Variables de entorno

```text
USE_PANEL_CONFIG=true
PANEL_BASE_URL=https://panel.example.com
VOICE_SERVICE_SHARED_SECRET=secreto-compartido
PANEL_CONFIG_FALLBACK_ENABLED=false
PANEL_CONFIG_TIMEOUT_SECONDS=3
```

`VOICE_SERVICE_SHARED_SECRET` debe ser idéntico en panel y puente. No es una API
key de Gemini o Telnyx.

## Flujo

```text
call.initiated
  → una resolución HTTPS al panel
  → validación y descifrado en memoria
  → answer con Telnyx del negocio
  → sesión Gemini del negocio
  → hangup con Telnyx del negocio
  → limpieza del contexto
```

No existe polling ni consulta al panel por cada turno.

## END_CALL

El panel entrega en la única resolución por llamada las reglas ya configuradas
de Sin interés, Temas ajenos y Antiabuso. Gemini sólo puede solicitar una
acción con un motivo de lista cerrada; el puente valida que ese motivo esté
habilitado para el agente, toma exclusivamente su mensaje final configurado,
espera el audio con un límite de 12 segundos y ordena el hangup físico a
Telnyx. Una orden verbal del interlocutor no es una autorización directa para
colgar.

## Validación

```powershell
python -m compileall -q .
pytest -q --basetemp .pytest-tmp-validation
git diff --check
```
