"""
SILO — tell the property manager when a bot booking lands.

The bot persists visits / calls / tokens to the backend, but those write-paths
fire NO manager notification (verified read-only: services/bookingBot/bookingBot.ts
saveLeadData / controllers/tenant.ts addLeadFromEazyPGID / addBookingBotPayement all
just insert + return status:200). A captured lead nobody is told about is the worst
failure on the "Real" truth.

The backend already has the mechanism — POST /others/sendNotificationOnCall sends a
real FCM push, keyed off the property's pg_id, to the owner (receiver_type 101) and
team members (receiver_type 103). It is unauthenticated, and the bot already holds
each property's pg_id/pg_number. So we call it ourselves, fire-and-forget, right
after a CONFIRMED booking success.

Hard rules:
- Fire ONLY after the caller has confirmed success (inner status:200). Never push
  "booked!" for a save that didn't take.
- Never raise, never block the user reply. A push failure must never turn a real
  booking into a user-visible error (mirrors the Wave-A / C1 fail-open discipline).
"""

import asyncio

import httpx

from config import settings
from core.log import get_logger

logger = get_logger("tools.notify_manager")

_NOTIFY_URL = f"{settings.RENTOK_API_BASE_URL}/others/sendNotificationOnCall"
_TIMEOUT_S = 6.0

# Owner + team members — both audiences act on leads (PO decision).
_RECEIVER_TYPES = (101, 103)

_TITLES = {
    "visit": "New visit booked 🏠",
    "call": "New call request 📞",
    "token": "Token received 💰",
}


def build_booking_notification(
    kind: str, name: str, property_name: str, date: str = "", time: str = ""
) -> tuple[str, str]:
    """Pure: build the (title, body) the manager sees for a booking event.

    `kind` is "visit" | "call" | "token". Degrades gracefully when the prospect
    name / property / date-time are missing — always returns non-empty strings.
    """
    who = (name or "").strip() or "A prospect"
    where = (property_name or "").strip() or "one of your properties"
    when = " ".join(p for p in [(date or "").strip(), (time or "").strip()] if p)

    title = _TITLES.get(kind, "New booking 🔔")
    if kind == "call":
        body = f"{who} requested a call about {where}"
    elif kind == "token":
        body = f"{who} paid a token for {where}"
    else:  # visit (default)
        body = f"{who} booked a visit at {where}"
    if when:
        body += f" on {when}"
    return title, body


def _notify_payloads(
    pg_id: str, pg_number: str, title: str, body: str, notification_name: str
) -> list[dict]:
    """Pure: one sendNotificationOnCall payload per audience. Empty pg_id → []
    (the backend keys owner/team FCM tokens off pg_id, so without it we can't and
    won't notify blind)."""
    if not pg_id:
        return []
    base = {
        "pg_id": pg_id,
        "pg_number": pg_number or "",
        "notification_title": title,
        "notification_body": body,
        "notification_name": notification_name,
        "click_action": "",
    }
    return [{**base, "receiver_type": rt} for rt in _RECEIVER_TYPES]


async def _post(payload: dict) -> None:
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        await client.post(_NOTIFY_URL, json=payload)


async def notify_manager_booking(
    pg_id: str,
    pg_number: str,
    title: str,
    body: str,
    notification_name: str = "booking_bot",
    _post=_post,
) -> None:
    """Fire-and-forget owner+team FCM push for a confirmed booking.

    Never raises, never blocks: every POST is awaited concurrently with a short
    timeout and all errors are swallowed (logged). `_post` is injectable for tests.
    """
    payloads = _notify_payloads(pg_id, pg_number, title, body, notification_name)
    if not payloads:
        return

    async def _safe(p: dict) -> None:
        try:
            await _post(p)
        except Exception as e:  # never let a push failure touch the booking flow
            logger.warning(
                "manager notify failed (rt=%s pg_id=%s): %s",
                p.get("receiver_type"), pg_id, e,
            )

    try:
        await asyncio.gather(*(_safe(p) for p in payloads))
    except Exception as e:  # defensive — gather itself should never raise here
        logger.warning("manager notify gather error pg_id=%s: %s", pg_id, e)


def _resolve_name(user_id: str, kind: str) -> str:
    """Best-effort prospect name for the push. Redis-backed, so it is resolved
    INSIDE the background task (never on the booking's critical path). Any failure
    → "" (the message falls back to "A prospect")."""
    try:
        from db.redis_store import get_aadhar_user_name, get_user_phone
        name = get_aadhar_user_name(user_id) or ""
        if not name and kind == "token":
            name = get_user_phone(user_id) or ""
        return name
    except Exception:
        return ""


def fire_booking_notification(
    kind: str,
    user_id: str,
    pg_id: str,
    pg_number: str,
    property_name: str,
    date: str = "",
    time: str = "",
) -> None:
    """Schedule a manager push as a background task — the caller does NOT await it.

    Everything that could be slow or fail (the redis name lookup, the FCM POST) runs
    off the booking's critical path, so a confirmed visit/call/token is never delayed
    and never broken by a notification hiccup. Call this ONLY after a confirmed
    success. Under a unit test's asyncio.run() the loop closes before the task runs,
    so this stays fully hermetic (no redis, no network).
    """
    async def _run() -> None:
        try:
            name = _resolve_name(user_id, kind)
            title, body = build_booking_notification(kind, name, property_name, date, time)
            await notify_manager_booking(pg_id, pg_number, title, body, f"booking_bot_{kind}")
        except Exception as e:
            logger.warning("manager notify (%s) skipped: %s", kind, e)

    try:
        asyncio.create_task(_run())
    except RuntimeError:  # no running event loop (shouldn't happen from the async tools)
        logger.debug("manager notify (%s): no running loop, skipped", kind)
