import httpx

from config import settings
from utils.api import parse_amenities, parse_sharing_types
from utils.properties import find_property


TOOL_SCHEMA = {
    "name": "fetch_room_details",
    "description": "Get room configurations for a property: room name, sharing type, and rent per room. Uses a different API endpoint from fetch_property_details. Call alongside fetch_property_details for a complete room picture. NOTE: this returns room layouts, not live per-bed availability — for confirmed vacancy the user should schedule a visit. Falls back to search cache data (sharing types, amenities, rent) if no rooms are returned.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "property_name": {"type": "string", "description": "Exact property name"},
        },
        "required": ["property_name"],
    },
}

# Ground truth (RentOk backend, verified 2026-05-31): rooms come from
# POST /bookingBot/get-room-details (json={"eazypg_id": ...}). The bot's old
# GET /bookingBot/getAvailableRoomFromEazyPGID route does NOT exist (real 404).
# Response wrapper is {status, message, data:{ rooms:[...], pg_name, ... }} — the
# room list is nested at data.data.rooms, NOT data.rooms. Unknown eazypg_id →
# HTTP 200 with body status 404 and data:{}. Each room has: id, name, rent,
# tags, type_tags, sharing_type — there is no beds_available / live bed count.
_ROOM_DETAILS_URL = f"{settings.RENTOK_API_BASE_URL}/bookingBot/get-room-details"


def _extract_rooms(data: dict) -> list:
    """Pull the room list out of the get-room-details envelope (data.data.rooms)."""
    inner = data.get("data") or {}
    if isinstance(inner, dict):
        return inner.get("rooms", [])
    return []


async def _fetch_rooms_raw(eazypg_id: str) -> list:
    """Fetch raw room list from API. Used by compare_properties.

    Returns a list of room dicts on success, [] on any failure or missing ID.
    Unlike fetch_room_details(), this returns structured data, not a
    formatted string — callers are responsible for rendering.
    """
    if not eazypg_id:
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(_ROOM_DETAILS_URL, json={"eazypg_id": eazypg_id})
            resp.raise_for_status()
            return _extract_rooms(resp.json())
    except Exception:
        return []


async def fetch_room_details(user_id: str, property_name: str, **kwargs) -> str:
    prop = find_property(user_id, property_name)
    if not prop:
        return f"Property '{property_name}' not found."

    eazypg_id = prop.get("eazypg_id", "")
    if not eazypg_id:
        return "Property EazyPG ID not available."

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(_ROOM_DETAILS_URL, json={"eazypg_id": eazypg_id})
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return f"Error fetching room details: {str(e)}"

    rooms = _extract_rooms(data)
    if not rooms:
        sharing_types = prop.get("sharing_types", [])
        amenities_raw = prop.get("amenities", "")
        rent = prop.get("property_rent", "")
        sharing_str = parse_sharing_types(sharing_types)
        amenities_str = parse_amenities(amenities_raw)
        if sharing_str or amenities_str:
            name = prop.get("property_name", property_name)
            result = f"Room details for '{name}' aren't showing right now. From our listings:\n"
            if sharing_str:
                result += f"- Sharing options: {sharing_str}\n"
            if amenities_str:
                result += f"- Amenities: {amenities_str}\n"
            if rent:
                result += f"- Rent starts from: ₹{rent}/mo\n"
            result += "For confirmed availability, schedule a visit or call the property directly."
            return result
        return f"No room data available for '{property_name}'. Schedule a visit to check in person."

    result = f"Rooms at '{prop.get('property_name', property_name)}':\n"
    for room in rooms:
        name = room.get("name") or room.get("room_name") or room.get("room_type") or "Room"
        sharing = room.get("sharing_type", "")
        rent = room.get("rent", "")
        line = f"- {name}"
        if sharing:
            line += f": {sharing} sharing"
        if rent:
            line += f", Rent: ₹{rent}/mo"
        result += line + "\n"
    result += "(Room layouts — for confirmed bed availability, schedule a visit.)"
    return result
