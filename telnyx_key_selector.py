"""Selección segura de la credencial Telnyx del negocio por llamada."""

from __future__ import annotations

import logging

from panel_config_client import PanelAgentObservation
from runtime_credential import decrypt_telnyx_credential_for_voice


logger = logging.getLogger("uvicorn.error")


def select_telnyx_api_key(
    agent_config: PanelAgentObservation,
    *,
    shared_secret: str,
) -> str:
    """Descifra la clave del negocio en memoria; nunca usa una clave global."""
    envelope = agent_config.telnyx_credential_envelope
    if not envelope:
        raise RuntimeError("El negocio no tiene una credencial Telnyx disponible.")
    try:
        selected_key = decrypt_telnyx_credential_for_voice(
            envelope=envelope,
            agent_id=agent_config.agent_id,
            client_id=agent_config.client_id,
            shared_secret=shared_secret,
        )
    except ValueError:
        logger.warning(
            "Credencial Telnyx del negocio no disponible: agent_id=%s client_id=%s",
            agent_config.agent_id,
            agent_config.client_id,
        )
        raise RuntimeError("La credencial Telnyx del negocio no es válida.") from None
    logger.info(
        "Credencial Telnyx preparada: source=negocio agent_id=%s client_id=%s",
        agent_config.agent_id,
        agent_config.client_id,
    )
    return selected_key
