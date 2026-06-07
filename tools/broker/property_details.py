import httpx

from config import settings
from db.redis_store import get_property_info_map, set_property_info_map, track_funnel, get_user_brand
from utils.api import check_rentok_response, parse_amenities
from utils.properties import find_property as _find_property


TOOL_SCHEMA = {
    "name": "fetch_property_details",
    "description": "Get comprehensive details about a specific property: amenities (common/food/services), notice period, agreement terms, check-in/out times, GST, property rules, reviews, FAQs, AND a list of available room types with rent. Preferred first call for detail requests. Has a Redis cache fallback if the API returns sparse data. Different endpoint from fetch_room_details.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "property_name": {"type": "string", "description": "Exact name of the property as shown in search results"},
        },
        "required": ["property_name"],
    },
}


def _parse_api_response(data: dict) -> tuple[dict, dict, list]:
    """Parse property-details-bots response into (pd, ms, rooms).

    Handles two response shapes:
    - New (real API): data["data"]["property"] (214 fields) + data["data"]["propertyMicrosite"] (21 fields)
    - Legacy/flat: data["property_data"] or data["data"] as a flat dict

    Returns (property_dict, microsite_dict, rooms_list).
    """
    outer = data.get("property_data") or data.get("data") or {}
    pd = outer.get("property") or outer          # new API nests under "property"
    ms = outer.get("propertyMicrosite") or {}    # new API nests microsite under "propertyMicrosite"
    rooms = data.get("property_rooms") or data.get("rooms") or []
    return pd, ms, rooms


def _bool_to_yes_no(val) -> str:
    """Convert a bool/None to 'Yes'/'No'/'' for surfacing inclusion flags."""
    if val is True:
        return "Yes"
    if val is False:
        return "No"
    return ""


def _list_to_str(val, fallback: str = "") -> str:
    """Convert a list value to a comma-separated string; return val as-is if already a string."""
    if isinstance(val, list):
        return ", ".join(str(v) for v in val if v) or fallback
    return val or fallback



async def _fetch_details_raw(prop_id: str) -> dict:
    """Fetch raw property details dict from API. Used by compare_properties.

    Returns a flat normalized dict (legacy-compatible field names) on success, {} on failure.
    Unlike fetch_property_details(), this returns structured data, not a formatted string.

    IMPORTANT: prop_id must be the UUID (p_id / property_id from search results),
    NOT the Firebase pg_id — passing pg_id causes HTTP 500 from the API.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{settings.RENTOK_API_BASE_URL}/property/property-details-bots",
                json={"property_id": prop_id},
            )
            resp.raise_for_status()
            data = resp.json()
            pd, ms, _ = _parse_api_response(data)
            if not pd:
                return {}
            return {
                "property_name":     pd.get("pg_name") or pd.get("property_name", ""),
                "location":          ", ".join(filter(None, [
                                         pd.get("address_line_1") or pd.get("location") or pd.get("address", ""),
                                         pd.get("address_line_2", ""),
                                         pd.get("city", ""),
                                     ])),
                "rent_starts_from":  pd.get("rent_starts_from") or pd.get("rent", ""),
                # amenities: property_amenities from microsite is a dict of categories
                "amenities":         parse_amenities(ms.get("property_amenities")) or parse_amenities(pd.get("common_amenities")) or pd.get("amenities", ""),
                "common_amenities":  parse_amenities(ms.get("property_amenities")) or parse_amenities(pd.get("common_amenities", "")),
                "food_amenities":    pd.get("food_amenities", ""),
                "services_amenities": pd.get("services_amenities", ""),
                "property_type":     pd.get("property_type", ""),
                "tenants_preferred": pd.get("tenants_preferred", ""),
                "notice_period":     pd.get("notice_period", ""),
                "agreement_period":  pd.get("agreement_period", ""),
                "min_token_amount":  ms.get("min_token_amount") or pd.get("min_token_amount", ""),
                "microsite_url":     pd.get("microsite_url", ""),
                "property_rules":    _list_to_str(ms.get("property_rules")) or pd.get("property_rules", ""),
                "about":             ms.get("about") or pd.get("about", ""),
                "reviews":           ms.get("reviews") or pd.get("reviews", ""),
                "faqs":              ms.get("faqs") or pd.get("faqs", ""),
                "security_deposit":  ms.get("security_deposit", ""),
                "electricity_included": _bool_to_yes_no(
                    ms.get("is_electricity_included") if ms.get("is_electricity_included") is not None
                    else pd.get("is_electricity_included")
                ),
                "food_included":     _bool_to_yes_no(
                    ms.get("is_food_included") if ms.get("is_food_included") is not None
                    else pd.get("is_food_included")
                ),
            }
    except Exception:
        return {}


async def fetch_property_details(user_id: str, property_name: str, **kwargs) -> str:
    prop = _find_property(user_id, property_name)
    if not prop:
        return f"Property '{property_name}' not found. Please check the exact name from search results."

    # Use the UUID (prop_id / property_id) — NOT the Firebase pg_id.
    # Passing pg_id to property-details-bots causes HTTP 500 "invalid input syntax for type uuid".
    prop_id = prop.get("prop_id") or prop.get("property_id")
    if not prop_id:
        return "Property ID not available."

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{settings.RENTOK_API_BASE_URL}/property/property-details-bots",
                json={"property_id": prop_id},
            )
            resp.raise_for_status()
            data = resp.json()
            check_rentok_response(data, "property-details-bots")
    except Exception as e:
        return f"Error fetching property details: {str(e)}"

    pd, ms, rooms = _parse_api_response(data)

    # Fallback: API returned no meaningful data — use cached search data
    # New API uses pg_name; legacy flat responses used property_name
    property_name_val = pd.get("pg_name") or pd.get("property_name", "")
    if not pd or not property_name_val:
        return (
            f"Detailed info for '{prop.get('property_name', property_name)}' is currently unavailable. "
            f"Here's what we know: Location: {prop.get('property_location', 'N/A')}, "
            f"Rent starts from: {prop.get('property_rent', 'N/A')}, "
            f"Type: {prop.get('property_type', 'N/A')}. "
            f"Link: {prop.get('property_link', 'N/A')}"
        )

    location = ", ".join(filter(None, [
        pd.get("address_line_1") or pd.get("location") or pd.get("address", ""),
        pd.get("address_line_2", ""),
        pd.get("city", ""),
    ])) or prop.get("property_location", "")

    details = {
        "property_name":      property_name_val,
        "location":           location,
        "rent_starts_from":   pd.get("rent_starts_from") or pd.get("rent", ""),
        "amenities":          parse_amenities(ms.get("property_amenities")) or parse_amenities(pd.get("common_amenities")) or pd.get("amenities", ""),
        "room_amenities":     parse_amenities(ms.get("room_amenities")),
        "unit_types_available": pd.get("unit_types_available", ""),
        "property_type":      pd.get("property_type", ""),
        "tenants_preferred":  pd.get("tenants_preferred", ""),
        "notice_period":      pd.get("notice_period", ""),
        "agreement_period":   pd.get("agreement_period", ""),
        "checkin_time":       pd.get("checkin_time", ""),
        "checkout_time":      pd.get("checkout_time", ""),
        "locking_period":     pd.get("locking_period", ""),
        "gst_on_rent":        pd.get("gst_on_rent", ""),
        "security_deposit":   ms.get("security_deposit", ""),
        "property_rules":     _list_to_str(ms.get("property_rules")) or pd.get("property_rules", ""),
        "food_amenities":     pd.get("food_amenities", ""),
        "services_amenities": pd.get("services_amenities", ""),
        "electricity_included": _bool_to_yes_no(
            ms.get("is_electricity_included") if ms.get("is_electricity_included") is not None
            else pd.get("is_electricity_included")
        ),
        "food_included":      _bool_to_yes_no(
            ms.get("is_food_included") if ms.get("is_food_included") is not None
            else pd.get("is_food_included")
        ),
        "about":              ms.get("about") or pd.get("about") or pd.get("owner_description", ""),
        "reviews":            ms.get("reviews") or pd.get("reviews", ""),
        "faqs":               ms.get("faqs") or pd.get("faqs", ""),
        "google_map":         pd.get("google_map") or prop.get("google_map", ""),
        "microsite_url":      pd.get("microsite_url") or prop.get("property_link", ""),
        "min_token_amount":   ms.get("min_token_amount") or pd.get("min_token_amount", ""),
    }

    # Persist enriched details back to the property info map cache
    info_map = get_property_info_map(user_id)
    for p in info_map:
        if p.get("property_name", "").strip().lower() == details["property_name"].strip().lower():
            p.update({k: v for k, v in details.items() if v})
            break
    set_property_info_map(user_id, info_map)
    track_funnel(user_id, "detail", brand_hash=get_user_brand(user_id))

    result = f"PROPERTY DETAILS: {details['property_name']}\n"
    for key, val in details.items():
        if val and key not in ("property_name",):
            label = key.replace("_", " ").title()
            result += f"- {label}: {val}\n"

    if rooms:
        result += "\nAVAILABLE ROOMS:\n"
        for room in rooms[:10]:
            result += f"- {room.get('room_name', 'Room')}: {room.get('sharing_type', '')} sharing, Rent: {room.get('rent', 'N/A')}\n"

    return result
