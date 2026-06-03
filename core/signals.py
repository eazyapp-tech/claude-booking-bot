"""Request-scoped truth signals.

Tools record what actually happened (a write landed, a search came back empty,
an upstream API failed); egress reads the slate once to shape honest UI —
partial-success receipts, error≠empty rails, scarcity only from real counts.

A ContextVar keeps the slate per-async-task: concurrent requests never bleed
into each other, and fire-and-forget tasks (which copy the context at spawn)
cannot mutate the parent turn's slate. The default is never mutated in place —
reset replaces it, record copies-then-replaces, current returns a copy.
"""
from contextvars import ContextVar

_signals: ContextVar[dict] = ContextVar("turn_signals", default={})


def reset_signals() -> None:
    """Start a clean slate for one turn. Call once at the top of the request."""
    _signals.set({})


def record_signal(**kw) -> None:
    """Record truth points as they become known. Merges into the turn's slate."""
    cur = dict(_signals.get())
    cur.update(kw)
    _signals.set(cur)


def current_signals() -> dict:
    """Read the turn's accumulated signals at egress. Returns a copy; never mutates."""
    return dict(_signals.get())
