"""Cálculos puros para el cierre de llamadas, sin red ni estado global."""

from __future__ import annotations


def closing_deadlines(
    *,
    answered_at: float,
    max_call_seconds: int,
    farewell_seconds_before_end: int,
    has_time_warning: bool,
) -> tuple[float | None, float]:
    """Devuelve inicio opcional de aviso y el inicio fijo de despedida final."""
    hard_limit = answered_at + max_call_seconds
    warning_start = (
        hard_limit - farewell_seconds_before_end
        if has_time_warning and farewell_seconds_before_end
        else None
    )
    return warning_start, hard_limit
