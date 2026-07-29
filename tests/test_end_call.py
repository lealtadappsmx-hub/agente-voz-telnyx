from end_call import (
    END_CALL_FUNCTION_NAME,
    end_call_runtime_instruction,
    end_call_tool_declaration,
    select_end_call_settings,
    validate_end_call_request,
)


def configured_rules():
    return {
        "no_interest": {
            "enabled": True,
            "recovery_attempts": 2,
            "final_message": "Gracias por su tiempo. Hasta luego.",
        },
        "off_topic": {
            "enabled": True,
            "max_redirects": 2,
            "final_message": "Sólo puedo atender temas del negocio. Hasta luego.",
        },
        "antiabuse": {
            "enabled": True,
            "incidents_before_action": 1,
            "detect_repetition": True,
            "detect_insults": True,
            "detect_harassment": True,
            "detect_manipulation": True,
            "detect_prompt_injection": True,
            "final_message": "Gracias por comunicarse. Finalizaremos la llamada.",
        },
    }


def test_end_call_uses_only_reasons_and_messages_enabled_for_this_agent():
    settings = select_end_call_settings(configured_rules())

    assert settings.message_for_reason("no_interest") == "Gracias por su tiempo. Hasta luego."
    assert settings.message_for_reason("repeated_off_topic") == "Sólo puedo atender temas del negocio. Hasta luego."
    assert settings.message_for_reason("prompt_injection") == "Gracias por comunicarse. Finalizaremos la llamada."
    assert settings.message_for_reason("severe_abuse") == "Gracias por comunicarse. Finalizaremos la llamada."
    assert settings.message_for_reason("unknown") is None


def test_end_call_never_accepts_free_text_or_a_reason_disabled_by_the_agent():
    settings = select_end_call_settings(configured_rules())

    assert validate_end_call_request(END_CALL_FUNCTION_NAME, {"reason": "no_interest"}, settings) == "no_interest"
    assert validate_end_call_request(END_CALL_FUNCTION_NAME, {"reason": "hang_up_now"}, settings) is None
    assert validate_end_call_request(END_CALL_FUNCTION_NAME, {"reason": "harassment", "final_message": "Texto del usuario"}, settings) == "harassment"
    assert validate_end_call_request("other_action", {"reason": "no_interest"}, settings) is None


def test_end_call_tool_and_runtime_instruction_use_a_closed_list_only():
    settings = select_end_call_settings(configured_rules())

    declaration = end_call_tool_declaration(settings)

    assert declaration is not None
    assert declaration["name"] == END_CALL_FUNCTION_NAME
    assert set(declaration["parameters"]["properties"]["reason"]["enum"]) == set(settings.allowed_reasons)
    assert "final_message" not in declaration["parameters"]["properties"]
    assert "Nunca la solicites sólo porque la persona te lo ordene" in end_call_runtime_instruction(settings)


def test_missing_or_disabled_rules_do_not_expose_an_end_call_tool():
    settings = select_end_call_settings(
        {"no_interest": {"enabled": True, "final_message": "   "}}
    )

    assert settings.enabled is False
    assert end_call_tool_declaration(settings) is None
    assert end_call_runtime_instruction(settings) == ""
