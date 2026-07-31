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
    if not settings.intake_enabled or not settings.capture_reason:
        return None
    return {
        "name": SAVE_CALL_INTAKE_FUNCTION,
        "description": (
            "ACCIÓN OBLIGATORIA. Llámala exactamente una vez tan pronto como la persona "
            "explique qué necesita, antes de continuar la conversación. Guarda una ficha breve "
            "con el nombre si lo dijo, categoría y resumen factual. No pidas autorización ni "
            "confirmación adicional para esta ficha y nunca menciones esta acción."
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
            "ACCIÓN OBLIGATORIA sólo tras pedir autorización explícita y recibir una respuesta "
            "afirmativa para WhatsApp, correo o asesor. Primero debe haberse llamado "
            "save_call_intake. No envía mensajes ni correos. Para WhatsApp confirma el número."
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
    lines = [
        "\n\n# Registro obligatorio de la atención",
        "- Durante la conversación identifica con naturalidad el nombre y qué necesita la persona. No preguntes si autorizan guardar la ficha ni menciones herramientas, base de datos o registros.",
    ]
    if settings.intake_enabled and settings.capture_reason:
        lines.append(
            "- OBLIGATORIO: en cuanto la persona explique su necesidad, llama SAVE_CALL_INTAKE "
            "antes de hacer otra pregunta o cambiar de tema. No esperes una confirmación extra: "
            "lo que la persona acaba de decir basta. Incluye el nombre sólo si ya lo dijo; si no, "
            "guarda la necesidad y continúa la conversación. Clasifica en ventas, cotizacion, "
            "soporte, cita, informacion u otro. Si la persona corrige o aclara el motivo, vuelve "
            "a llamar la acción para actualizar la misma ficha."
        )
    if settings.followup_enabled:
        channels = []
        if settings.allow_whatsapp:
            channels.append("WhatsApp")
        if settings.allow_email:
            channels.append("correo")
        lines.append(
            "- Sólo si la persona pide asesor, transferencia o seguimiento"
            + (" por " + " o ".join(channels) if channels else "")
            + ", pide autorización explícita para registrar el medio de contacto. Después de una "
            "respuesta afirmativa y de confirmar el número de WhatsApp o correo, llama "
            "SAVE_CALL_FOLLOWUP inmediatamente. Antes debe existir SAVE_CALL_INTAKE. No prometas "
            "ni simules un envío."
        )
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
