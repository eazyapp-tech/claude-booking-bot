"""Request-scoped truth signals.

Tools record what actually happened (a write landed, a search came back empty,
an upstream API failed); egress reads the slate once to shape honest UI —
partial-success receipts, error≠empty rails, scarcity only from real counts.

A ContextVar holds the per-turn slate. reset_signals() installs a FRESH dict at
the top of each turn (in the parent task, before any asyncio.gather over tool
execution). record_signal() mutates that dict IN PLACE: asyncio.gather runs each
tool in a child task whose context is a COPY of the parent's, but copy_context()
shares the dict VALUE by reference — so an in-place update inside a gathered tool
is visible to the parent at egress. (Copy-then-set would write to the child's
context copy and be lost — the bug this design avoids.) The shared module default
is guarded and never mutated, so concurrent turns never bleed into each other.
current_signals() returns a copy.
"""
from contextvars import ContextVar

# A guarded sentinel: never mutated. Each turn installs its own dict via reset.
_DEFAULT: dict = {}
_signals: ContextVar[dict] = ContextVar("turn_signals", default=_DEFAULT)


def reset_signals() -> None:
    """Start a clean slate for one turn. Call once at the top of the request,
    in the parent task — BEFORE any asyncio.gather over tool execution."""
    _signals.set({})


def record_signal(**kw) -> None:
    """Record truth points as they become known. Mutates the turn's slate IN
    PLACE so writes made inside asyncio.gather child tasks (which copy the
    context but share the dict value) reach the parent at egress."""
    cur = _signals.get()
    if cur is _DEFAULT:  # never mutate the shared default; install a private dict
        cur = {}
        _signals.set(cur)
    cur.update(kw)


def current_signals() -> dict:
    """Read the turn's accumulated signals at egress. Returns a copy; never mutates."""
    return dict(_signals.get())
