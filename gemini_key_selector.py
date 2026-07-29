"""Selección acotada de la clave Gemini para una sola llamada."""

from __future__ import annotations

import logging

from panel_config_client import PanelAgentObservation
from runtime_credential import decrypt_gemini_credential_for_voice


logger = logging.getLogger("uvicorn.error")


def select_gemini_api_key(
    agent_config: PanelAgentObservation | None,
    *,
    shared_secret: str,
) -> str:
    """Exige la clave cifrada del negocio para esta llamada concreta."""
    envelope = agent_config.gemini_credential_envelope if agent_config else None
    if envelope and agent_config:
        try:
            return decrypt_gemini_credential_for_voice(
                envelope=envelope,
                agent_id=agent_config.agent_id,
                client_id=agent_config.client_id,
                shared_secret=shared_secret,
            )
        except ValueError:
            logger.warning("Credencial Gemini del negocio no disponible; la llamada no iniciará Gemini.")

    raise RuntimeError("No hay una credencial Gemini válida para el negocio de esta llamada.")
