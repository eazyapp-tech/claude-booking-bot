"""
Per-room live availability + anonymized resident profile for a property.
Calls GET /bookingBot/availability and formats the data with compatibility
signals (working type match, shared hometowns, gender mix, avg tenure).
"""
import logging
from typing import Optional

import httpx

from config import settings
from db.redis_store import get_preferences
from utils.properties import find_property

logger = logging.getLogger("tools.broker.room_availability")

_AVAILABILITY_URL = f"{settings.RENTOK_API_BASE_URL}/bookingBot/availability"

TOOL_SCHEMA = {
    "name": "fetch_room_availability",
    "description": (
        "Get per-room live availability AND anonymized resident profile "
        "(working type, hometowns, gender mix, avg tenure) for a property. "
        "Call alongside fetch_room_details when the user asks about a specific "
        "property — resident data enables compatibility matching (professional vs "
        "student, shared hometowns). Takes a property name from search results."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "property_name": {
                "type": "string",
                "description": "Exact property name from search results",
            },
        },
        "required": ["property_name"],
    },
}


def _compat_hint(user_type: str, room_type: str) -> str:
    if not user_type or room_type in ("unknown", "mixed"):
        return ""
    u = user_type.lower()
    if "student" in u and room_type == "student":
        return " ← fellow students"
    if any(w in u for w in ("professional", "working", "employ")) and room_type == "professional":
        return " ← fellow professionals"
    return ""


def _format_room(room: dict, user_working_type: str, user_hometown: str) -> list[str]:
    lines = []
    name = room.get("room_name", "Room")
    sharing = room.get("sharing_type")
    rent = room.get("rent_per_bed", 0)
    kind = room.get("availability_kind", "")
    next_from = room.get("next_available_from")
    mix = room.get("tenant_mix") or {}
    tags = room.get("tags") or []

    status_map = {
        "vacant_now": "✅ VACANT NOW",
        "soft_hold": "🟡 ON HOLD",
        "freeing_on_notice": f"🟡 FREEING ~{next_from}" if next_from else "🟡 FREEING SOON",
        "occupied": "🔴 OCCUPIED",
    }
    status = status_map.get(kind, kind.upper())
    sharing_str = f"{sharing}-sharing" if sharing else "shared"
    rent_str = f" | ₹{rent:,}/bed" if rent else ""
    lines.append(f"**{name}** — {sharing_str}{rent_str} | {status}")

    total = mix.get("total_tenants", 0)
    gender = mix.get("gender", "unknown")
    wtype = mix.get("working_type", "unknown")
    tenure = mix.get("avg_tenure_months")
    cities: list = mix.get("top_origin_cities") or []

    if total > 0:
        wtype_label = {
            "professional": "working professionals",
            "student": "students",
            "mixed": "professionals + students",
        }.get(wtype, f"{total} resident{'s' if total > 1 else ''}")
        hint = _compat_hint(user_working_type, wtype)
        gender_label = {
            "all_male": ", all male",
            "all_female": ", all female",
            "mixed": ", mixed-gender",
        }.get(gender, "")
        if wtype != "unknown":
            lines.append(f"  Residents: {total} {wtype_label}{gender_label}{hint}")
        else:
            lines.append(f"  Residents: {total}{gender_label}")

        if cities:
            city_str = ", ".join(cities[:3])
            match = ""
            if user_hometown:
                if any(user_hometown in c.lower() for c in cities):
                    match = " ← your city too"
            lines.append(f"  Hometowns: {city_str}{match}")

        if tenure is not None:
            if tenure >= 6:
                lines.append(f"  Vibe: Settled — residents avg {tenure} months here")
            elif tenure <= 3:
                lines.append(f"  Vibe: Fresh room — recently turned over ({tenure} months avg)")

    elif kind == "vacant_now":
        lines.append("  Residents: Completely vacant — you'd be among the first")

    if tags:
        lines.append(f"  Features: {', '.join(tags)}")

    return lines


async def fetch_room_availability(user_id: str, property_name: str, **kwargs) -> str:
    prop = find_property(user_id, property_name)
    if not prop:
        return f"Property '{property_name}' not found in your recent search."

    pg_id = prop.get("property_id") or prop.get("pg_id") or ""
    if not pg_id:
        return "Property ID not available — try fetching room details instead."

    try:
        prefs = get_preferences(user_id) or {}
    except Exception:
        prefs = {}

    user_working_type = prefs.get("working_type", "")
    user_hometown = (prefs.get("hometown") or "").strip().lower()

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(_AVAILABILITY_URL, params={"pg_ids": pg_id})
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("availability error pg_id=%s: %s", pg_id, e)
        return "Availability info isn't loading right now. Room details are still available via fetch_room_details."

    rooms: list = []
    raw = data.get("data") or {}
    if isinstance(raw, dict):
        rooms = raw.get(pg_id, [])

    if not rooms:
        return f"No room availability data for '{property_name}' yet. Use fetch_room_details for layout info."

    available = [r for r in rooms if r.get("is_available")]
    occupied = [r for r in rooms if not r.get("is_available")]

    prop_name = prop.get("property_name", property_name)
    lines = [f"**Room availability — {prop_name}**", ""]

    if available:
        for r in available:
            lines.extend(_format_room(r, user_working_type, user_hometown))
            lines.append("")

    if occupied:
        lines.append(f"*{len(occupied)} room{'s' if len(occupied) > 1 else ''} currently occupied:*")
        lines.append("")
        for r in occupied:
            lines.extend(_format_room(r, user_working_type, user_hometown))
            lines.append("")

    return "\n".join(lines).strip()
