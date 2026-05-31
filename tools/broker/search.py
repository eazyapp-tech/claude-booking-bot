import asyncio
import hashlib
import json as _json

import httpx

from config import settings
from core.log import get_logger

logger = get_logger("tools.search")
from db.redis_store import (
    get_preferences,
    get_property_info_map,
    set_property_info_map,
    set_property_id_for_search,
    set_last_search_results,
    save_property_template,
    get_whitelabel_pg_ids,
    save_preferences as redis_save_preferences,
    track_funnel,
    record_property_viewed,
    update_user_memory,
    get_user_memory,
    get_user_brand,
    track_property_event,
    _r as _redis,
)
from utils.api import parse_amenities, parse_sharing_types
from utils.geo import geocode_address
from utils.scoring import match_score as calc_match_score


SEARCH_CACHE_TTL = 900  # 15 minutes

TOOL_SCHEMA = {
    "name": "search_properties",
    "description": "Search for properties based on saved preferences. Returns up to 20 properties with name, location, rent, images, and match scores. Show 5 at a time.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "radius_flag": {"type": "boolean", "description": "Set true to expand search radius by 5km"},
        },
        "required": [],
    },
}


def _search_cache_key(payload: dict) -> str:
    """Deterministic cache key from search payload."""
    # Sort keys for consistency; remove non-deterministic fields
    stable = _json.dumps(payload, sort_keys=True, default=str)
    return f"search_cache:{hashlib.md5(stable.encode()).hexdigest()}"


def _get_search_cache(payload: dict) -> list | None:
    """Return cached search results or None on miss."""
    key = _search_cache_key(payload)
    raw = _redis().get(key)
    if raw is None:
        return None
    try:
        data = _json.loads(raw)
        logger.info("cache HIT (%s): %d results", key[-12:], len(data))
        return data
    except Exception as e:
        logger.debug("search cache decode failed for %s: %s", key[-12:], e)
        return None


def _set_search_cache(payload: dict, results: list) -> None:
    """Store search results in Redis with TTL."""
    key = _search_cache_key(payload)
    try:
        _redis().setex(key, SEARCH_CACHE_TTL, _json.dumps(results, default=str))
        logger.debug("cache SET (%s): %d results, TTL=%ds", key[-12:], len(results), SEARCH_CACHE_TTL)
    except Exception as e:
        logger.warning("cache SET failed: %s", e)



async def _geocode_properties(properties: list[dict], limit: int = 5) -> None:
    """Geocode property addresses concurrently to fill in missing lat/lng.
    Mutates the property dicts in-place.  Only processes the first `limit` entries
    that don't already have coordinates."""
    import asyncio

    async def _geocode_one(p: dict) -> None:
        addr = ", ".join(filter(None, [
            p.get("p_address_line_1", ""),
            p.get("p_address_line_2", ""),
            p.get("p_city", ""),
        ]))
        if not addr:
            addr = p.get("p_pg_name", "")
        if not addr:
            return
        lat, lng = await geocode_address(addr)
        if lat and lng:
            p["_geocoded_lat"] = str(lat)
            p["_geocoded_lng"] = str(lng)

    # Only geocode properties that are missing coordinates
    to_geocode = []
    for p in properties[:limit]:
        has_lat = any(p.get(k) for k in ("p_latitude", "p_lat", "p_pg_latitude", "latitude", "lat"))
        has_lng = any(p.get(k) for k in ("p_longitude", "p_long", "p_pg_longitude", "longitude", "long", "lng"))
        if not has_lat or not has_lng:
            to_geocode.append(p)

    if not to_geocode:
        return

    logger.info("geocoding %d properties (missing lat/lng)", len(to_geocode))
    geo_results = await asyncio.gather(*[_geocode_one(p) for p in to_geocode], return_exceptions=True)
    for i, r in enumerate(geo_results):
        if isinstance(r, Exception):
            logger.warning("geocode failed for property %d: %s", i, r)


async def _call_search_api(payload: dict) -> list:
    """Call Rentok search API and return raw properties list. Uses Redis cache."""
    # Rentok API requires pg_ids to be a non-empty array.
    if not payload.get("pg_ids"):
        logger.warning("pg_ids is empty — API will return no results. Ensure account_values.pg_ids is configured.")
        return []

    # Check cache first
    cached = _get_search_cache(payload)
    if cached is not None:
        return cached

    from utils.retry import http_post

    try:
        data = await http_post(
            f"{settings.RENTOK_API_BASE_URL}/property/getPropertyDetailsAroundLatLong",
            json=payload,
            timeout=30,
        )
        # Check for inner error (API returns 200 but data.status may be 500)
        inner = data.get("data", {})
        if inner.get("status") == 500:
            logger.error("API inner error: %s — %s", inner.get("message", ""), inner.get("data", {}).get("error", ""))
            return []
        results = inner.get("data", {}).get("results", [])
        logger.info("search API: %d results", len(results))

        # Cache successful non-empty results
        if results:
            _set_search_cache(payload, results)

        return results
    except Exception as e:
        logger.error("search API error: %s", e)
        return []


async def _fetch_first_image(client: httpx.AsyncClient, pg_id: str, pg_number: str) -> str:
    """Fetch the first image URL for a property. Returns '' on any failure."""
    if not pg_id or not pg_number:
        return ""
    try:
        resp = await client.post(
            f"{settings.RENTOK_API_BASE_URL}/bookingBot/fetchPropertyImages",
            json={"pg_id": pg_id, "pg_number": pg_number},
        )
        resp.raise_for_status()
        data = resp.json()
        images = data.get("images", data.get("data", []))
        if images:
            first = images[0]
            return first.get("url", first.get("media_id", "")) if isinstance(first, dict) else str(first)
    except Exception as e:
        logger.debug("image fetch failed for pg_id=%s: %s", pg_id, e)
    return ""


async def _enrich_with_images(properties: list, limit: int = 5) -> None:
    """Concurrently fetch first image for properties missing p_image. Mutates in place."""
    targets = []
    for i, p in enumerate(properties[:limit]):
        if not p.get("p_image") and not p.get("image"):
            targets.append((i, p.get("p_pg_id", ""), p.get("p_pg_number", "")))

    if not targets:
        logger.debug("image enrichment: all %d have images, skipping", min(len(properties), limit))
        return

    logger.info("image enrichment: fetching images for %d properties", len(targets))
    async with httpx.AsyncClient(timeout=8) as client:
        tasks = [_fetch_first_image(client, pg_id, pg_num) for _, pg_id, pg_num in targets]
        urls = await asyncio.gather(*tasks, return_exceptions=True)

    enriched = 0
    for (idx, _, _), url in zip(targets, urls):
        if isinstance(url, Exception):
            logger.warning("image fetch failed for property at idx %d: %s", idx, url)
            continue
        if url:
            properties[idx]["p_image"] = url
            enriched += 1
    logger.info("image enrichment: %d/%d images found", enriched, len(targets))


async def search_properties(user_id: str, radius_flag: bool = False, **kwargs) -> str:
    prefs = get_preferences(user_id)
    if not prefs.get("location"):
        return "No location set. Please save preferences with a location first."

    location = prefs.get("location", "")
    min_budget = prefs.get("min_budget", 0)
    max_budget = prefs.get("max_budget", 100000)
    amenities = prefs.get("amenities", "")
    property_type = prefs.get("property_type")
    unit_types = prefs.get("unit_types_available")
    pg_available_for = prefs.get("pg_available_for")
    sharing_types = prefs.get("sharing_types_enabled")
    radius = prefs.get("radius", 20000)

    if radius_flag:
        radius = min(radius + 5000, 35000)
        prefs["radius"] = radius
        redis_save_preferences(user_id, prefs)

    # Step 1: Geocode location to lat/long
    lat, lng = await geocode_address(location)
    if lat is None or lng is None:
        return f"Could not find coordinates for '{location}'. Please try a more specific area or city name."

    logger.info("geocoded '%s' → lat=%s, lng=%s", location, lat, lng)

    # Persist search center for map view
    prefs["search_lat"] = str(lat) if lat else ""
    prefs["search_lng"] = str(lng) if lng else ""
    redis_save_preferences(user_id, prefs)

    # Step 2: Get PG IDs
    pg_ids = get_whitelabel_pg_ids(user_id)

    # Step 3: Build payload matching the original API format
    payload = {
        "coords": [[lat, lng]],
        "radius": radius,
        "rent_ends_to": max_budget if max_budget else 10000000,
        "pg_ids": pg_ids,
    }
    if min_budget:
        payload["rent_starts_from"] = min_budget
    if unit_types:
        payload["unit_types_available"] = unit_types
    if pg_available_for and pg_available_for in ["All Boys", "All Girls"]:
        payload["pg_available_for"] = pg_available_for
    if sharing_types:
        payload["sharing_type_enabled"] = sharing_types

    logger.debug("search payload: %s", payload)

    # Step 4: Search with progressive relaxation — surface MORE results
    MIN_RESULTS_THRESHOLD = 5

    properties = await _call_search_api(payload)
    relaxed_note = ""
    logger.info("initial query returned %d results", len(properties))

    if len(properties) < MIN_RESULTS_THRESHOLD:
        # Round 1: expand radius + triple budget, drop gender/sharing filters
        r1_payload = {
            "coords": [[lat, lng]],
            "radius": 35000,
            "rent_ends_to": max(max_budget * 3, 300000) if max_budget else 10000000,
            "pg_ids": pg_ids,
        }
        if unit_types:
            r1_payload["unit_types_available"] = unit_types
        logger.debug("relaxation round 1 payload: %s", r1_payload)
        r1_results = await _call_search_api(r1_payload)
        logger.info("relaxation round 1 returned %d results", len(r1_results))

        if len(r1_results) > len(properties):
            seen_ids = {p.get("p_id", p.get("prop_id")) for p in properties}
            for p in r1_results:
                pid = p.get("p_id", p.get("prop_id"))
                if pid not in seen_ids:
                    properties.append(p)
                    seen_ids.add(pid)
            relaxed_note = "[RELAXED: expanded area, flexible budget] "
        logger.info("after round 1 merge: %d total", len(properties))

    if len(properties) < MIN_RESULTS_THRESHOLD:
        # Round 2: drop ALL filters — just coords + pg_ids + wide radius
        r2_payload = {
            "coords": [[lat, lng]],
            "radius": 50000,
            "rent_ends_to": 10000000,
            "pg_ids": pg_ids,
        }
        logger.debug("relaxation round 2 payload: %s", r2_payload)
        r2_results = await _call_search_api(r2_payload)
        logger.info("relaxation round 2 returned %d results", len(r2_results))

        if len(r2_results) > len(properties):
            seen_ids = {p.get("p_id", p.get("prop_id")) for p in properties}
            for p in r2_results:
                pid = p.get("p_id", p.get("prop_id"))
                if pid not in seen_ids:
                    properties.append(p)
                    seen_ids.add(pid)
            relaxed_note = "[RELAXED: showing all nearby properties] "
        logger.info("after round 2 merge: %d total", len(properties))

    if not properties:
        return "No properties are currently available in this region."

    logger.info("found %d properties", len(properties))

    # Enrich top results with images from dedicated images API
    await _enrich_with_images(properties, limit=5)

    # Geocode top properties to get lat/lng for map view
    await _geocode_properties(properties, limit=5)

    # Re-score with custom scoring (weighted amenities + deal-breaker penalties)
    user_mem = get_user_memory(user_id)
    deal_breakers = user_mem.get("deal_breakers", [])
    scoring_prefs = {
        "min_budget": min_budget,
        "max_budget": max_budget,
        "amenities": amenities,
        "must_have_amenities": prefs.get("must_have_amenities", ""),
        "nice_to_have_amenities": prefs.get("nice_to_have_amenities", ""),
        "property_type": property_type or "",
        "pg_available_for": pg_available_for or "",
    }
    # Load property outcome signals for scoring (Sprint 5)
    try:
        from db.redis.analytics import get_property_signals
    except ImportError:
        get_property_signals = None

    for p in properties:
        prop_data = {
            "rent": p.get("p_rent_starts_from", p.get("rent", 0)),
            "distance": p.get("p_distance", p.get("distance")),
            "amenities": p.get("p_common_amenities", p.get("p_amenities", "")),
            "property_type": p.get("p_property_type", ""),
            "pg_available_for": p.get("p_pg_available_for", ""),
        }
        # Fetch outcome signals for this property (fire-and-forget on failure)
        signals = {}
        if get_property_signals:
            try:
                pid = p.get("p_property_id", p.get("property_id", ""))
                if pid:
                    signals = get_property_signals(pid)
            except Exception:
                pass
        p["_custom_score"] = calc_match_score(prop_data, scoring_prefs, deal_breakers=deal_breakers, property_signals=signals)

    # Sort by custom score (descending) to surface best matches first
    properties.sort(key=lambda p: p.get("_custom_score", 0), reverse=True)

    existing_map = get_property_info_map(user_id)
    # Build index for fast dedup by prop_id → position in existing_map
    _existing_idx = {}
    for _i, _e in enumerate(existing_map):
        _eid = _e.get("prop_id") or _e.get("property_id")
        if _eid:
            _existing_idx[_eid] = _i

    property_template = []

    results = []
    for p in properties[:20]:
        property_name = p.get("p_pg_name", p.get("property_name", "Property"))
        address = ", ".join(filter(None, [
            p.get("p_address_line_1", ""),
            p.get("p_address_line_2", ""),
            p.get("p_city", ""),
        ]))
        rent = p.get("p_rent_starts_from", p.get("rent", ""))
        available_for = p.get("p_pg_available_for", "Any")
        prop_type = p.get("p_property_type", "")
        prop_id = p.get("p_id", p.get("prop_id", ""))
        pg_id = p.get("p_pg_id", "")
        pg_number = p.get("p_pg_number", "")
        eazypg_id = p.get("p_eazypg_id", "")
        image = p.get("p_image", p.get("image", ""))
        distance = p.get("p_distance", p.get("distance", ""))
        lat_val = (p.get("p_latitude") or p.get("p_lat") or p.get("p_pg_latitude")
                   or p.get("latitude") or p.get("lat")
                   or p.get("_geocoded_lat") or "")
        long_val = (p.get("p_longitude") or p.get("p_long") or p.get("p_pg_longitude")
                    or p.get("longitude") or p.get("long") or p.get("lng")
                    or p.get("_geocoded_lng") or "")
        phone = p.get("p_phone_number", "")
        min_token = p.get("p_min_token_amount", 1000)
        microsite_url = p.get("p_microsite_url", p.get("microsite_url", ""))
        match_score = p.get("_custom_score", p.get("p_match_score", p.get("match_score", "")))
        # Normalise at write time → cache always contains clean strings,
        # no downstream tool needs to know the raw API shape.
        amenities_raw = parse_amenities(p.get("p_common_amenities", p.get("p_amenities", "")))
        sharing_types_data = parse_sharing_types(p.get("p_sharing_types_enabled", []))

        info = {
            "property_name": property_name,
            "property_location": address,
            "property_rent": str(rent),
            "pg_available_for": available_for,
            "property_type": prop_type,
            "property_image": image,
            "prop_id": prop_id,
            "property_id": prop_id,                    # alias for booking tools
            "pg_id": pg_id,
            "pg_number": pg_number,
            "eazypg_id": eazypg_id,
            "property_link": microsite_url,
            "google_map": f"https://www.google.com/maps?q={lat_val},{long_val}" if lat_val and long_val else "",
            "match_score": match_score,
            "distance": distance,
            "property_lat": lat_val,
            "property_long": long_val,
            "phone_number": phone,
            "min_token_amount": min_token,
            "property_min_token_amount": min_token,    # alias for payment tool
            "amenities": amenities_raw,
            "sharing_types": sharing_types_data,
        }
        # Replace old entry for same property (dedup) or append new
        if prop_id and prop_id in _existing_idx:
            existing_map[_existing_idx[prop_id]] = info
        else:
            existing_map.append(info)
            if prop_id:
                _existing_idx[prop_id] = len(existing_map) - 1
        property_template.append(info)

        results.append(
            f"- {property_name} | {address} | "
            f"Rent starts from: {rent} | For: {available_for} | "
            f"Match: {match_score} | Distance: {distance} | "
            f"Image: {image} | Link: {microsite_url}"
        )

    set_property_info_map(user_id, existing_map)

    # Save pg_ids for KB doc injection in broker agent (uses brand-config pg_id, not Rentok UUID)
    kb_ids = [info["pg_id"] for info in property_template[:5] if info.get("pg_id")]
    if kb_ids:
        set_property_id_for_search(user_id, kb_ids)

    # Cache summary of top-10 results for cross-session context (24h TTL)
    set_last_search_results(user_id, [
        {
            "property_name": p.get("property_name", ""),
            "pg_id": p.get("pg_id", ""),
            "property_rent": p.get("property_rent", ""),
            "property_location": p.get("property_location", ""),
        }
        for p in existing_map[:10]
    ])

    save_property_template(user_id, property_template[:5])
    track_funnel(user_id, "search", brand_hash=get_user_brand(user_id))

    # Update cross-session memory + property-level analytics
    brand_hash_val = get_user_brand(user_id)
    for info in property_template[:5]:
        pid = info.get("prop_id", "")
        record_property_viewed(user_id, pid)
        try:
            track_property_event(pid, "viewed", brand_hash=brand_hash_val)
        except Exception:
            pass
    budget_str = ""
    if min_budget or max_budget:
        budget_str = f"₹{min_budget}-{max_budget}" if min_budget else f"up to ₹{max_budget}"
    update_user_memory(
        user_id,
        last_search_location=location,
        last_search_budget=budget_str,
    )

    from core.untrusted import fence
    return (
        f"{relaxed_note}Found {len(properties)} properties. Here are the results:\n"
        + fence("\n".join(results), "property listing data from the Rentok API")
    )
