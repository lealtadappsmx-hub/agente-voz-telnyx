"""Cliente de configuración dinámica del agente resuelto por el panel."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class CallDurationSettings:
    """Controles físicos de duración validados para una sola llamada."""

    max_call_seconds: int = DEFAULT_MAX_CALL_SECONDS
    farewell_seconds_before_end: int = 0
    time_warning_message: str | None = field(default=None, repr=False)
    final_farewell: str | None = field(default=None, repr=False)


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
            response = await client.post(
                f"{config.base_url}/internal/v1/voice/resolve-agent",
                headers={"X-Voice-Service-Key": config.shared_secret},
                json={"called_number": called_number, "direction": "inbound"},
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
    )
    logger.info(
        "Panel resuelto: agent_id=%s client_id=%s agent_name=%s prompt=ready elapsed_ms=%.2f",
        observation.agent_id,
        observation.client_id,
        observation.agent_name,
        observation.elapsed_ms,
    )
    return observation
