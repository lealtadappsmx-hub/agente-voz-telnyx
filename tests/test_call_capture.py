from call_capture import (
    SAVE_CALL_FOLLOWUP_FUNCTION,
    SAVE_CALL_INTAKE_FUNCTION,
    capture_runtime_instruction,
    intake_tool_declaration,
    select_capture_settings,
    validate_followup_request,
    validate_intake_request,
)
from panel_config_client import _capture_response_status


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


def test_intake_requires_closed_reason_and_short_summary():
    settings = select_capture_settings({"intake_enabled": True, "capture_name": True})
    assert validate_intake_request(SAVE_CALL_INTAKE_FUNCTION, {
        "name": "  Ana  López ", "contact_reason": "cotizacion", "reason_summary": "Necesita una propuesta para su negocio."
    }, settings) == {"name": "Ana López", "contact_reason": "cotizacion", "reason_summary": "Necesita una propuesta para su negocio."}
    assert validate_intake_request(SAVE_CALL_INTAKE_FUNCTION, {"contact_reason": "libre", "reason_summary": "x"}, settings) is None


def test_followup_requires_configured_channel_and_valid_contact():
    settings = select_capture_settings({"followup_enabled": True, "allow_whatsapp": True})
    assert validate_followup_request(SAVE_CALL_FOLLOWUP_FUNCTION, {
        "channel": "whatsapp", "whatsapp_phone": "+526688000000"
    }, settings) == {"channel": "whatsapp", "caller_number_has_whatsapp": None, "whatsapp_phone": "+526688000000", "email": None}
    assert validate_followup_request(SAVE_CALL_FOLLOWUP_FUNCTION, {
        "channel": "whatsapp", "caller_number_has_whatsapp": True
    }, settings) == {"channel": "whatsapp", "caller_number_has_whatsapp": True, "whatsapp_phone": None, "email": None}
    assert validate_followup_request(SAVE_CALL_FOLLOWUP_FUNCTION, {"channel": "whatsapp"}, settings) is None
    assert validate_followup_request(SAVE_CALL_FOLLOWUP_FUNCTION, {
        "channel": "email", "email": "persona@ejemplo.com"
    }, settings) is None


def test_intake_instruction_requires_immediate_capture_without_authorization():
    settings = select_capture_settings({
        "intake_enabled": True,
        "capture_name": True,
        "capture_reason": True,
    })
    declaration = intake_tool_declaration(settings)
    instruction = capture_runtime_instruction(settings)

    assert declaration is not None
    assert "OBLIGATORIA" in declaration["description"]
    assert "antes de hacer otra pregunta" in instruction
    assert "No esperes una confirmación extra" in instruction


def test_capture_status_exposes_only_safe_diagnostic_category():
    assert _capture_response_status(_Response(200)) == "accepted"
    assert _capture_response_status(_Response(401)) == "unauthorized"
    assert _capture_response_status(_Response(404)) == "call_not_found"
    assert _capture_response_status(_Response(409)) == "capture_disabled_or_prerequisite_missing"
    assert _capture_response_status(_Response(503)) == "panel_database_unavailable"
