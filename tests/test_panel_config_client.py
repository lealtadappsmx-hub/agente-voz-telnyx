import asyncio
import logging
from pathlib import Path

import httpx

from panel_config_client import PanelObservationSettings, observe_panel_agent


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_SECRET = "test-shared-secret-never-production"
TEST_PHONE = "+526682680350"


def settings(**overrides):
    values = {
        "enabled": True,
        "base_url": "https://panel.example.test",
        "shared_secret": TEST_SECRET,
        "fallback_enabled": True,
        "timeout_seconds": 3.0,
    }
    values.update(overrides)
    return PanelObservationSettings(**values)


def test_disabled_mode_does_not_make_request():
    async def handler(_request):
        raise AssertionError("No debe existir una llamada externa cuando está desactivado")

    result = asyncio.run(
        observe_panel_agent(
            TEST_PHONE,
            settings=settings(enabled=False),
            transport=httpx.MockTransport(handler),
        )
    )
    assert result is None


def test_success_returns_prompt_without_logging_it(caplog):
    captured = {}

    async def handler(request):
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["secret"] = request.headers["X-Voice-Service-Key"]
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={
                "api_version": "1",
                "agent": {
                    "id": 1,
                    "client_id": 1,
                    "name": "Luisa\n",
                    "system_prompt": "PROMPT-MUY-SENSIBLE-Y-COMPLETO",
                    "voice_name": "Leda",
                    "thinking_level": "low",
                },
                "credentials_status": {"gemini_configured": True, "telnyx_configured": True},
                "runtime": {
                    "gemini_credential_envelope": "encrypted-gemini-envelope-for-test",
                    "telnyx_credential_envelope": "encrypted-telnyx-envelope-for-test",
                },
                "conversation": {
                    "max_call_seconds": 300,
                    "farewell_seconds_before_end": 20,
                    "time_warning_message": "Aviso privado de tiempo.",
                    "final_farewell": "Despedida privada.",
                },
            },
        )

    caplog.set_level(logging.INFO, logger="uvicorn.error")
    result = asyncio.run(
        observe_panel_agent(TEST_PHONE, settings=settings(), transport=httpx.MockTransport(handler))
    )

    assert result is not None
    assert (result.agent_id, result.client_id, result.agent_name) == (1, 1, "Luisa")
    assert result.system_prompt == "PROMPT-MUY-SENSIBLE-Y-COMPLETO"
    assert result.voice_name == "Leda"
    assert result.thinking_level == "low"
    assert result.max_call_seconds == 300
    assert result.farewell_seconds_before_end == 20
    assert result.time_warning_message == "Aviso privado de tiempo."
    assert result.final_farewell == "Despedida privada."
    assert result.gemini_credential_envelope == "encrypted-gemini-envelope-for-test"
    assert result.telnyx_credential_envelope == "encrypted-telnyx-envelope-for-test"
    assert "PROMPT-MUY-SENSIBLE-Y-COMPLETO" not in repr(result)
    assert captured["method"] == "POST"
    assert captured["path"] == "/internal/v1/voice/resolve-agent"
    assert captured["secret"] == TEST_SECRET
    assert b'"direction":"inbound"' in captured["body"]
    assert "Panel resuelto: agent_id=1 client_id=1 agent_name=Luisa prompt=ready" in caplog.text
    assert TEST_SECRET not in caplog.text
    assert TEST_PHONE not in caplog.text
    assert "PROMPT-MUY-SENSIBLE-Y-COMPLETO" not in caplog.text
    assert "Leda" not in caplog.text


def test_invalid_runtime_credentials_are_ignored_without_rejecting_the_prompt():
    async def handler(_request):
        return httpx.Response(
            200,
            json={
                "agent": {
                    "id": 1,
                    "client_id": 1,
                    "name": "Luisa",
                    "system_prompt": "Prompt válido suficientemente largo para conservar la llamada activa.",
                },
                "runtime": {
                    "gemini_credential_envelope": 42,
                    "telnyx_credential_envelope": [],
                },
            },
        )

    result = asyncio.run(
        observe_panel_agent(TEST_PHONE, settings=settings(), transport=httpx.MockTransport(handler))
    )

    assert result is not None
    assert result.gemini_credential_envelope is None
    assert result.telnyx_credential_envelope is None


def test_http_error_is_controlled_without_sensitive_logs(caplog):
    async def handler(_request):
        return httpx.Response(401, json={"error": {"message": TEST_SECRET}})

    caplog.set_level(logging.INFO, logger="uvicorn.error")
    result = asyncio.run(
        observe_panel_agent(TEST_PHONE, settings=settings(), transport=httpx.MockTransport(handler))
    )
    assert result is None
    assert "status_code=401" in caplog.text
    assert TEST_SECRET not in caplog.text
    assert TEST_PHONE not in caplog.text


def test_network_failure_is_fallback_only(caplog):
    async def handler(request):
        raise httpx.ConnectTimeout("simulated timeout", request=request)

    caplog.set_level(logging.INFO, logger="uvicorn.error")
    result = asyncio.run(
        observe_panel_agent(TEST_PHONE, settings=settings(), transport=httpx.MockTransport(handler))
    )
    assert result is None
    assert "reason=request_failed" in caplog.text
    assert TEST_SECRET not in caplog.text


def test_invalid_response_is_ignored():
    async def handler(_request):
        return httpx.Response(200, json={"agent": {"id": "1", "client_id": 1, "name": "Luisa"}})

    result = asyncio.run(
        observe_panel_agent(TEST_PHONE, settings=settings(), transport=httpx.MockTransport(handler))
    )
    assert result is None


def test_short_or_missing_prompt_is_rejected_without_exposure(caplog):
    async def handler(_request):
        return httpx.Response(
            200,
            json={"agent": {"id": 1, "client_id": 1, "name": "Luisa", "system_prompt": "corto"}},
        )

    result = asyncio.run(
        observe_panel_agent(TEST_PHONE, settings=settings(), transport=httpx.MockTransport(handler))
    )
    assert result is None
    assert "corto" not in caplog.text


def test_invalid_live_values_use_safe_defaults_without_rejecting_the_prompt():
    async def handler(_request):
        return httpx.Response(
            200,
            json={
                "agent": {
                    "id": 1,
                    "client_id": 1,
                    "name": "Luisa",
                    "system_prompt": "Prompt válido suficientemente largo para conservar la llamada activa.",
                    "voice_name": "voz-no-autorizada",
                    "thinking_level": "máximo",
                }
            },
        )

    result = asyncio.run(
        observe_panel_agent(TEST_PHONE, settings=settings(), transport=httpx.MockTransport(handler))
    )
    assert result is not None
    assert result.voice_name == "Kore"
    assert result.thinking_level == "minimal"


def test_protected_audio_pipeline_remains_present():
    source = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
    for protected_fragment in (
        "audioop.ulaw2lin",
        "audioop.ratecv",
        "audioop.lin2ulaw",
        "tamano_paquete = 160",
        '@app.websocket("/media")',
        "cliente_gemini.aio.live.connect",
        '"system_instruction": (\n                system_prompt',
        "recibir_inicio_telnyx",
        "cancelar_temporizador_llamada",
        "select_call_duration_settings",
        "request_closure_message",
        "closure_turn_finished",
    ):
        assert protected_fragment in source
