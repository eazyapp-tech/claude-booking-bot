"""
db/redis/idempotency.py — Burst-dedup primitives for write-path tools (Wave 3).

A short-window guard so a tool whose execution has a real-world side effect
(creating a booking / payment / lead) is not run twice when the pipeline
re-fires within seconds — the Phase B/C multi-turn drain race, a client
double-tap, or a network retry. Two layers, both keyed by a hash of
(user, tool, args) computed in core.tool_boundary.idempotency_key:

  - lock   (SET NX, window TTL): only one in-flight execution at a time.
  - result (window TTL): a completed result is replayed for duplicates arriving
    inside the window — no second handler call, no second CRM write.

After the window expires a fresh attempt is allowed, so legitimate retries and
genuinely new bookings still go through. Mirrors the SET-NX lock pattern used
by wa_processing_acquire in conversation.py.
"""

from ._base import _r, _json_get, _json_set


def _lock_key(idem_key: str) -> str:
    return f"idem:lock:{idem_key}"


def _result_key(idem_key: str) -> str:
    return f"idem:result:{idem_key}"


def idem_begin(idem_key: str, window: int) -> tuple[str | None, bool]:
    """Check for a cached result, else acquire the in-flight lock.

    Returns (cached_result, acquired):
      - (str, False)  → a completed result exists; replay it, do NOT run.
      - (None, True)  → first caller; lock acquired, run the tool.
      - (None, False) → another call is in flight; tell the user to wait.
    """
    cached = _json_get(_result_key(idem_key))
    if cached is not None:
        return cached, False
    acquired = bool(_r().set(_lock_key(idem_key), b"1", ex=window, nx=True))
    return None, acquired


def idem_complete(idem_key: str, result: str, window: int) -> None:
    """Cache the completed result (replayed for in-window duplicates) and release the lock."""
    _json_set(_result_key(idem_key), result, ex=window)
    _r().delete(_lock_key(idem_key))


def idem_release(idem_key: str) -> None:
    """Release the lock without caching — a failed execution should be retryable."""
    _r().delete(_lock_key(idem_key))
