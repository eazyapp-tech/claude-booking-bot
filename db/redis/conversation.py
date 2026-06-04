"""
db/redis/conversation.py — Conversation history, session routing, and account context.

Covers:
  - Conversation history (get/save/clear)
  - Active request dedup (legacy text-based)
  - wamid dedup (wamid-based, for WhatsApp)
  - Per-user WhatsApp message queue (debounce + merge)
  - Pipeline cancellation signal (Phase C)
  - Last agent tracking
  - Account values + whitelabel PG IDs
"""

import time
from typing import Optional

import json

from config import settings
from db.redis._base import _r, _json_set, _json_get


# ---------------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------------

def get_conversation(user_id: str) -> list[dict]:
    raw = _r().get(f"{user_id}:conversation")
    if raw is None:
        return []
    return json.loads(raw)


def save_conversation(user_id: str, messages: list[dict], brand_hash: str | None = None) -> None:
    # Allow more messages when a summary is present (summary compresses older context)
    limit = settings.CONVERSATION_HISTORY_LIMIT * 2  # default: 40
    if messages and "[CONVERSATION_SUMMARY]" in str(messages[0].get("content", "")):
        limit = settings.CONVERSATION_HISTORY_LIMIT * 3  # with summary: 60
    trimmed = messages[-limit:]
    _r().setex(
        f"{user_id}:conversation",
        settings.CONVERSATION_TTL_SECONDS,
        json.dumps(trimmed),
    )
    # Track user in active_users sorted set (score = unix timestamp for recency ordering)
    _r().zadd("active_users", {user_id: time.time()})
    # Also track in brand-scoped sorted set if brand_hash is known
    if brand_hash:
        from db.redis.admin import add_to_brand_active_users, set_user_brand
        add_to_brand_active_users(user_id, brand_hash)
        set_user_brand(user_id, brand_hash)


def clear_conversation(user_id: str) -> None:
    _r().delete(f"{user_id}:conversation")


# ---------------------------------------------------------------------------
# Active request dedup (30s TTL)
# ---------------------------------------------------------------------------

def set_active_request(user_id: str, message: str, ttl: int = 30) -> None:
    _r().set(f"{user_id}:active_request", message, ex=ttl)


def get_active_request(user_id: str) -> Optional[str]:
    raw = _r().get(f"{user_id}:active_request")
    return raw.decode() if raw else None


def delete_active_request(user_id: str) -> None:
    _r().delete(f"{user_id}:active_request")


# ---------------------------------------------------------------------------
# Last active agent tracking (10-min TTL for multi-turn continuations)
# ---------------------------------------------------------------------------

def set_last_agent(user_id: str, agent_name: str, ttl: int = 3600) -> None:
    # 1h (was 600s): the 10-min window expired between turns when a user paused,
    # losing routing stickiness mid-conversation and leaving the admin view
    # showing "default" for every recent conversation (UAT artifact).
    _r().set(f"{user_id}:last_agent", agent_name, ex=ttl)


def get_last_agent(user_id: str) -> Optional[str]:
    raw = _r().get(f"{user_id}:last_agent")
    return raw.decode() if raw else None


# ---------------------------------------------------------------------------
# Account values (whitelabel config)
# ---------------------------------------------------------------------------

def set_account_values(user_id: str, values: dict) -> None:
    _json_set(f"{user_id}:account_values", values)


def get_account_values(user_id: str) -> dict:
    return _json_get(f"{user_id}:account_values", default={})


def clear_account_values(user_id: str) -> None:
    _r().delete(f"{user_id}:account_values")


# ---------------------------------------------------------------------------
# Whitelabel PG IDs
# ---------------------------------------------------------------------------

def set_whitelabel_pg_ids(user_id: str, pg_ids: list) -> None:
    _json_set(f"{user_id}:pg_ids", pg_ids)


def get_whitelabel_pg_ids(user_id: str) -> list[str]:
    return _json_get(f"{user_id}:pg_ids", default=[])


# ---------------------------------------------------------------------------
# wamid-based dedup (replaces text-content dedup for WhatsApp)
# ---------------------------------------------------------------------------
# Meta's unique message ID (wamid) is stable across duplicate delivery retries.
# TTL of 24h covers the longest known Meta retry window.

def set_wamid_seen(wamid: str, ttl: Optional[int] = None) -> None:
    """Mark a WhatsApp message ID as seen to prevent duplicate processing."""
    from config import settings
    _r().set(f"wamid:{wamid}", "1", ex=ttl or settings.WAMID_DEDUP_TTL)


def is_wamid_seen(wamid: str) -> bool:
    """Return True if this wamid has already been processed."""
    return bool(_r().exists(f"wamid:{wamid}"))


# ---------------------------------------------------------------------------
# Per-user WhatsApp message queue (for debounce + accumulation)
# ---------------------------------------------------------------------------
# Pattern: rapid-fire messages from same user accumulate in a Redis List.
# A single async drain task processes the full batch after a debounce window.

def wa_queue_push(user_id: str, message: str, ttl: Optional[int] = None) -> None:
    """Append a message text to the user's pending WhatsApp queue."""
    from config import settings
    key = f"{user_id}:wa_queue"
    _r().rpush(key, message)
    _r().expire(key, ttl or settings.WA_QUEUE_TTL)


def wa_queue_drain(user_id: str) -> list[str]:
    """Atomically drain all pending messages from the user's WhatsApp queue.

    Returns a list of message strings (may be empty if queue was already drained).
    """
    key = f"{user_id}:wa_queue"
    messages: list[str] = []
    while True:
        raw = _r().lpop(key)
        if raw is None:
            break
        messages.append(raw.decode() if isinstance(raw, bytes) else raw)
    return messages


def wa_queue_len(user_id: str) -> int:
    """Return the number of messages currently waiting in the user's queue."""
    return int(_r().llen(f"{user_id}:wa_queue") or 0)


def wa_processing_acquire(user_id: str, ttl: Optional[int] = None) -> bool:
    """Acquire the per-user processing lock using SET NX (atomic).

    Returns True if the lock was acquired (this caller should start draining).
    Returns False if another coroutine already holds the lock.
    """
    from config import settings
    result = _r().set(
        f"{user_id}:wa_processing", "1",
        ex=ttl or settings.WA_PROCESSING_TTL,
        nx=True,
    )
    return result is not None  # SET NX returns None if key already exists


def wa_processing_release(user_id: str) -> None:
    """Release the per-user processing lock."""
    _r().delete(f"{user_id}:wa_processing")


# ---------------------------------------------------------------------------
# Pipeline cancellation signal (Phase C — WhatsApp mid-flight interrupt)
# ---------------------------------------------------------------------------
# When a new batch of messages arrives while a pipeline run is still executing
# tool calls, the drain task sets this flag before starting the next run.
# The pipeline checks it between tool-call iterations — if set, the old run
# exits cleanly at the next natural boundary, allowing the new run to proceed.

CANCEL_REQUESTED_TTL = 30  # seconds — short-lived, only needed within one turn


def set_cancel_requested(user_id: str) -> None:
    """Signal that the current pipeline run for this user should cancel."""
    _r().set(f"{user_id}:cancel_requested", "1", ex=CANCEL_REQUESTED_TTL)


def clear_cancel_requested(user_id: str) -> None:
    """Clear the cancellation signal (called by pipeline after acting on it)."""
    _r().delete(f"{user_id}:cancel_requested")


def is_cancel_requested(user_id: str) -> bool:
    """Return True if a cancellation was requested for this user's pipeline."""
    return bool(_r().exists(f"{user_id}:cancel_requested"))
