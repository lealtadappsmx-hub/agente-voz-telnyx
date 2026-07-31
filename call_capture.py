"""Acciones cerradas para la ficha de atención y seguimiento autorizado."""

from __future__ import annotations

import re
from dataclasses import dataclass


SAVE_CALL_INTAKE_ACTION = "SAVE_CALL_INTAKE"
SAVE_CALL_FOLLOWUP_ACTION = "SAVE_CALL_FOLLOWUP"
SAVE_CALL_INTAKE_FUNCTION = "save_call_intake"
SAVE_CALL_FOLLOWUP_FUNCTION = "save_call_followup"
_REASONS = frozenset({"ventas", "cotizacion", "soporte", "cita", "informacion", "otro"})
_PHONE = re.compile(r"^\+[1-9][0-9]{7,14}$")
_EMAIL = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,255}\.[^\s@]{2,63}$")


def _text(value: object, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned if cleaned and len(cleaned) <= maximum else None


@dataclass(frozen=True)
class CaptureSettings:
    intake_enabled: bool = False
    capture_name: bool = False
    capture_reason: bool = False
    followup_enabled: bool = False
    allow_whatsapp: bool = False
    allow_email: bool = False


def select_capture_settings(value: object) -> CaptureSettings:
    if not isinstance(value, dict):
        return CaptureSettings()
    return CaptureSettings(
        intake_enabled=value.get("intake_enabled") is True,
        capture_name=value.get("capture_name") is True,
        capture_reason=value.get("capture_reason") is True,
        followup_enabled=value.get("followup_enabled") is True,
        allow_whatsapp=value.get("allow_whatsapp") is True,
        allow_email=value.get("allow_email") is True,
    )


def intake_tool_declaration(settings: CaptureSettings) -> dict[str, object] | None:
    if not settings.intake_enabled:
        return None
    return {
        "name": SAVE_CALL_INTAKE_FUNCTION,
        "description": (
            "Guarda la ficha breve de la llamada una sola vez, después de que la persona haya "
            "confirmado naturalmente el nombre y el resumen del asunto. No menciones esta acción."
        ),
        "parameters": {"type": "OBJECT", "properties": {
            "name": {"type": "STRING", "description": "Nombre dicho por la persona; omítelo si no lo dijo."},
            "contact_reason": {"type": "STRING", "enum": sorted(_REASONS)},
            "reason_summary": {"type": "STRING", "description": "Resumen factual breve, máximo 500 caracteres."},
        }, "required": ["contact_reason", "reason_summary"]},
    }


def followup_tool_declaration(settings: CaptureSettings) -> dict[str, object] | None:
    if not settings.followup_enabled:
        return None
    return {
        "name": SAVE_CALL_FOLLOWUP_FUNCTION,
        "description": (
            "Registra contacto de seguimiento sólo tras pedir autorización explícita y recibir una "
            "respuesta afirmativa. No envía mensajes ni correos. Para WhatsApp confirma el número."
        ),
        "parameters": {"type": "OBJECT", "properties": {
            "channel": {"type": "STRING", "enum": ["whatsapp", "email", "advisor"]},
            "caller_number_has_whatsapp": {"type": "BOOLEAN", "description": "Sólo tras preguntarlo y recibir respuesta clara."},
            "whatsapp_phone": {"type": "STRING"},
            "email": {"type": "STRING"},
        }, "required": ["channel"]},
    }


def capture_runtime_instruction(settings: CaptureSettings) -> str:
    if not settings.intake_enabled and not settings.followup_enabled:
        return ""
    lines = ["\n\n# Ficha de atención", "- Durante la conversación identifica con naturalidad el nombre y qué necesita la persona. No preguntes si autorizan guardar la ficha."]
    if settings.intake_enabled:
        lines.append("- Cuando nombre y necesidad estén claros, confirma en lenguaje natural un resumen breve y solicita SAVE_CALL_INTAKE sólo después de esa confirmación. Clasifica el motivo en ventas, cotizacion, soporte, cita, informacion u otro.")
    if settings.followup_enabled:
        channels = []
        if settings.allow_whatsapp:
            channels.append("WhatsApp")
        if settings.allow_email:
            channels.append("correo")
        lines.append("- Sólo si la persona pide asesor, transferencia o seguimiento" + (" por " + " o ".join(channels) if channels else "") + ", pide autorización explícita para registrar el medio de contacto. Confirma el número de WhatsApp o correo antes de solicitar SAVE_CALL_FOLLOWUP. No prometas ni simules un envío.")
    return "\n".join(lines)


def validate_intake_request(name: object, arguments: object, settings: CaptureSettings) -> dict[str, str | None] | None:
    if name != SAVE_CALL_INTAKE_FUNCTION or not isinstance(arguments, dict) or not settings.intake_enabled:
        return None
    reason = arguments.get("contact_reason")
    summary = _text(arguments.get("reason_summary"), 500)
    customer_name = _text(arguments.get("name"), 120) if settings.capture_name else None
    if reason not in _REASONS or not summary:
        return None
    return {"name": customer_name, "contact_reason": reason, "reason_summary": summary}


def validate_followup_request(name: object, arguments: object, settings: CaptureSettings) -> dict[str, str | None] | None:
    if name != SAVE_CALL_FOLLOWUP_FUNCTION or not isinstance(arguments, dict) or not settings.followup_enabled:
        return None
    channel = arguments.get("channel")
    phone = _text(arguments.get("whatsapp_phone"), 20)
    email = _text(arguments.get("email"), 320)
    confirmed = arguments.get("caller_number_has_whatsapp") if type(arguments.get("caller_number_has_whatsapp")) is bool else None
    if channel == "whatsapp" and settings.allow_whatsapp and ((phone and _PHONE.fullmatch(phone)) or confirmed is True):
        return {"channel": channel, "caller_number_has_whatsapp": confirmed, "whatsapp_phone": phone if phone and _PHONE.fullmatch(phone) else None, "email": None}
    if channel == "email" and settings.allow_email and email and _EMAIL.fullmatch(email.lower()):
        return {"channel": channel, "caller_number_has_whatsapp": confirmed, "whatsapp_phone": None, "email": email.lower()}
    if channel == "advisor":
        return {"channel": channel, "caller_number_has_whatsapp": confirmed, "whatsapp_phone": None, "email": None}
    return None
