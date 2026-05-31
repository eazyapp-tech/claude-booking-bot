import httpx

from config import settings
from core.log import get_logger
from utils.api import user_error
from utils.properties import find_property as _find_property

logger = get_logger("tools.cancel")


TOOL_SCHEMA = {
    "name": "cancel_booking",
    "description": "Cancel an existing visit, call, or booking for a property.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "property_name": {"type": "string", "description": "Exact property name"},
        },
        "required": ["property_name"],
    },
}


async def cancel_booking(user_id: str, property_name: str, **kwargs) -> str:
    prop = _find_property(user_id, property_name)
    if not prop:
        return f"Property '{property_name}' not found."

    property_id = prop.get("property_id", "")
    if not property_id:
        return "Property ID not available."

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{settings.RENTOK_API_BASE_URL}/bookingBot/cancel-booking",
                json={"user_id": user_id, "property_id": property_id},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return user_error("cancel your booking", e, logger=logger)

    # cancel-booking normally returns HTTP 200 with no `success` field, so a clean
    # response is treated as success. But if the body DOES carry an explicit
    # failure signal, respect it rather than reporting a false success.
    if isinstance(data, dict):
        status = data.get("status")
        if (data.get("success") is False
                or (isinstance(status, int) and status >= 400)
                or (isinstance(status, str) and status.lower() == "error")):
            msg = data.get("message", "the booking could not be cancelled")
            logger.warning("cancel rejected by API for user=%s property=%s: %s", user_id, property_id, msg)
            return f"I couldn't cancel that booking: {msg}. Please try again or contact support."

    # Cancellation succeeded — fully clear any reserve_bed idempotency entry (lock
    # AND cached result) so the user can immediately re-reserve the same property.
    # Without this, a reserve→cancel→reserve sequence within the dedup window would
    # replay the stale cached "reserved" result instead of making a fresh
    # reservation. idem_release alone is insufficient: it only drops the lock,
    # leaving the cached result to be replayed — idem_clear removes both.
    try:
        from core.tool_boundary import idempotency_key
        from db.redis_store import idem_clear
        idem_clear(idempotency_key(user_id, "reserve_bed", {"property_name": property_name}))
    except Exception:
        pass

    return f"Booking cancelled successfully for '{prop.get('property_name', property_name)}'."
