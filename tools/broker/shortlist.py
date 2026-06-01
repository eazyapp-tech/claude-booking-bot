import httpx

from config import settings
from db.redis_store import get_whitelabel_pg_ids, track_funnel, get_user_phone, record_property_shortlisted, schedule_followup, get_user_brand, track_property_event
from core.log import get_logger
from utils.properties import find_property

logger = get_logger("tools.shortlist")

TOOL_SCHEMA = {
    "name": "shortlist_property",
    "description": "Add a property to the user's shortlist for later reference.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "property_name": {"type": "string", "description": "Exact name of the property to shortlist"},
        },
        "required": ["property_name"],
    },
}


async def shortlist_property(user_id: str, property_name: str, **kwargs) -> str:
    prop = find_property(user_id, property_name)
    if not prop:
        return f"Property '{property_name}' not found in search results."

    prop_id = prop.get("prop_id") or prop.get("pg_id")
    if not prop_id:
        return f"Cannot shortlist — property ID missing for '{property_name}'. Please search again."
    # property_contact = the property's own phone (from listing data), not the user's phone
    property_contact = prop.get("phone_number", "")
    # user_id field: use real phone if available, else full user_id as opaque key
    user_phone = get_user_phone(user_id) or user_id

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{settings.RENTOK_API_BASE_URL}/bookingBot/shortlist-booking-bot-property",
                json={
                    "user_id": user_phone,
                    "property_id": prop_id,
                    "property_contact": property_contact,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return f"Error shortlisting property: {str(e)}"

    # This endpoint signals success via inner status==200 (HTTP is always 200) and
    # carries NO `success` key — verified live. Accept either convention defensively
    # so a genuine 200 is never mis-reported as a failure.
    ok = data.get("success") is True or data.get("status") in (200, "200")
    if not ok:
        msg = data.get("message", "unknown error")
        return f"Could not shortlist '{property_name}': {msg}. Please try again."

    brand_hash_val = get_user_brand(user_id)
    track_funnel(user_id, "shortlist", brand_hash=brand_hash_val)
    record_property_shortlisted(user_id, prop_id)
    try:
        track_property_event(prop_id, "shortlisted", brand_hash=brand_hash_val)
    except Exception:
        pass

    # Schedule follow-up: 48h after shortlisting (only if no visit scheduled)
    try:
        schedule_followup(user_id, "shortlist_idle", {
            "property_name": prop.get("property_name", property_name),
            "property_id": str(prop_id),
        }, 172800)  # 48 hours
    except Exception as e:
        logger.warning("shortlist follow-up scheduling failed: %s", e)

    return f"Property '{prop.get('property_name', property_name)}' has been shortlisted successfully."
