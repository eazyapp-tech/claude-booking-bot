"""
db/redis/admin.py — Admin portal state: user enumeration, human mode, session cost, brand tagging.

Covers:
  - Active users sorted set (global + per-brand)
  - User → brand mapping (set/get)
  - Human mode takeover (get/set/clear)
  - Session cost tracking (per-user, 7-day TTL)
"""

import time

from db.redis._base import _r


# ---------------------------------------------------------------------------
# Admin portal — user enumeration (global)
# ---------------------------------------------------------------------------

def get_active_users(offset: int = 0, limit: int = 50) -> list[str]:
    """Return user IDs sorted by most recent activity (newest first).

    Uses the active_users sorted set populated by save_conversation().
    """
    raw = _r().zrevrange("active_users", offset, offset + limit - 1)
    return [uid.decode() if isinstance(uid, bytes) else uid for uid in raw]


def get_active_users_count() -> int:
    """Return total number of tracked users."""
    return _r().zcard("active_users") or 0


# ---------------------------------------------------------------------------
# Brand user tagging + per-brand user enumeration
# ---------------------------------------------------------------------------

def set_user_brand(uid: str, brand_hash: str) -> None:
    """Tag a user with their brand. No TTL — persistent."""
    _r().set(f"{uid}:brand_hash", brand_hash)


def get_user_brand(uid: str) -> str | None:
    """Return the brand_hash for this user, or None."""
    raw = _r().get(f"{uid}:brand_hash")
    return raw.decode() if raw else None


def add_to_brand_active_users(uid: str, brand_hash: str) -> None:
    """Add user to brand-scoped active_users sorted set."""
    _r().zadd(f"active_users:{brand_hash}", {uid: time.time()})


def get_brand_active_users(brand_hash: str, offset: int = 0, limit: int = 50) -> list[str]:
    """Return UIDs for a specific brand, sorted by recency."""
    raw = _r().zrevrange(f"active_users:{brand_hash}", offset, offset + limit - 1)
    return [uid.decode() if isinstance(uid, bytes) else uid for uid in raw]


def get_brand_active_users_count(brand_hash: str) -> int:
    """Return total number of tracked users for a brand."""
    return _r().zcard(f"active_users:{brand_hash}") or 0


# ---------------------------------------------------------------------------
# Human mode (admin takeover)
# ---------------------------------------------------------------------------

def get_human_mode(uid: str, brand_hash: str | None = None) -> bool:
    """Return True if admin has taken over this conversation.

    Checks brand-scoped key first (``{uid}:{brand_hash}:human_mode``), then
    falls back to the global key (``{uid}:human_mode``) for backward compat
    with pre-migration takeovers.
    """
    if brand_hash:
        val = _r().hget(f"{uid}:{brand_hash}:human_mode", "active")
        if val == b"1" or val == "1":
            return True
    # Fallback: global key (backward compat with pre-migration takeovers)
    val = _r().hget(f"{uid}:human_mode", "active")
    return val == b"1" or val == "1"


def set_human_mode(uid: str, brand_hash: str | None = None) -> None:
    """Activate human takeover. Brand-scoped if *brand_hash* is provided."""
    key = f"{uid}:{brand_hash}:human_mode" if brand_hash else f"{uid}:human_mode"
    _r().hset(key, mapping={"active": "1", "taken_at": str(time.time())})


def clear_human_mode(uid: str, brand_hash: str | None = None) -> None:
    """Deactivate human takeover — AI resumes handling the conversation.

    Always clears the global key as well for backward-compat cleanup.
    """
    if brand_hash:
        _r().delete(f"{uid}:{brand_hash}:human_mode")
    _r().delete(f"{uid}:human_mode")  # Always clear global for migration cleanup


# ---------------------------------------------------------------------------
# Session cost tracking (per-user, 7-day TTL)
# ---------------------------------------------------------------------------

def increment_session_cost(uid: str, tokens_in: int, tokens_out: int, cost_usd: float) -> None:
    """Accumulate token usage and USD cost for a user's session (7-day TTL).

    `cost_usd` is precomputed by the caller (core.claude._usage_cost) so cache
    reads/writes are billed at their correct multipliers — recomputing here at
    the full input rate would over-count cached tokens.
    """
    key = f"{uid}:session_cost"
    pipe = _r().pipeline()
    pipe.hincrbyfloat(key, "tokens_in", tokens_in)
    pipe.hincrbyfloat(key, "tokens_out", tokens_out)
    pipe.hincrbyfloat(key, "cost_usd", cost_usd)
    pipe.expire(key, 7 * 86400)
    pipe.execute()


def get_session_cost(uid: str) -> dict:
    """Return accumulated cost stats for a user. Returns zeros if no data."""
    raw = _r().hgetall(f"{uid}:session_cost")
    if not raw:
        return {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
    return {
        "tokens_in": int(float(raw.get(b"tokens_in", 0))),
        "tokens_out": int(float(raw.get(b"tokens_out", 0))),
        "cost_usd": round(float(raw.get(b"cost_usd", 0.0)), 6),
    }
