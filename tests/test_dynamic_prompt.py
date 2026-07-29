import pytest
import base64
import hashlib
import json

from cryptography.fernet import Fernet

from call_duration import closing_deadlines
from panel_config_client import (
    PanelAgentObservation,
    PanelObservationSettings,
    select_call_duration_settings,
    select_live_session_settings,
    select_system_prompt,
)
from gemini_key_selector import select_gemini_api_key


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


def test_call_duration_uses_valid_controls_from_the_resolved_agent():
    observation = PanelAgentObservation(
        agent_id=1,
        client_id=1,
        agent_name="Luisa",
        system_prompt="Prompt dinámico completo de Luisa para la llamada real.",
        elapsed_ms=20.0,
        max_call_seconds=300,
        farewell_seconds_before_end=20,
        time_warning_message="Quedan pocos segundos para cerrar la llamada.",
        final_farewell="Gracias por llamar. Hasta luego.",
    )

    selected = select_call_duration_settings(observation)

    assert selected.max_call_seconds == 300
    assert selected.farewell_seconds_before_end == 20
    assert selected.time_warning_message == "Quedan pocos segundos para cerrar la llamada."
    assert selected.final_farewell == "Gracias por llamar. Hasta luego."


def test_call_duration_keeps_fixed_safe_fallback_for_invalid_or_missing_panel_values():
    assert select_call_duration_settings(None).max_call_seconds == 180

    invalid = PanelAgentObservation(
        agent_id=1,
        client_id=1,
        agent_name="Luisa",
        system_prompt="Prompt dinámico completo de Luisa para la llamada real.",
        elapsed_ms=20.0,
        max_call_seconds=10,
        farewell_seconds_before_end=20,
        time_warning_message="No debe aplicarse.",
        final_farewell="No debe aplicarse.",
    )

    selected = select_call_duration_settings(invalid)
    assert selected.max_call_seconds == 180
    assert selected.farewell_seconds_before_end == 0
    assert selected.time_warning_message is None
    assert selected.final_farewell is None


def test_final_farewell_starts_at_the_configured_limit_when_there_is_no_warning():
    """El margen previo no debe adelantar una despedida final por sí solo."""
    warning_start, final_farewell_start = closing_deadlines(
        answered_at=0,
        max_call_seconds=90,
        farewell_seconds_before_end=20,
        has_time_warning=False,
    )

    assert warning_start is None
    assert final_farewell_start == 90


def _runtime_envelope(secret: str, *, agent_id: int, client_id: int, key: str) -> str:
    transport_key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(transport_key).encrypt(
        json.dumps(
            {
                "version": 1,
                "agent_id": agent_id,
                "client_id": client_id,
                "gemini_api_key": key,
            },
            separators=(",", ":"),
        ).encode()
    ).decode()


def test_client_gemini_key_is_required_for_the_resolved_call():
    secret = "runtime-secret-for-selection"
    observation = agent_config()
    observation = PanelAgentObservation(
        **{
            **observation.__dict__,
            "gemini_credential_envelope": _runtime_envelope(
                secret, agent_id=1, client_id=1, key="client-gemini-key"
            ),
        }
    )
    key = select_gemini_api_key(
        observation,
        shared_secret=secret,
    )

    assert key == "client-gemini-key"


def test_missing_client_gemini_key_rejects_the_call_without_a_global_fallback():
    with pytest.raises(RuntimeError, match="credencial Gemini válida"):
        select_gemini_api_key(
            None,
            shared_secret="test-secret",
        )
