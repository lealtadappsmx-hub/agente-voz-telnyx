"""Contexto efímero e independiente para cada llamada activa."""

from __future__ import annotations

from dataclasses import dataclass

from panel_config_client import PanelAgentObservation


@dataclass
class CallContext:
    call_control_id: str
    call_session_id: str | None
    from_number: str | None
    to_number: str
    agent_config: PanelAgentObservation | None = None
    timer_state: str = "pending"
    hangup_reason: str | None = None


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

    def set_timer_state(self, call_control_id: str, state: str) -> bool:
        context = self.get(call_control_id=call_control_id)
        if context is None:
            return False
        context.timer_state = state
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
        if context.call_session_id:
            self._control_id_by_session.pop(context.call_session_id, None)
        return context

    @property
    def active_count(self) -> int:
        return len(self._by_control_id)
