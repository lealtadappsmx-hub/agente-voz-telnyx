"""Descifrado en memoria de una credencial Gemini por llamada.

El panel conserva la clave del negocio cifrada en PostgreSQL. Para una llamada
la entrega dentro de un sobre Fernet autenticado con el secreto compartido del
servicio de voz. El sobre sólo vive en el contexto efímero de esa llamada.
"""

from __future__ import annotations

import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken


RUNTIME_CREDENTIAL_VERSION = 1
MAX_ENVELOPE_LENGTH = 10_000
MAX_GEMINI_API_KEY_LENGTH = 2_048


def _transport_fernet(shared_secret: str) -> Fernet:
    if not isinstance(shared_secret, str) or not shared_secret.strip():
        raise ValueError("missing shared secret")
    key = base64.urlsafe_b64encode(hashlib.sha256(shared_secret.encode()).digest())
    return Fernet(key)


def decrypt_gemini_credential_for_voice(
    *,
    envelope: str,
    agent_id: int,
    client_id: int,
    shared_secret: str,
) -> str:
    """Devuelve una clave sólo si el sobre pertenece a esta llamada/agente."""
    if (
        not isinstance(envelope, str)
        or not envelope.strip()
        or len(envelope) > MAX_ENVELOPE_LENGTH
        or type(agent_id) is not int
        or type(client_id) is not int
    ):
        raise ValueError("invalid runtime credential")

    try:
        raw_payload = _transport_fernet(shared_secret).decrypt(envelope.encode())
        payload = json.loads(raw_payload)
    except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        raise ValueError("invalid runtime credential") from None

    if (
        not isinstance(payload, dict)
        or payload.get("version") != RUNTIME_CREDENTIAL_VERSION
        or payload.get("agent_id") != agent_id
        or payload.get("client_id") != client_id
    ):
        raise ValueError("invalid runtime credential")

    gemini_api_key = payload.get("gemini_api_key")
    if not isinstance(gemini_api_key, str):
        raise ValueError("invalid runtime credential")
    cleaned_key = gemini_api_key.strip()
    if not cleaned_key or len(cleaned_key) > MAX_GEMINI_API_KEY_LENGTH:
        raise ValueError("invalid runtime credential")
    return cleaned_key
