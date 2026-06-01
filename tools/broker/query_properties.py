import httpx

from config import settings
from db.redis.property import get_property_info_map, set_property_info_map
from db.redis_store import get_whitelabel_pg_ids
from utils.api import RentokAPIError, check_rentok_response


TOOL_SCHEMA = {
    "name": "fetch_properties_by_query",
    "description": "Fetch properties matching a text query/name.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string", "description": "Property name or search query"},
        },
        "required": ["query"],
    },
}


async def fetch_properties_by_query(user_id: str, query: str, **kwargs) -> str:
    pg_ids = get_whitelabel_pg_ids(user_id)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{settings.RENTOK_API_BASE_URL}/bookingBot/fetch-all-properties",
                json={"pg_ids": pg_ids, "search_query": query},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return f"Error fetching properties: {str(e)}"

    try:
        check_rentok_response(data, "fetch-all-properties")
    except RentokAPIError as e:
        return f"Error fetching properties: {e}"

    # fetch-all-properties returns data at top-level "data" key (array of property objects).
    # Each object uses "pg_name" (not "property_name" or "name") for the property name,
    # and "id" (UUID) / "pg_id" (Firebase UID) for identifiers.
    properties = data.get("data", data.get("properties", []))
    if not properties:
        return f"No properties matching '{query}' found."

    # Map API response shape → cache shape used by find_property and all booking tools.
    # find_property looks up "property_name"; booking tools read eazypg_id, personal_contact, pg_id, id.
    def _to_cache_shape(p: dict) -> dict:
        ms_data = p.get("microsite_data") or {}
        # personal_contact (operator's private number) is not returned by this
        # unauthenticated endpoint. Use the public-facing customer_support_number
        # as the property_contact key for the shortlist API instead.
        contact = ms_data.get("customer_support_number", "") or ms_data.get("customer_support_whatsapp", "")
        return {
            "property_name": p.get("pg_name", ""),
            "pg_id": p.get("pg_id", ""),
            "id": p.get("id", ""),
            "eazypg_id": p.get("eazypg_id", ""),
            "phone_number": contact,
            "pg_available_for": p.get("pg_available_for", ""),
            "rent_starts_from": p.get("rent_starts_from"),
            "address_line_1": p.get("address_line_1", ""),
            "address_line_2": p.get("address_line_2", ""),
            "microsite_link": p.get("microsite_link", ""),
            "common_amenities": ms_data.get("common_amenities", []),
        }

    new_entries = [_to_cache_shape(p) for p in properties]

    # Merge into existing cache (keyed by pg_id) so a name search doesn't wipe
    # properties the user already found via location search.
    existing = get_property_info_map(user_id)
    existing_by_pg_id = {e["pg_id"]: e for e in existing if e.get("pg_id")}
    for entry in new_entries:
        existing_by_pg_id[entry["pg_id"]] = entry
    set_property_info_map(user_id, list(existing_by_pg_id.values()))

    results = []
    for p in properties[:5]:
        name = p.get("pg_name", "")
        rent = p.get("rent_starts_from")
        addr = " ".join(filter(None, [p.get("address_line_1"), p.get("address_line_2")]))
        gender = p.get("pg_available_for", "")
        rent_str = f"₹{rent}/mo" if rent else "rent N/A"
        line = f"- {name} | {rent_str} | {gender} | {addr}"
        results.append(line)

    return f"Properties matching '{query}':\n" + "\n".join(results)
