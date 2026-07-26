"""Cliente de configuración dinámica del agente resuelto por el panel."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from time import perf_counter

import httpx


# Uvicorn ya configura este logger en INFO dentro del contenedor.
logger = logging.getLogger("uvicorn.error")


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
    except (KeyError, TypeError, ValueError):
        logger.warning("Panel no observado: reason=invalid_response elapsed_ms=%.2f", elapsed_ms)
        return None

    observation = PanelAgentObservation(
        agent_id=agent_id,
        client_id=client_id,
        agent_name=safe_name,
        system_prompt=safe_prompt,
        elapsed_ms=elapsed_ms,
    )
    logger.info(
        "Panel resuelto: agent_id=%s client_id=%s agent_name=%s prompt=ready elapsed_ms=%.2f",
        observation.agent_id,
        observation.client_id,
        observation.agent_name,
        observation.elapsed_ms,
    )
    return observation
