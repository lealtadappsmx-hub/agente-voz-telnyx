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


def test_success_observes_only_safe_metadata(caplog):
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
                    "system_prompt": "PROMPT-MUY-SENSIBLE",
                    "voice_name": "Leda",
                },
                "credentials_status": {"gemini_configured": True, "telnyx_configured": True},
            },
        )

    caplog.set_level(logging.INFO, logger="voice.panel_observation")
    result = asyncio.run(
        observe_panel_agent(TEST_PHONE, settings=settings(), transport=httpx.MockTransport(handler))
    )

    assert result is not None
    assert (result.agent_id, result.client_id, result.agent_name) == (1, 1, "Luisa")
    assert captured["method"] == "POST"
    assert captured["path"] == "/internal/v1/voice/resolve-agent"
    assert captured["secret"] == TEST_SECRET
    assert b'"direction":"inbound"' in captured["body"]
    assert "Panel observado: agent_id=1 client_id=1 agent_name=Luisa" in caplog.text
    assert TEST_SECRET not in caplog.text
    assert TEST_PHONE not in caplog.text
    assert "PROMPT-MUY-SENSIBLE" not in caplog.text
    assert "Leda" not in caplog.text


def test_http_error_is_controlled_without_sensitive_logs(caplog):
    async def handler(_request):
        return httpx.Response(401, json={"error": {"message": TEST_SECRET}})

    caplog.set_level(logging.INFO, logger="voice.panel_observation")
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

    caplog.set_level(logging.INFO, logger="voice.panel_observation")
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


def test_protected_audio_pipeline_remains_present():
    source = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
    for protected_fragment in (
        "audioop.ulaw2lin",
        "audioop.ratecv",
        "audioop.lin2ulaw",
        "tamano_paquete = 160",
        '@app.websocket("/media")',
        "cliente_gemini.aio.live.connect",
        "cancelar_temporizador_llamada",
    ):
        assert protected_fragment in source
