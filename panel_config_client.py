"""Cliente de configuración dinámica del agente resuelto por el panel."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter

import httpx


# Uvicorn ya configura este logger en INFO dentro del contenedor.
logger = logging.getLogger("uvicorn.error")


# Gemini 3.1 Flash Live admite las voces predefinidas de Gemini TTS. Mantener
# esta lista explícita evita enviar nombres arbitrarios desde el panel a la API.
DEFAULT_VOICE_NAME = "Kore"
DEFAULT_THINKING_LEVEL = "minimal"
DEFAULT_MAX_CALL_SECONDS = 180
MAX_TEXT_CONTROL_LENGTH = 500
SUPPORTED_VOICE_NAMES = frozenset(
    {
        "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
        "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
        "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
        "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
        "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
    }
)
SUPPORTED_THINKING_LEVELS = frozenset({"minimal", "low", "medium", "high"})
_VOICE_BY_CASEFOLD = {voice.casefold(): voice for voice in SUPPORTED_VOICE_NAMES}


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _timeout_seconds(value: str | None) -> float:
    try:
        timeout = float(value or "3")
    except ValueError:
        return 3.0
    return min(max(timeout, 0.25), 10.0)


def _safe_log_name(value: str) -> str:
    return " ".join(value.split())[:120]


def _selected_voice_name(value: object) -> str:
    if not isinstance(value, str):
        return DEFAULT_VOICE_NAME
    return _VOICE_BY_CASEFOLD.get(value.strip().casefold(), DEFAULT_VOICE_NAME)


def _selected_thinking_level(value: object) -> str:
    if not isinstance(value, str):
        return DEFAULT_THINKING_LEVEL
    candidate = value.strip().lower()
    return candidate if candidate in SUPPORTED_THINKING_LEVELS else DEFAULT_THINKING_LEVEL


def _selected_control_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:MAX_TEXT_CONTROL_LENGTH] or None


@dataclass(frozen=True)
class PanelObservationSettings:
    enabled: bool
    base_url: str
    shared_secret: str
    fallback_enabled: bool
    timeout_seconds: float

    @classmethod
    def from_environment(cls) -> "PanelObservationSettings":
        return cls(
            enabled=_as_bool(os.getenv("USE_PANEL_CONFIG"), False),
            base_url=os.getenv("PANEL_BASE_URL", "https://panelvoz.lealtadapps.com").strip().rstrip("/"),
            shared_secret=os.getenv("VOICE_SERVICE_SHARED_SECRET", "").strip(),
            fallback_enabled=_as_bool(os.getenv("PANEL_CONFIG_FALLBACK_ENABLED"), True),
            timeout_seconds=_timeout_seconds(os.getenv("PANEL_CONFIG_TIMEOUT_SECONDS")),
        )


@dataclass(frozen=True)
class PanelAgentObservation:
    agent_id: int
    client_id: int
    agent_name: str
    system_prompt: str = field(repr=False)
    elapsed_ms: float
    voice_name: str = DEFAULT_VOICE_NAME
    thinking_level: str = DEFAULT_THINKING_LEVEL
    max_call_seconds: int = DEFAULT_MAX_CALL_SECONDS
    farewell_seconds_before_end: int = 0
    time_warning_message: str | None = field(default=None, repr=False)
    final_farewell: str | None = field(default=None, repr=False)
    gemini_credential_envelope: str | None = field(default=None, repr=False)
    telnyx_credential_envelope: str | None = field(default=None, repr=False)
    end_call: dict[str, object] = field(default_factory=dict, repr=False)
    handoff: dict[str, object] = field(default_factory=dict, repr=False)
    capture: dict[str, object] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class CallDurationSettings:
    """Controles físicos de duración validados para una sola llamada."""

    max_call_seconds: int = DEFAULT_MAX_CALL_SECONDS
    farewell_seconds_before_end: int = 0
    time_warning_message: str | None = field(default=None, repr=False)
    final_farewell: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class OutboundCallClaim:
    """Una reserva puntual del panel; no contiene datos aptos para logs."""

    campaign_id: int
    recipient_id: int
    to_number: str = field(repr=False)
    from_number: str = field(repr=False)
    connection_id: str = field(repr=False)
    observation: PanelAgentObservation = field(repr=False)


def select_system_prompt(
    agent_config: PanelAgentObservation | None,
    fallback_prompt: str,
    settings: PanelObservationSettings,
) -> tuple[str, str]:
    """Elige el prompt por llamada sin imprimir ni exponer su contenido."""
    if agent_config is not None and agent_config.system_prompt.strip():
        return agent_config.system_prompt, "panel"
    if settings.enabled and not settings.fallback_enabled:
        raise RuntimeError("No fue posible obtener la configuración del agente.")
    return fallback_prompt, "respaldo"


def select_live_session_settings(
    agent_config: PanelAgentObservation | None,
) -> tuple[str, str]:
    """Devuelve únicamente valores compatibles con Gemini Live para esta llamada."""
    if agent_config is None:
        return DEFAULT_VOICE_NAME, DEFAULT_THINKING_LEVEL
    return (
        _selected_voice_name(agent_config.voice_name),
        _selected_thinking_level(agent_config.thinking_level),
    )


def select_call_duration_settings(
    agent_config: PanelAgentObservation | None,
) -> CallDurationSettings:
    """Selecciona duración del panel sin alterar el corte fijo de respaldo."""
    if agent_config is None:
        return CallDurationSettings()

    maximum = agent_config.max_call_seconds
    farewell_seconds = agent_config.farewell_seconds_before_end
    if type(maximum) is not int or not 30 <= maximum <= 1800:
        return CallDurationSettings()
    if type(farewell_seconds) is not int or not 0 <= farewell_seconds < maximum:
        return CallDurationSettings(max_call_seconds=maximum)
    return CallDurationSettings(
        max_call_seconds=maximum,
        farewell_seconds_before_end=farewell_seconds,
        time_warning_message=_selected_control_text(agent_config.time_warning_message),
        final_farewell=_selected_control_text(agent_config.final_farewell),
    )


async def observe_panel_agent(
    called_number: str,
    settings: PanelObservationSettings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    webhook_token: str | None = None,
) -> PanelAgentObservation | None:
    """Consulta una vez la configuración del agente para una llamada entrante."""
    config = settings or PanelObservationSettings.from_environment()
    if not config.enabled:
        return None

    started_at = perf_counter()
    if not called_number or not config.base_url or not config.shared_secret:
        logger.warning("Panel no observado: reason=incomplete_configuration")
        return None

    if not config.base_url.startswith("https://"):
        logger.warning("Panel no observado: reason=invalid_base_url")
        return None

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(config.timeout_seconds),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=0),
            transport=transport,
            follow_redirects=False,
        ) as client:
            request_payload = {"called_number": called_number, "direction": "inbound"}
            # El token llega exclusivamente desde la ruta privada del webhook.
            # No se registra ni se conserva en el contexto de llamada.
            if webhook_token is not None:
                request_payload["webhook_token"] = webhook_token
            response = await client.post(
                f"{config.base_url}/internal/v1/voice/resolve-agent",
                headers={"X-Voice-Service-Key": config.shared_secret},
                json=request_payload,
            )
    except httpx.HTTPError:
        elapsed_ms = (perf_counter() - started_at) * 1000
        logger.warning("Panel no observado: reason=request_failed elapsed_ms=%.2f", elapsed_ms)
        return None

    elapsed_ms = (perf_counter() - started_at) * 1000
    if response.status_code != 200:
        logger.warning(
            "Panel no observado: reason=http_status status_code=%s elapsed_ms=%.2f",
            response.status_code,
            elapsed_ms,
        )
        return None

    try:
        payload = response.json()
        agent = payload["agent"]
        agent_id = agent["id"]
        client_id = agent["client_id"]
        agent_name = agent["name"]
        system_prompt = agent["system_prompt"]
        voice_name = agent.get("voice_name")
        thinking_level = agent.get("thinking_level")
        conversation = payload.get("conversation")
        if not isinstance(conversation, dict):
            conversation = {}
        max_call_seconds = conversation.get("max_call_seconds")
        farewell_seconds_before_end = conversation.get("farewell_seconds_before_end")
        time_warning_message = conversation.get("time_warning_message")
        final_farewell = conversation.get("final_farewell")
        end_call = payload.get("end_call")
        if not isinstance(end_call, dict):
            end_call = {}
        handoff = payload.get("handoff")
        if not isinstance(handoff, dict):
            handoff = {}
        capture = payload.get("capture")
        if not isinstance(capture, dict):
            capture = {}
        runtime = payload.get("runtime")
        if not isinstance(runtime, dict):
            runtime = {}
        gemini_credential_envelope = runtime.get("gemini_credential_envelope")
        telnyx_credential_envelope = runtime.get("telnyx_credential_envelope")
        if (
            type(agent_id) is not int
            or type(client_id) is not int
            or not isinstance(agent_name, str)
            or not isinstance(system_prompt, str)
        ):
            raise ValueError("invalid observation fields")
        safe_name = _safe_log_name(agent_name)
        safe_prompt = system_prompt.strip()
        if not safe_name or not (20 <= len(safe_prompt) <= 60000):
            raise ValueError("invalid agent configuration")
        if not isinstance(gemini_credential_envelope, str):
            gemini_credential_envelope = None
        elif not gemini_credential_envelope.strip() or len(gemini_credential_envelope) > 10_000:
            gemini_credential_envelope = None
        if not isinstance(telnyx_credential_envelope, str):
            telnyx_credential_envelope = None
        elif not telnyx_credential_envelope.strip() or len(telnyx_credential_envelope) > 10_000:
            telnyx_credential_envelope = None
    except (KeyError, TypeError, ValueError):
        logger.warning("Panel no observado: reason=invalid_response elapsed_ms=%.2f", elapsed_ms)
        return None

    observation = PanelAgentObservation(
        agent_id=agent_id,
        client_id=client_id,
        agent_name=safe_name,
        system_prompt=safe_prompt,
        elapsed_ms=elapsed_ms,
        voice_name=_selected_voice_name(voice_name),
        thinking_level=_selected_thinking_level(thinking_level),
        max_call_seconds=max_call_seconds,
        farewell_seconds_before_end=farewell_seconds_before_end,
        time_warning_message=_selected_control_text(time_warning_message),
        final_farewell=_selected_control_text(final_farewell),
        gemini_credential_envelope=gemini_credential_envelope,
        telnyx_credential_envelope=telnyx_credential_envelope,
        end_call=end_call,
        handoff=handoff,
        capture=capture,
    )
    logger.info(
        "Panel resuelto: agent_id=%s client_id=%s agent_name=%s prompt=ready elapsed_ms=%.2f",
        observation.agent_id,
        observation.client_id,
        observation.agent_name,
        observation.elapsed_ms,
    )
    return observation


def _observation_from_payload(payload: object, elapsed_ms: float) -> PanelAgentObservation | None:
    """Valida la misma respuesta firmada que usa la ruta entrante, sin registrar secretos."""
    try:
        if not isinstance(payload, dict):
            raise ValueError("invalid payload")
        agent = payload["agent"]
        if not isinstance(agent, dict):
            raise ValueError("invalid agent")
        agent_id, client_id = agent["id"], agent["client_id"]
        agent_name, system_prompt = agent["name"], agent["system_prompt"]
        conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
        runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
        if type(agent_id) is not int or type(client_id) is not int or not isinstance(agent_name, str) or not isinstance(system_prompt, str):
            raise ValueError("invalid observation fields")
        safe_name, safe_prompt = _safe_log_name(agent_name), system_prompt.strip()
        if not safe_name or not (20 <= len(safe_prompt) <= 60000):
            raise ValueError("invalid agent configuration")
        gemini_envelope = runtime.get("gemini_credential_envelope")
        telnyx_envelope = runtime.get("telnyx_credential_envelope")
        if not isinstance(gemini_envelope, str) or not gemini_envelope.strip() or len(gemini_envelope) > 10_000:
            gemini_envelope = None
        if not isinstance(telnyx_envelope, str) or not telnyx_envelope.strip() or len(telnyx_envelope) > 10_000:
            telnyx_envelope = None
        return PanelAgentObservation(
            agent_id=agent_id, client_id=client_id, agent_name=safe_name, system_prompt=safe_prompt,
            elapsed_ms=elapsed_ms, voice_name=_selected_voice_name(agent.get("voice_name")),
            thinking_level=_selected_thinking_level(agent.get("thinking_level")),
            max_call_seconds=conversation.get("max_call_seconds"),
            farewell_seconds_before_end=conversation.get("farewell_seconds_before_end"),
            time_warning_message=_selected_control_text(conversation.get("time_warning_message")),
            final_farewell=_selected_control_text(conversation.get("final_farewell")),
            gemini_credential_envelope=gemini_envelope, telnyx_credential_envelope=telnyx_envelope,
            end_call=payload.get("end_call") if isinstance(payload.get("end_call"), dict) else {},
            handoff=payload.get("handoff") if isinstance(payload.get("handoff"), dict) else {},
            capture=payload.get("capture") if isinstance(payload.get("capture"), dict) else {},
        )
    except (KeyError, TypeError, ValueError):
        return None


async def claim_outbound_call(
    campaign_id: int,
    settings: PanelObservationSettings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> OutboundCallClaim | None:
    """Pide exactamente un destino disponible. La concurrencia se valida en PostgreSQL."""
    config = settings or PanelObservationSettings.from_environment()
    if not config.enabled or campaign_id < 1 or not config.base_url.startswith("https://") or not config.shared_secret:
        return None
    started_at = perf_counter()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(config.timeout_seconds), limits=httpx.Limits(max_connections=1, max_keepalive_connections=0), transport=transport, follow_redirects=False) as client:
            response = await client.post(
                f"{config.base_url}/internal/v1/voice/outbound/claim",
                headers={"X-Voice-Service-Key": config.shared_secret}, json={"campaign_id": campaign_id},
            )
    except httpx.HTTPError:
        logger.warning("Outbound no reclamado: reason=request_failed")
        return None
    if response.status_code != 200:
        logger.warning("Outbound no reclamado: reason=http_status status_code=%s", response.status_code)
        return None
    elapsed_ms = (perf_counter() - started_at) * 1000
    try:
        payload = response.json()
        if payload.get("available") is False:
            return None
        campaign = payload["campaign"]
        outbound = payload["outbound"]
        observation = _observation_from_payload(payload, elapsed_ms)
        campaign_id_value, recipient_id = campaign["id"], campaign["recipient_id"]
        to_number, from_number, connection_id = outbound["to_number"], outbound["from_number"], outbound["connection_id"]
        if (type(campaign_id_value) is not int or type(recipient_id) is not int or not all(isinstance(value, str) and value.strip() for value in (to_number, from_number, connection_id)) or observation is None):
            raise ValueError("invalid outbound payload")
        return OutboundCallClaim(campaign_id_value, recipient_id, to_number.strip(), from_number.strip(), connection_id.strip(), observation)
    except (KeyError, TypeError, ValueError):
        logger.warning("Outbound no reclamado: reason=invalid_response")
        return None


async def report_outbound_event(
    *, campaign_id: int, recipient_id: int, result: str,
    settings: PanelObservationSettings | None = None,
) -> bool:
    """Notifica una sola vez el resultado; no reintenta ni ejecuta polling."""
    config = settings or PanelObservationSettings.from_environment()
    if result not in {"completed", "failed"} or campaign_id < 1 or recipient_id < 1 or not config.enabled or not config.base_url.startswith("https://") or not config.shared_secret:
        return False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(config.timeout_seconds), limits=httpx.Limits(max_connections=1, max_keepalive_connections=0), follow_redirects=False) as client:
            response = await client.post(
                f"{config.base_url}/internal/v1/voice/outbound/events",
                headers={"X-Voice-Service-Key": config.shared_secret},
                json={"campaign_id": campaign_id, "recipient_id": recipient_id, "result": result},
            )
        return response.status_code == 200
    except httpx.HTTPError:
        logger.warning("Outbound no confirmado: reason=request_failed")
        return False


async def report_call_event(
    *,
    agent_id: int,
    external_call_id: str,
    call_session_id: str | None,
    direction: str,
    event_type: str,
    from_number: str | None,
    to_number: str | None,
    hangup_cause: str | None = None,
    termination_source: str | None = None,
    termination_reason: str | None = None,
    settings: PanelObservationSettings | None = None,
) -> bool:
    """Envía un único hecho de llamada; no reintenta ni contiene texto conversacional."""
    config = settings or PanelObservationSettings.from_environment()
    allowed_events = {
        "initiated", "answered", "hangup", "failed",
        "transfer_requested", "transfer_dialing", "transfer_bridged", "transfer_failed",
    }
    if (
        agent_id < 1 or event_type not in allowed_events or direction not in {"inbound", "outbound"}
        or not external_call_id.strip() or not config.enabled
        or not config.base_url.startswith("https://") or not config.shared_secret
    ):
        return False
    payload = {
        "agent_id": agent_id,
        "external_call_id": external_call_id,
        "call_session_id": call_session_id,
        "direction": direction,
        "event_type": event_type,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "from_number": from_number,
        "to_number": to_number,
        "hangup_cause": hangup_cause,
        "termination_source": termination_source,
        "termination_reason": termination_reason,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(config.timeout_seconds), limits=httpx.Limits(max_connections=1, max_keepalive_connections=0), follow_redirects=False) as client:
            response = await client.post(
                f"{config.base_url}/internal/v1/voice/calls/events",
                headers={"X-Voice-Service-Key": config.shared_secret}, json=payload,
            )
        return response.status_code == 200
    except httpx.HTTPError:
        logger.warning("Call event no confirmado: event=%s", event_type)
        return False


async def report_call_intake(
    *, agent_id: int, external_call_id: str, name: str | None,
    contact_reason: str, reason_summary: str,
    settings: PanelObservationSettings | None = None,
) -> bool:
    """Envía una única ficha confirmada; nunca registra el contenido sensible en logs."""
    config = settings or PanelObservationSettings.from_environment()
    if (agent_id < 1 or contact_reason not in {"ventas", "cotizacion", "soporte", "cita", "informacion", "otro"}
            or not external_call_id.strip() or not reason_summary.strip() or not config.enabled
            or not config.base_url.startswith("https://") or not config.shared_secret):
        return False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(config.timeout_seconds), limits=httpx.Limits(max_connections=1, max_keepalive_connections=0), follow_redirects=False) as client:
            response = await client.post(
                f"{config.base_url}/internal/v1/voice/calls/intake",
                headers={"X-Voice-Service-Key": config.shared_secret},
                json={"agent_id": agent_id, "external_call_id": external_call_id, "name": name,
                      "contact_reason": contact_reason, "reason_summary": reason_summary, "confirmed": True},
            )
        accepted = response.status_code == 200
        logger.info("Ficha de llamada enviada: status=%s", "accepted" if accepted else "rejected")
        return accepted
    except httpx.HTTPError:
        logger.warning("Ficha de llamada no confirmada")
        return False


async def report_call_followup(
    *, agent_id: int, external_call_id: str, channel: str, caller_number_has_whatsapp: bool | None = None,
    whatsapp_phone: str | None = None, email: str | None = None,
    settings: PanelObservationSettings | None = None,
) -> bool:
    """Registra seguimiento sólo tras consentimiento; no envía WhatsApp ni correo."""
    config = settings or PanelObservationSettings.from_environment()
    if (agent_id < 1 or channel not in {"whatsapp", "email", "advisor"} or not external_call_id.strip()
            or not config.enabled or not config.base_url.startswith("https://") or not config.shared_secret):
        return False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(config.timeout_seconds), limits=httpx.Limits(max_connections=1, max_keepalive_connections=0), follow_redirects=False) as client:
            response = await client.post(
                f"{config.base_url}/internal/v1/voice/calls/followup",
                headers={"X-Voice-Service-Key": config.shared_secret},
                json={"agent_id": agent_id, "external_call_id": external_call_id, "channel": channel, "caller_number_has_whatsapp": caller_number_has_whatsapp,
                      "whatsapp_phone": whatsapp_phone, "email": email, "consent_confirmed": True},
            )
        accepted = response.status_code == 200
        logger.info("Seguimiento autorizado enviado: status=%s", "accepted" if accepted else "rejected")
        return accepted
    except httpx.HTTPError:
        logger.warning("Seguimiento de llamada no confirmado")
        return False
