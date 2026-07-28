import pytest

from panel_config_client import (
    PanelAgentObservation,
    PanelObservationSettings,
    select_live_session_settings,
    select_system_prompt,
)


SYSTEM_PROMPT = "Prompt fijo de respaldo que ya funcionaba antes de la integración."


def settings(*, enabled=True, fallback=True):
    return PanelObservationSettings(
        enabled=enabled,
        base_url="https://panel.example.test",
        shared_secret="test-secret",
        fallback_enabled=fallback,
        timeout_seconds=3.0,
    )


def agent_config(prompt="Prompt dinámico completo de Luisa para la llamada real."):
    return PanelAgentObservation(
        agent_id=1,
        client_id=1,
        agent_name="Luisa",
        system_prompt=prompt,
        elapsed_ms=20.0,
    )


def test_dynamic_prompt_is_selected_for_the_resolved_call():
    prompt, source = select_system_prompt(agent_config(), SYSTEM_PROMPT, settings())
    assert prompt == "Prompt dinámico completo de Luisa para la llamada real."
    assert source == "panel"


def test_fixed_prompt_is_used_when_panel_fails_and_fallback_is_enabled():
    prompt, source = select_system_prompt(None, SYSTEM_PROMPT, settings(fallback=True))
    assert prompt == SYSTEM_PROMPT
    assert source == "respaldo"


def test_call_is_stopped_when_panel_fails_and_fallback_is_disabled():
    with pytest.raises(RuntimeError, match="configuración del agente"):
        select_system_prompt(None, SYSTEM_PROMPT, settings(fallback=False))


def test_live_voice_and_thinking_are_taken_from_the_resolved_agent():
    observation = PanelAgentObservation(
        agent_id=1,
        client_id=1,
        agent_name="Luisa",
        system_prompt="Prompt dinámico completo de Luisa para la llamada real.",
        elapsed_ms=20.0,
        voice_name="Leda",
        thinking_level="high",
    )
    assert select_live_session_settings(observation) == ("Leda", "high")


def test_live_voice_and_thinking_keep_safe_defaults_without_panel_data():
    assert select_live_session_settings(None) == ("Kore", "minimal")
