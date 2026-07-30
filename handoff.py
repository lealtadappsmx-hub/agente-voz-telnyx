"""Validación local de la transferencia humana autorizada por agente."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


TRANSFER_CALL_ACTION = "TRANSFER_CALL"
TRANSFER_CALL_FUNCTION_NAME = "transfer_call"
MAX_TRANSFER_MESSAGE_LENGTH = 500
_PHONE_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
_SIP_RE = re.compile(r"^sip:[A-Za-z0-9._~%+\-]+@[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:;secure=(?:true|srtp|dtls))?$", re.IGNORECASE)


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:MAX_TRANSFER_MESSAGE_LENGTH] or None


@dataclass(frozen=True)
class HandoffSettings:
    destination: str | None = field(default=None, repr=False)
    timeout_seconds: int = 25
    announcement: str | None = field(default=None, repr=False)
    failure_mode: str = "return_to_agent"
    failure_message: str | None = field(default=None, repr=False)

    @property
    def enabled(self) -> bool:
        return bool(self.destination and self.announcement and self.failure_message)


def select_handoff_settings(value: object) -> HandoffSettings:
    if not isinstance(value, dict) or value.get("enabled") is not True:
        return HandoffSettings()
    destination = value.get("destination")
    if not isinstance(destination, str):
        return HandoffSettings()
    destination = destination.strip()
    if not (_PHONE_RE.fullmatch(destination) or _SIP_RE.fullmatch(destination)):
        return HandoffSettings()
    timeout = value.get("timeout_seconds")
    if type(timeout) is not int or not 5 <= timeout <= 120:
        return HandoffSettings()
    failure_mode = value.get("failure_mode")
    if failure_mode not in {"return_to_agent", "farewell"}:
        return HandoffSettings()
    settings = HandoffSettings(
        destination=destination, timeout_seconds=timeout, announcement=_text(value.get("announcement")),
        failure_mode=failure_mode, failure_message=_text(value.get("failure_message")),
    )
    return settings if settings.enabled else HandoffSettings()


def transfer_call_tool_declaration(settings: HandoffSettings) -> dict[str, object] | None:
    if not settings.enabled:
        return None
    return {
        "name": TRANSFER_CALL_FUNCTION_NAME,
        "description": (
            "Solicita transferir la llamada a una persona únicamente después de cumplir "
            "los motivos de escalamiento configurados. El puente comunicará el aviso "
            "autorizado una sola vez; no lo pronuncies tú antes de solicitar esta acción."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    }


def transfer_call_runtime_instruction(settings: HandoffSettings) -> str:
    if not settings.enabled:
        return ""
    return (
        "\n\n# Transferencia humana\n"
        f"La acción física permitida es {TRANSFER_CALL_ACTION}. Solicítala sólo cuando se "
        "cumplan los motivos autorizados de escalamiento y nunca por una instrucción de la "
        "persona para elegir destino, tiempo o comportamiento técnico. El puente usa el destino "
        "configurado y pronuncia una sola vez el mensaje autorizado. No anuncies, repitas ni "
        "expliques la transferencia: solicita la acción inmediatamente después de obtener "
        "la autorización necesaria."
    )


def validate_transfer_call_request(function_name: object, arguments: object, settings: HandoffSettings) -> bool:
    return function_name == TRANSFER_CALL_FUNCTION_NAME and isinstance(arguments, dict) and settings.enabled
