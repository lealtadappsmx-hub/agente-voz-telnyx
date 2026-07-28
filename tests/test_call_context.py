from call_context import CallContextStore
from panel_config_client import PanelAgentObservation


def observation(agent_id: int, name: str) -> PanelAgentObservation:
    return PanelAgentObservation(
        agent_id=agent_id,
        client_id=agent_id,
        agent_name=name,
        system_prompt=f"Prompt dinámico completo para {name} y su empresa.",
        elapsed_ms=25.0,
    )


def test_two_calls_keep_independent_contexts():
    store = CallContextStore()
    first = store.register(
        call_control_id="control-a",
        call_session_id="session-a",
        from_number="caller-a",
        to_number="called-a",
    )
    second = store.register(
        call_control_id="control-b",
        call_session_id="session-b",
        from_number="caller-b",
        to_number="called-b",
    )

    assert store.set_agent_config("control-a", observation(1, "Luisa")) is True
    assert store.set_agent_config("control-b", observation(2, "Ana")) is True
    assert store.set_timer_state("control-a", "active") is True

    assert first.agent_config.agent_name == "Luisa"
    assert second.agent_config.agent_name == "Ana"
    assert first.agent_config.system_prompt != second.agent_config.system_prompt
    assert first.timer_state == "active"
    assert second.timer_state == "pending"
    assert store.get(call_session_id="session-a") is first
    assert store.get(call_session_id="session-b") is second
    assert store.active_count == 2


def test_finishing_one_call_does_not_remove_the_other():
    store = CallContextStore()
    first = store.register(
        call_control_id="control-a",
        call_session_id="session-a",
        from_number="caller-a",
        to_number="called-a",
    )
    second = store.register(
        call_control_id="control-b",
        call_session_id="session-b",
        from_number="caller-b",
        to_number="called-b",
    )

    finished = store.finish("control-a", "normal_clearing")

    assert finished is first
    assert finished.hangup_reason == "normal_clearing"
    assert finished.timer_state == "finished"
    assert store.get(call_control_id="control-a") is None
    assert store.get(call_session_id="session-a") is None
    assert store.get(call_control_id="control-b") is second
    assert store.active_count == 1


def test_stream_can_link_session_without_exposing_or_copying_context():
    store = CallContextStore()
    context = store.register(
        call_control_id="control-a",
        call_session_id=None,
        from_number="caller-a",
        to_number="called-a",
    )

    linked = store.link_session(
        call_control_id="control-a",
        call_session_id="session-from-stream",
    )

    assert linked is context
    assert store.get(call_session_id="session-from-stream") is context


def test_late_observation_is_ignored_after_cleanup():
    store = CallContextStore()
    store.register(
        call_control_id="control-a",
        call_session_id="session-a",
        from_number=None,
        to_number="called-a",
    )
    store.finish("control-a", "remote_hangup")

    assert store.set_agent_config("control-a", observation(1, "Luisa")) is False
    assert store.active_count == 0


def test_context_queues_one_closing_message_only_when_runtime_is_ready():
    store = CallContextStore()
    context = store.register(
        call_control_id="control-a",
        call_session_id="session-a",
        from_number="caller-a",
        to_number="called-a",
    )

    assert store.request_closure_message("control-a", "time_warning", "Mensaje") is False
    assert store.mark_runtime_ready("control-a", True) is True
    assert store.request_closure_message("control-a", "time_warning", "Mensaje") is True
    assert context.closure_queue.get_nowait() == ("time_warning", "Mensaje")
    assert context.closure_turn_finished.is_set() is False
    assert store.complete_closure_turn("control-a") == "time_warning"
    assert context.closure_turn_finished.is_set() is True
    assert store.is_closing("control-a") is False
