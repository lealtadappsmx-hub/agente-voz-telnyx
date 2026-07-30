"""Contexto efímero e independiente para cada llamada activa."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from time import monotonic

from panel_config_client import PanelAgentObservation


@dataclass
class CallContext:
    call_control_id: str
    call_session_id: str | None
    from_number: str | None
    to_number: str
    direction: str = "inbound"
    outbound_campaign_id: int | None = None
    outbound_recipient_id: int | None = None
    agent_config: PanelAgentObservation | None = None
    telnyx_api_key: str | None = field(default=None, repr=False)
    configuration_ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    configuration_failed: bool = False
    timer_state: str = "pending"
    hangup_reason: str | None = None
    answered_at_monotonic: float | None = None
    runtime_ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    closure_turn_finished: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    closure_queue: asyncio.Queue[tuple[str, str]] = field(default_factory=asyncio.Queue, repr=False)
    closure_phase: str | None = None
    transfer_state: str = "idle"


class CallContextStore:
    """Índices en memoria para las llamadas del único proceso Uvicorn."""

    def __init__(self) -> None:
        self._by_control_id: dict[str, CallContext] = {}
        self._control_id_by_session: dict[str, str] = {}

    @staticmethod
    def _clean_optional(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned or None

    def register(
        self,
        *,
        call_control_id: str,
        call_session_id: str | None,
        from_number: str | None,
        to_number: str,
        direction: str = "inbound",
        outbound_campaign_id: int | None = None,
        outbound_recipient_id: int | None = None,
    ) -> CallContext:
        control_id = self._clean_optional(call_control_id)
        if control_id is None:
            raise ValueError("call_control_id is required")

        previous = self._by_control_id.get(control_id)
        if previous and previous.call_session_id:
            self._control_id_by_session.pop(previous.call_session_id, None)

        context = CallContext(
            call_control_id=control_id,
            call_session_id=self._clean_optional(call_session_id),
            from_number=self._clean_optional(from_number),
            to_number=self._clean_optional(to_number) or "",
            direction="outbound" if direction == "outbound" else "inbound",
            outbound_campaign_id=outbound_campaign_id if type(outbound_campaign_id) is int and outbound_campaign_id > 0 else None,
            outbound_recipient_id=outbound_recipient_id if type(outbound_recipient_id) is int and outbound_recipient_id > 0 else None,
        )
        self._by_control_id[control_id] = context
        if context.call_session_id:
            self._control_id_by_session[context.call_session_id] = control_id
        return context

    def get(
        self,
        *,
        call_control_id: str | None = None,
        call_session_id: str | None = None,
    ) -> CallContext | None:
        control_id = self._clean_optional(call_control_id)
        if control_id:
            return self._by_control_id.get(control_id)

        session_id = self._clean_optional(call_session_id)
        if not session_id:
            return None
        indexed_control_id = self._control_id_by_session.get(session_id)
        return self._by_control_id.get(indexed_control_id) if indexed_control_id else None

    def link_session(
        self,
        *,
        call_control_id: str | None,
        call_session_id: str | None,
    ) -> CallContext | None:
        context = self.get(
            call_control_id=call_control_id,
            call_session_id=call_session_id,
        )
        session_id = self._clean_optional(call_session_id)
        if context is None or session_id is None:
            return context

        if context.call_session_id and context.call_session_id != session_id:
            self._control_id_by_session.pop(context.call_session_id, None)
        context.call_session_id = session_id
        self._control_id_by_session[session_id] = context.call_control_id
        return context

    def set_agent_config(
        self,
        call_control_id: str,
        agent_config: PanelAgentObservation,
    ) -> bool:
        context = self.get(call_control_id=call_control_id)
        if context is None:
            return False
        context.agent_config = agent_config
        return True

    def set_telnyx_api_key(self, call_control_id: str, api_key: str) -> bool:
        context = self.get(call_control_id=call_control_id)
        cleaned_key = api_key.strip() if isinstance(api_key, str) else ""
        if context is None or not cleaned_key:
            return False
        context.telnyx_api_key = cleaned_key
        return True

    def mark_configuration_ready(self, call_control_id: str, ready: bool) -> bool:
        context = self.get(call_control_id=call_control_id)
        if context is None:
            return False
        context.configuration_failed = not ready
        context.configuration_ready.set()
        return True

    def set_timer_state(self, call_control_id: str, state: str) -> bool:
        context = self.get(call_control_id=call_control_id)
        if context is None:
            return False
        context.timer_state = state
        return True

    def mark_answered(self, call_control_id: str) -> bool:
        context = self.get(call_control_id=call_control_id)
        if context is None:
            return False
        context.answered_at_monotonic = monotonic()
        return True

    def mark_runtime_ready(self, call_control_id: str, ready: bool) -> bool:
        context = self.get(call_control_id=call_control_id)
        if context is None:
            return False
        if ready:
            context.runtime_ready.set()
        else:
            context.runtime_ready.clear()
        return True

    def request_closure_message(self, call_control_id: str, phase: str, message: str) -> bool:
        context = self.get(call_control_id=call_control_id)
        if (
            context is None
            or not context.runtime_ready.is_set()
            or context.closure_phase is not None
            or not message
        ):
            return False
        context.closure_phase = phase
        context.closure_turn_finished.clear()
        context.closure_queue.put_nowait((phase, message))
        return True

    def complete_closure_turn(self, call_control_id: str) -> str | None:
        context = self.get(call_control_id=call_control_id)
        if context is None or context.closure_phase is None:
            return None
        phase = context.closure_phase
        context.closure_phase = None
        context.closure_turn_finished.set()
        return phase

    def is_closing(self, call_control_id: str) -> bool:
        context = self.get(call_control_id=call_control_id)
        return bool(context and context.closure_phase)

    def begin_transfer(self, call_control_id: str) -> bool:
        context = self.get(call_control_id=call_control_id)
        if context is None or context.transfer_state != "idle" or not context.runtime_ready.is_set():
            return False
        context.transfer_state = "announcing"
        return True

    def set_transfer_state(self, call_control_id: str, state: str) -> bool:
        context = self.get(call_control_id=call_control_id)
        if context is None:
            return False
        context.transfer_state = state
        return True

    def finish(
        self,
        call_control_id: str,
        hangup_reason: str | None,
    ) -> CallContext | None:
        control_id = self._clean_optional(call_control_id)
        if control_id is None:
            return None
        context = self._by_control_id.pop(control_id, None)
        if context is None:
            return None
        context.hangup_reason = self._clean_optional(hangup_reason) or "unknown"
        context.timer_state = "finished"
        context.telnyx_api_key = None
        context.configuration_failed = True
        context.configuration_ready.set()
        if context.call_session_id:
            self._control_id_by_session.pop(context.call_session_id, None)
        return context

    @property
    def active_count(self) -> int:
        return len(self._by_control_id)
