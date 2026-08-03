import asyncio

import pytest
from starlette.websockets import WebSocketDisconnect, WebSocketState

import main
from call_context import CallContextStore


class FakeWebSocket:
    def __init__(self, *, state=WebSocketState.CONNECTED, fail_send=False):
        self.client_state = state
        self.fail_send = fail_send
        self.messages = []
        self.accepted = False
        self.closed = False

    async def accept(self):
        self.accepted = True

    async def close(self):
        self.closed = True

    async def send_json(self, message):
        if self.fail_send:
            raise RuntimeError("simulated send failure")
        self.messages.append(message)


class FakeAudioFile:
    def __init__(self, data=None, error=None):
        self.data = data
        self.error = error

    def read_bytes(self):
        if self.error:
            raise self.error
        return self.data


def _prepare_call(monkeypatch):
    store = CallContextStore()
    store.register(
        call_control_id="control-a", call_session_id="session-a",
        from_number="caller", to_number="called",
    )
    monkeypatch.setattr(main, "CALL_CONTEXTS", store)
    return store


@pytest.mark.parametrize("stage", ["connect", "stream"])
def test_gemini_failure_plays_contingency_and_hangs_up_once(monkeypatch, stage):
    store = _prepare_call(monkeypatch)
    websocket = FakeWebSocket()
    hangups = []

    async def fake_hangup(call_control_id):
        hangups.append(call_control_id)

    monkeypatch.setattr(main, "CONTINGENCY_AUDIO_PATH", FakeAudioFile(data=b"\x11" * 160))
    monkeypatch.setattr(main, "colgar_llamada_telnyx", fake_hangup)

    asyncio.run(main.manejar_fallo_gemini(websocket, "control-a", stage))
    asyncio.run(main.manejar_fallo_gemini(websocket, "control-a", stage))

    context = store.get(call_control_id="control-a")
    assert len(websocket.messages) == 1
    assert hangups == ["control-a"]
    assert (context.failure_provider, context.failure_code, context.failure_stage) == (
        "gemini", "gemini_failed", stage,
    )


@pytest.mark.parametrize("scenario", ["send_error", "missing_file", "closed_socket"])
def test_gemini_failure_always_hangs_up_when_audio_cannot_play(monkeypatch, scenario):
    _prepare_call(monkeypatch)
    websocket = FakeWebSocket(
        state=WebSocketState.DISCONNECTED if scenario == "closed_socket" else WebSocketState.CONNECTED,
        fail_send=scenario == "send_error",
    )
    hangups = []

    async def fake_hangup(call_control_id):
        hangups.append(call_control_id)

    monkeypatch.setattr(
        main,
        "CONTINGENCY_AUDIO_PATH",
        FakeAudioFile(
            data=b"\x11" * 160 if scenario == "send_error" else None,
            error=FileNotFoundError("simulated missing file") if scenario == "missing_file" else None,
        ),
    )
    monkeypatch.setattr(main, "colgar_llamada_telnyx", fake_hangup)

    asyncio.run(main.manejar_fallo_gemini(websocket, "control-a", "stream"))

    assert websocket.messages == []
    assert hangups == ["control-a"]


def test_normal_websocket_disconnect_is_not_marked_as_gemini_failure(monkeypatch):
    websocket = FakeWebSocket()
    failures = []

    async def fake_receive_start(_websocket):
        raise WebSocketDisconnect(code=1000)

    async def fake_failure(*args):
        failures.append(args)

    monkeypatch.setattr(main, "recibir_inicio_telnyx", fake_receive_start)
    monkeypatch.setattr(main, "manejar_fallo_gemini", fake_failure)

    asyncio.run(main.websocket_audio_telnyx(websocket))

    assert websocket.accepted is True
    assert failures == []


def test_gemini_failure_has_no_gemini_retry_and_terminal_callback_recovers_details(monkeypatch):
    store = _prepare_call(monkeypatch)
    websocket = FakeWebSocket()
    hangups = []

    async def fake_hangup(call_control_id):
        hangups.append(call_control_id)

    class FailIfGeminiIsUsed:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Gemini must not be retried")

    monkeypatch.setattr(main, "CONTINGENCY_AUDIO_PATH", FakeAudioFile(data=b"\x11" * 160))
    monkeypatch.setattr(main, "colgar_llamada_telnyx", fake_hangup)
    monkeypatch.setattr(main.genai, "Client", FailIfGeminiIsUsed)

    asyncio.run(main.manejar_fallo_gemini(websocket, "control-a", "connect"))
    finished = store.finish("control-a", "provider_hangup")

    assert hangups == ["control-a"]
    assert main._detalles_terminales(finished) == ("gemini", "gemini_failed:connect")
    assert main._tipo_evento_terminal(finished) == "failed"
