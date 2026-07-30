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

## Llamadas salientes

El administrador del panel inicia una campaña o una llamada directa. El
puente autentica esa única orden, reclama de PostgreSQL un destinatario ya
cifrado por medio del panel y ejecuta `POST /v2/calls` de Telnyx usando la API
key, Call Control Application y número del negocio correcto. Al recibir
`call.hangup`, notifica el resultado al panel y reclama como máximo un nuevo
destinatario para llenar el lugar liberado. La simultaneidad se limita en
PostgreSQL a 1–3 llamadas y no hay worker, cron ni polling.

## Webhooks aislados por negocio

El puente acepta temporalmente la ruta histórica `/webhooks/telnyx` y la ruta
privada `/webhooks/telnyx/{token}`. Para cada negocio activo se configura en
Telnyx únicamente la ruta privada que muestra el panel. Al recibirla, el
puente usa el token una sola vez en la resolución HTTPS al panel; no lo guarda
en el contexto de la llamada ni lo registra. El panel verifica que token,
negocio y número Telnyx correspondan antes de entregar credenciales.

La ruta sin token es sólo compatibilidad de despliegue y debe retirarse cuando
todos los negocios hayan migrado. Esto no añade workers, polling ni consultas
durante el audio.

## END_CALL

El panel entrega en la única resolución por llamada las reglas ya configuradas
de Sin interés, Temas ajenos y Antiabuso. Gemini sólo puede solicitar una
acción con un motivo de lista cerrada; el puente valida que ese motivo esté
habilitado para el agente, toma exclusivamente su mensaje final configurado,
espera el audio con un límite de 12 segundos y ordena el hangup físico a
Telnyx. Una orden verbal del interlocutor no es una autorización directa para
colgar.

## Transferencia humana

Durante la única resolución de configuración, el panel entrega al puente el
destino ya descifrado únicamente en memoria, el tiempo de timbrado y la
alternativa de error del agente. Gemini recibe la herramienta `TRANSFER_CALL`
sin parámetros: no conoce ni controla el número o SIP de destino.

Después de reproducir el mensaje configurado, el puente envía el comando de
transferencia a Telnyx. Los webhooks `call.initiated`, `call.bridged` y
`call.hangup` resuelven el resultado; si no se enlaza, el flujo vuelve al agente
o se despide según la regla configurada. No existe polling, worker, cron ni
consulta recurrente al panel.

## Validación

```powershell
python -m compileall -q .
pytest -q --basetemp .pytest-tmp-validation
git diff --check
```
