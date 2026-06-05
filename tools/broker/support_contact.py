"""
Share a property's PUBLIC customer-care / support contact — G-20.

RentOk already captures this per property: `property.communication_contact`, with
microsite overrides `customer_support_whatsapp` / `customer_support_number`. The
RentOk backend's own bot surfaces it to users, so it is a public-safe line —
distinct from `personal_contact`, the OWNER's private number, which the bot caches
server-side and must NEVER reveal.

This tool is invoked ONLY when the user asks for a number / a human / is stuck, so
the contact is never volunteered. The number lives only in property-details-bots
(search results don't carry it), so we fetch on demand and cache onto the property.
"""

import httpx

from config import settings
from db.redis_store import get_property_info_map, set_property_info_map
from utils.properties import find_property as _find_property
from core.log import get_logger

logger = get_logger("tools.support_contact")

TOOL_SCHEMA = {
    "name": "get_support_contact",
    "description": (
        "Get the property's customer-care / support phone number to share with the user. "
        "Call this ONLY when the user asks for a phone number, asks to talk to a person/human, "
        "or is stuck / an action keeps failing — never volunteer it otherwise. "
        "Pass the property name if the user named one; otherwise the most recently discussed "
        "property is used. Returns the PUBLIC customer-care line, never the owner's private number."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "property_name": {
                "type": "string",
                "description": "Exact property name the user is asking about (optional — defaults to the most recent).",
            },
        },
        "required": [],
    },
}


def extract_support_contact(pd: dict, ms: dict) -> str:
    """Public customer-care number via RentOk's own fallback order.

    microsite customer_support_whatsapp → customer_support_number → property
    communication_contact. NEVER personal_contact (the owner's private line).
    """
    for k in ("customer_support_whatsapp", "customer_support_number"):
        v = (ms or {}).get(k)
        if v and str(v).strip():
            return str(v).strip()
    v = (pd or {}).get("communication_contact")
    if v and str(v).strip():
        return str(v).strip()
    return ""


async def _fetch_support_contact(prop_id: str) -> str:
    """Fetch property-details-bots and extract the public support contact. '' on any failure."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{settings.RENTOK_API_BASE_URL}/property/property-details-bots",
                json={"property_id": prop_id},
            )
            resp.raise_for_status()
            outer = (resp.json() or {}).get("data", {}) or {}
            pd = outer.get("property") or outer
            ms = outer.get("propertyMicrosite") or {}
            return extract_support_contact(pd, ms)
    except Exception as e:
        logger.debug("support contact fetch failed for prop_id=%s: %s", prop_id, e)
        return ""


_CALLBACK_OFFER = (
    "I couldn't pull a direct number just now, but I can have our team reach out to you — "
    "want me to schedule a callback?"
)


async def get_support_contact(user_id: str, property_name: str = "", **kwargs) -> str:
    info_map = get_property_info_map(user_id)
    prop = _find_property(user_id, property_name) if property_name else (info_map[-1] if info_map else None)

    if not prop:
        return (
            "I don't have a specific property in view yet — tell me which one you're interested in "
            "and I'll share the right contact, or I can schedule a callback for you."
        )

    name = prop.get("property_name", "property")
    cached = prop.get("support_contact")
    if cached:
        return f"You can reach the {name} support team at 📞 {cached}."

    prop_id = prop.get("prop_id") or prop.get("property_id")
    contact = await _fetch_support_contact(prop_id) if prop_id else ""
    if not contact:
        return _CALLBACK_OFFER

    # Cache onto the property so we don't re-fetch.
    for p in info_map:
        if p.get("property_name", "").strip().lower() == name.strip().lower():
            p["support_contact"] = contact
            break
    try:
        set_property_info_map(user_id, info_map)
    except Exception:
        pass
    return f"You can reach the {name} support team at 📞 {contact}."
