"""
Structured property comparison: fetches details for 2-3 properties in parallel
and returns a structured comparison table with a recommendation.

Reduces comparison from 4+ LLM turns to 1 tool call + 1 response.
"""

import asyncio
import re

from core.log import get_logger
from core.signals import record_signal
from db.redis_store import get_preferences, get_user_memory
from tools.broker.property_details import _fetch_details_raw as _fetch_details
from tools.broker.room_details import _fetch_rooms_raw as _fetch_rooms
from utils.properties import find_property as _find_property
from utils.scoring import match_score as calc_match_score

logger = get_logger("tools.compare")


def _rent_value(raw):
    """Parse a numeric rent for best-cell comparison; None when not parseable."""
    if raw is None:
        return None
    m = re.search(r"\d[\d,]*", str(raw))
    return float(m.group(0).replace(",", "")) if m else None


def build_comparison_items(comparison: list[dict]) -> list[dict]:
    """Map the structured comparison[] onto the frontend's native comparison contract:
    items[] of {name, score, badge, attrs:[{label, value, best?}]}.

    The FE's native items[] path does NOT auto-compute best cells (only its legacy
    headers/rows path does), so best is set here — the lowest rent and the top match
    score. The top scorer also gets a "Best match" badge. Empty attributes are omitted
    so the card never shows blank rows; the renderer degrades on ragged attrs[].
    """
    if not comparison:
        return []
    top_name = max(comparison, key=lambda c: c.get("score", 0)).get("name")
    rents = {c.get("name"): _rent_value(c.get("rent")) for c in comparison}
    valid = [v for v in rents.values() if v is not None]
    # Only declare a cheapest when there is a real spread (≥2 values, not all equal).
    min_rent = min(valid) if len(valid) >= 2 and len(set(valid)) > 1 else None

    items = []
    for c in comparison:
        name = c.get("name", "Property")
        attrs = []
        if c.get("rent") not in (None, "", "N/A"):
            attrs.append({"label": "Rent", "value": f"₹{c['rent']}",
                          "best": min_rent is not None and rents.get(name) == min_rent})
        if c.get("location") and c["location"] != "N/A":
            attrs.append({"label": "Location", "value": c["location"]})
        attrs.append({"label": "Match", "value": f"{c.get('score', 0)}/100",
                      "best": name == top_name})
        if c.get("available_for"):
            attrs.append({"label": "For", "value": c["available_for"]})
        if c.get("distance"):
            attrs.append({"label": "Distance", "value": f"{c['distance']}m"})
        if c.get("amenities"):
            attrs.append({"label": "Amenities", "value": c["amenities"]})
        if c.get("token_amount"):
            attrs.append({"label": "Token", "value": f"₹{c['token_amount']}"})
        if c.get("total_beds"):
            attrs.append({"label": "Beds available", "value": str(c["total_beds"])})
        items.append({"name": name, "score": c.get("score", 0),
                      "badge": "Best match" if name == top_name else "", "attrs": attrs})
    return items

TOOL_SCHEMA = {
    "name": "compare_properties",
    "description": "Compare 2-3 properties side-by-side. Fetches details and rooms for all properties in parallel and returns a structured comparison with match scores and a recommendation. Use when user says 'compare', 'which is better', 'X vs Y'.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "property_names": {
                "type": "string",
                "description": "Comma-separated property names to compare (2-3 properties). E.g. 'Stanza Living, Zolo Stays'",
            },
        },
        "required": ["property_names"],
    },
}


async def compare_properties(
    user_id: str,
    property_names: str,
    **kwargs,
) -> str:
    """Compare 2-3 properties side-by-side with structured data + recommendation."""
    names = [n.strip() for n in property_names.split(",") if n.strip()]
    if len(names) < 2:
        return "Please provide at least 2 property names separated by commas to compare."
    if len(names) > 3:
        names = names[:3]

    # Resolve properties from info_map
    props = []
    for name in names:
        prop = _find_property(user_id, name)
        if not prop:
            return f"Property '{name}' not found in search results. Please check the exact name."
        props.append(prop)

    # Fetch details + rooms in parallel for all properties
    tasks = []
    for prop in props:
        prop_id = prop.get("prop_id") or prop.get("property_id", "")
        eazypg_id = prop.get("eazypg_id", "")
        tasks.append(_fetch_details(prop_id))
        tasks.append(_fetch_rooms(eazypg_id))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            logger.warning("compare fetch task %d failed: %s", i, r)
            results[i] = {} if i % 2 == 0 else []  # details={}, rooms=[]

    # Build comparison data
    prefs = get_preferences(user_id)
    user_mem = get_user_memory(user_id)
    deal_breakers = user_mem.get("deal_breakers", [])

    comparison = []
    for i, prop in enumerate(props):
        details = results[i * 2] or {}
        rooms = results[i * 2 + 1] or []

        # Merge search data + API details
        name = details.get("property_name") or prop.get("property_name", "Property")
        location = details.get("location") or details.get("address") or prop.get("property_location", "N/A")
        rent = details.get("rent_starts_from") or prop.get("property_rent", "N/A")
        amenities = details.get("common_amenities") or details.get("amenities") or ""
        food = details.get("food_amenities", "")
        services = details.get("services_amenities", "")
        prop_type = details.get("property_type") or prop.get("property_type", "")
        available_for = details.get("tenants_preferred") or prop.get("pg_available_for", "")
        notice = details.get("notice_period", "")
        agreement = details.get("agreement_period", "")
        token = details.get("min_token_amount") or prop.get("min_token_amount", "")
        maps_link = prop.get("google_map", "")
        microsite = details.get("microsite_url") or prop.get("property_link", "")
        distance = prop.get("distance", "")

        # Room summary
        room_summary = []
        total_beds = 0
        for room in rooms[:5]:
            rname = room.get("room_name", room.get("name", "Room"))
            sharing = room.get("sharing_type", "")
            beds = room.get("beds_available", room.get("available", "?"))
            room_rent = room.get("rent", "N/A")
            room_summary.append(f"{rname}: {sharing} sharing, ₹{room_rent}, {beds} beds available")
            try:
                total_beds += int(beds)
            except (ValueError, TypeError):
                pass  # beds_available may be "?" or empty string — skip, don't crash

        # Custom match score
        prop_data = {
            "rent": rent,
            "distance": distance,
            "amenities": amenities,
            "property_type": prop_type,
            "pg_available_for": available_for,
        }
        scoring_prefs = {
            "min_budget": prefs.get("min_budget", 0),
            "max_budget": prefs.get("max_budget", 100000),
            "amenities": prefs.get("amenities", ""),
            "must_have_amenities": prefs.get("must_have_amenities", ""),
            "nice_to_have_amenities": prefs.get("nice_to_have_amenities", ""),
            "property_type": prefs.get("property_type", ""),
            "pg_available_for": prefs.get("pg_available_for", ""),
        }
        score = calc_match_score(prop_data, scoring_prefs, deal_breakers=deal_breakers)

        comparison.append({
            "name": name,
            "location": location,
            "rent": rent,
            "score": score,
            "amenities": amenities,
            "food": food,
            "services": services,
            "type": prop_type,
            "available_for": available_for,
            "notice_period": notice,
            "agreement_period": agreement,
            "token_amount": token,
            "distance": distance,
            "rooms": room_summary,
            "total_beds": total_beds,
            "maps_link": maps_link,
            "microsite": microsite,
        })

    # Record the structured comparison so the egress emits a native comparison unit
    # (the FE renders the side-by-side / carousel table). The prose `output` below is
    # still returned to the LLM so it can reason and write a short recommendation.
    try:
        record_signal(comparison_items=build_comparison_items(comparison))
    except Exception as e:
        logger.warning("comparison signal record failed: %s", e)

    # Build structured comparison output
    output = "PROPERTY COMPARISON\n" + "=" * 50 + "\n\n"

    for c in comparison:
        output += f"📍 {c['name']}\n"
        output += f"   Location: {c['location']}\n"
        output += f"   Rent starts from: ₹{c['rent']}\n"
        output += f"   Match Score: {c['score']}/100\n"
        output += f"   Type: {c['type']} | For: {c['available_for']}\n"
        if c['distance']:
            output += f"   Distance: {c['distance']}m\n"
        if c['amenities']:
            output += f"   Amenities: {c['amenities']}\n"
        if c['food']:
            output += f"   Food: {c['food']}\n"
        if c['services']:
            output += f"   Services: {c['services']}\n"
        if c['notice_period']:
            output += f"   Notice Period: {c['notice_period']}\n"
        if c['token_amount']:
            output += f"   Token Amount: ₹{c['token_amount']}\n"
        if c['rooms']:
            output += f"   Rooms ({c['total_beds']} beds total):\n"
            for r in c['rooms']:
                output += f"     • {r}\n"
        if c['maps_link']:
            output += f"   Map: {c['maps_link']}\n"
        if c['microsite']:
            output += f"   Link: {c['microsite']}\n"
        output += "\n"

    # Recommendation
    best = max(comparison, key=lambda x: x["score"])
    output += "=" * 50 + "\n"
    output += f"RECOMMENDATION: {best['name']} (score: {best['score']}/100)\n"
    output += "Use this data to explain WHY this property is the best fit. Consider rent, amenities, distance, and the user's specific needs.\n"

    return output
