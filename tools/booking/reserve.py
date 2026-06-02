import httpx

from config import settings
from core.log import get_logger
from db.redis_store import track_funnel, get_user_brand, track_property_event
from utils.api import user_error
from utils.properties import find_property as _find_property

logger = get_logger("tools.reserve")


CHECK_RESERVE_BED_SCHEMA = {
    "name": "check_reserve_bed",
    "description": "Check if a bed is already reserved for the user at a property. Returns success: true if reserved, false if not.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "property_name": {"type": "string", "description": "Exact property name"},
        },
        "required": ["property_name"],
    },
}

_reserve_prereq = (
    "ONLY call after KYC verification and payment completion."
    if settings.PAYMENT_REQUIRED and settings.KYC_ENABLED
    else "ONLY call after payment completion."
    if settings.PAYMENT_REQUIRED
    else "ONLY call after KYC verification."
    if settings.KYC_ENABLED
    else "Call to reserve a bed/room at a property."
)

RESERVE_BED_SCHEMA = {
    "name": "reserve_bed",
    "description": f"Reserve a bed/room at a property. {_reserve_prereq}",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "property_name": {"type": "string", "description": "Exact property name"},
        },
        "required": ["property_name"],
    },
}


async def check_reserve_bed(user_id: str, property_name: str, **kwargs) -> str:
    prop = _find_property(user_id, property_name)
    if not prop:
        return f"Property '{property_name}' not found."

    property_id = prop.get("property_id", "")
    if not property_id:
        return "Property ID not available."

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{settings.RENTOK_API_BASE_URL}/bookingBot/checkPropetyReserved",
                json={"user_id": user_id, "property_id": property_id},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return user_error("check the reservation status", e, logger=logger)

    if data.get("data") is True:
        return f"A bed is already reserved for you at '{prop.get('property_name', property_name)}'."
    return f"No bed reserved yet at '{prop.get('property_name', property_name)}'. You can proceed with reservation."


async def reserve_bed(user_id: str, property_name: str, **kwargs) -> str:
    prop = _find_property(user_id, property_name)
    if not prop:
        return f"Property '{property_name}' not found."

    property_id = prop.get("property_id", "")
    if not property_id:
        return "Property ID not available."

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{settings.RENTOK_API_BASE_URL}/bookingBot/reserveProperty",
                json={"user_id": user_id, "property_id": property_id},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return user_error("reserve the bed", e, logger=logger)

    if data.get("success"):
        brand_hash_val = get_user_brand(user_id)
        track_funnel(user_id, "booking_initiated", brand_hash=brand_hash_val)
        try:
            track_property_event(property_id, "booking_initiated", brand_hash=brand_hash_val)
        except Exception:
            pass
        # Reconciliation: record a durable claim that this reserve landed, so the
        # hourly cron can detect a silent RentOk write failure. Fire-and-forget —
        # a ledger failure must never affect the booking the user just made.
        if settings.RECONCILE_ENABLED:
            try:
                from db import postgres as pg
                await pg.insert_claim(
                    uid=user_id, phone=None, property_id=property_id,
                    event="reserve", brand_hash=brand_hash_val,
                )
            except Exception:
                logger.warning("recon claim insert failed (reserve)", exc_info=True)
        return f"Bed reserved successfully at '{prop.get('property_name', property_name)}'!"
    return f"Failed to reserve bed: {data.get('message', 'Unknown error')}"
