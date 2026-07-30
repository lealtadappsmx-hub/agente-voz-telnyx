from handoff import (
    TRANSFER_CALL_FUNCTION_NAME,
    select_handoff_settings,
    transfer_call_runtime_instruction,
    transfer_call_tool_declaration,
    validate_transfer_call_request,
)


def configured_handoff():
    return {
        "enabled": True,
        "destination": "+526688000001",
        "timeout_seconds": 25,
        "announcement": "Un momento, le comunicaré con un asesor.",
        "failure_mode": "return_to_agent",
        "failure_message": "No fue posible comunicarle. Con gusto sigo atendiendo.",
    }


def test_handoff_accepts_only_panel_configured_destination_and_no_arguments():
    settings = select_handoff_settings(configured_handoff())
    assert settings.enabled is True
    tool = transfer_call_tool_declaration(settings)
    assert tool and tool["name"] == TRANSFER_CALL_FUNCTION_NAME
    assert validate_transfer_call_request(TRANSFER_CALL_FUNCTION_NAME, {}, settings) is True
    assert validate_transfer_call_request(TRANSFER_CALL_FUNCTION_NAME, {"to": "+526688999999"}, settings) is True
    assert "+526688000001" not in repr(settings)
    assert "TRANSFER_CALL" in transfer_call_runtime_instruction(settings)


def test_handoff_rejects_invalid_destination_or_missing_failure_message():
    invalid_destination = {**configured_handoff(), "destination": "numero libre"}
    missing_message = {**configured_handoff(), "failure_message": ""}
    assert select_handoff_settings(invalid_destination).enabled is False
    assert select_handoff_settings(missing_message).enabled is False
