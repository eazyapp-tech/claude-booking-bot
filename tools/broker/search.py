import asyncio
import hashlib
import json as _json
import re

import httpx

from config import settings
from core.log import get_logger
from core.signals import record_signal

logger = get_logger("tools.search")
from db.redis_store import (
    get_preferences,
    get_property_info_map,
    set_property_info_map,
    set_property_id_for_search,
    set_last_search_results,
    save_property_template,
    set_search_carousel,
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
from utils.api import parse_amenities, parse_sharing_types, parse_sharing_types_structured
from utils.geo import geocode_address
from utils.retry import http_get
from utils.scoring import match_score as calc_match_score, gender_compatible_listing


SEARCH_CACHE_TTL = 900  # 15 minutes

# R1 — commute-based ranking. Re-rank at most this many top (by area) candidates by
# real driving time to the user's daily destination, in ONE OSRM matrix call. Bounds
# the URL length / compute; properties beyond this window keep their area ranking.
COMMUTE_RANK_TOPN = 10
COMMUTE_RANK_TIMEOUT_S = 12  # tight: one matrix call should be fast (Instant truth)


def _fmt_rent(raw) -> str:
    """Format a raw rent value into the '₹9,000/mo' display string the FE card/sheet
    expect (today's carousel value is prose-derived). Non-numeric input passes through
    verbatim ('On request')."""
    s = str(raw).strip()
    digits = re.sub(r"[^\d]", "", s)
    return f"₹{int(digits):,}/mo" if digits else s


def build_carousel_items(info_list, search_lat, search_lng, limit: int = 5):
    """Build native property-carousel items (+ map_center) from the structured `info`
    dicts search builds (the set_property_info_map payload). Byte-compatible with
    message_parser._build_carousel_parts so the live FE card + detail sheet render
    identically — but sourced from structured data instead of regex-scraped prose.

    Returns (items, map_center | None). Pure: no Redis/network/LLM.
    """
    items = []
    for info in (info_list or [])[:limit]:
        item = {
            "name": info.get("property_name", ""),
            "location": info.get("property_location", ""),
            "rent": _fmt_rent(info.get("property_rent", "")),
            "gender": info.get("pg_available_for", ""),
            "distance": str(info.get("distance", "") or ""),
            "image": info.get("property_image", ""),
            "link": info.get("property_link", ""),
            "lat": str(info.get("property_lat", "") or ""),
            "lng": str(info.get("property_long", "") or ""),
            "score": "",
            "amenities": info.get("amenities", "") or "",
        }
        raw_score = info.get("match_score", "")
        if raw_score not in ("", None):
            try:
                item["score"] = str(round(float(raw_score)))
            except (ValueError, TypeError):
                pass  # non-numeric score → leave ""
        # Sheet-only enrichment (multi-image gallery + structured sharing) — additive,
        # included ONLY when present (mirrors message_parser._sheet_enrichment, which
        # returns {} for legacy cache entries so the sheet degrades gracefully).
        images = info.get("images")
        if images:
            item["images"] = images
        sharing = info.get("sharing_types_list")
        if sharing:
            item["sharing"] = sharing
        # R1 — real commute label, shown on the card only when computed this search.
        cmin = info.get("commute_minutes")
        clabel = info.get("commute_label")
        if cmin is not None and clabel:
            item["commute"] = f"{int(cmin)} min to {clabel}"
        items.append(item)

    map_center = None
    try:
        if search_lat not in ("", None) and search_lng not in ("", None):
            map_center = {"lat": float(search_lat), "lng": float(search_lng)}
    except (ValueError, TypeError):
        map_center = None
    if not map_center:
        coords = []
        for it in items:
            try:
                if it["lat"] and it["lng"]:
                    coords.append((float(it["lat"]), float(it["lng"])))
            except (ValueError, TypeError):
                continue
        if coords:
            map_center = {
                "lat": sum(c[0] for c in coords) / len(coords),
                "lng": sum(c[1] for c in coords) / len(coords),
            }
    return items, map_center

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


async def _call_search_api(payload: dict) -> list | None:
    """Call Rentok search API and return raw properties list. Uses Redis cache.

    Returns None on a *hard failure* (the API could not be reached, raised, or
    returned an internal error) so callers can tell "we couldn't get an answer"
    apart from a genuine empty result set. Returns a list (possibly empty) only
    when the API actually answered.
    """
    # Rentok API requires pg_ids to be a non-empty array — without them we
    # cannot run a meaningful search, so this is a failure, not "no inventory".
    if not payload.get("pg_ids"):
        logger.warning("pg_ids is empty — cannot search. Ensure account_values.pg_ids is configured.")
        return None

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
            return None
        results = inner.get("data", {}).get("results", [])
        logger.info("search API: %d results", len(results))

        # Cache successful non-empty results
        if results:
            _set_search_cache(payload, results)

        return results
    except Exception as e:
        logger.error("search API error: %s", e)
        return None


async def _fetch_images(client: httpx.AsyncClient, pg_id: str, pg_number: str) -> list:
    """Fetch ALL image URLs for a property (same call that backs the single cover —
    the full list is already returned, we just stop discarding it so the detail sheet
    can show a real gallery). Returns [] on any failure."""
    if not pg_id or not pg_number:
        return []
    try:
        resp = await client.post(
            f"{settings.RENTOK_API_BASE_URL}/bookingBot/fetchPropertyImages",
            json={"pg_id": pg_id, "pg_number": pg_number},
        )
        resp.raise_for_status()
        data = resp.json()
        images = data.get("images", data.get("data", []))
        urls = []
        for im in images:
            url = im.get("url", im.get("media_id", "")) if isinstance(im, dict) else str(im)
            if url:
                urls.append(url)
        return urls
    except Exception as e:
        logger.debug("image fetch failed for pg_id=%s: %s", pg_id, e)
    return []


async def _enrich_with_images(properties: list, limit: int = 5) -> None:
    """Concurrently fetch images for properties missing p_image. Mutates in place:
    sets p_image (cover = first url) AND _images (full gallery list for the sheet)."""
    targets = []
    for i, p in enumerate(properties[:limit]):
        if not p.get("p_image") and not p.get("image"):
            targets.append((i, p.get("p_pg_id", ""), p.get("p_pg_number", "")))

    if not targets:
        logger.debug("image enrichment: all %d have images, skipping", min(len(properties), limit))
        return

    logger.info("image enrichment: fetching images for %d properties", len(targets))
    async with httpx.AsyncClient(timeout=8) as client:
        tasks = [_fetch_images(client, pg_id, pg_num) for _, pg_id, pg_num in targets]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    enriched = 0
    for (idx, _, _), urls in zip(targets, results):
        if isinstance(urls, Exception):
            logger.warning("image fetch failed for property at idx %d: %s", idx, urls)
            continue
        if urls:
            properties[idx]["p_image"] = urls[0]
            properties[idx]["_images"] = urls
            enriched += 1
    logger.info("image enrichment: %d/%d images found", enriched, len(targets))


def _load_property_signals(properties: list) -> dict:
    """Fetch outcome signals (conversion / no-show) per property — observably.

    The learning loop must never run BLIND AND SILENT. If signals can't be loaded
    (import missing, Redis outage) we still rank — degrading to no-signal scoring —
    but we log it loudly so a dark loop is visible instead of swallowed. A missing
    property id is skipped quietly (not a failure). Returns {property_id: signals}.
    """
    try:
        from db.redis.analytics import get_property_signals
    except ImportError as e:
        logger.warning("outcome-signal scoring disabled — get_property_signals unavailable: %s", e)
        return {}

    out = {}
    failures = 0
    for p in properties:
        pid = p.get("p_property_id", p.get("property_id", ""))
        if not pid:
            continue
        try:
            sig = get_property_signals(pid)
            if sig:
                out[pid] = sig
        except Exception:
            failures += 1
    if failures:
        logger.warning(
            "outcome-signal fetch failed for %d/%d properties — ranking those without signals "
            "(learning loop degraded, not blind-silent)",
            failures, len(properties),
        )
    return out


async def _enrich_top_results(properties: list, limit: int = 5) -> None:
    """Run image enrichment and geocoding concurrently for the top results.

    Both walk the same top-N properties but write disjoint keys (p_image/_images
    vs lat/lng), so they have no ordering dependency. Overlapping them shaves the
    slower call's latency off every search. Each function already swallows its own
    failures, so gather never raises here."""
    await asyncio.gather(
        _enrich_with_images(properties, limit=limit),
        _geocode_properties(properties, limit=limit),
    )


def _short_dest_label(dest: str, max_len: int = 24) -> str:
    """Trim a commute destination to the leading segment for card display.
    'Reliance Corporate Park, Navi Mumbai' → 'Reliance Corporate Park'."""
    head = (dest or "").split(",")[0].strip()
    if len(head) > max_len:
        head = head[: max_len - 1].rstrip() + "…"
    return head


def _prop_coords(p: dict) -> tuple:
    """Pull (lat, lng) floats from a property using the same key fallbacks the
    results loop uses (API fields → geocoded). Returns (None, None) when absent."""
    plat = (p.get("p_latitude") or p.get("p_lat") or p.get("p_pg_latitude")
            or p.get("latitude") or p.get("lat") or p.get("_geocoded_lat") or "")
    plng = (p.get("p_longitude") or p.get("p_long") or p.get("p_pg_longitude")
            or p.get("longitude") or p.get("long") or p.get("lng")
            or p.get("_geocoded_lng") or "")
    try:
        if plat and plng:
            return float(plat), float(plng)
    except (ValueError, TypeError):
        pass
    return None, None


async def _compute_commute_minutes(properties: list, destination: str,
                                   limit: int = COMMUTE_RANK_TOPN) -> None:
    """R1 — fill `_commute_min` (real driving minutes) for the top-N candidates.

    When the user gave a daily commute destination (office/college), compute the
    real driving time from THAT place to each top candidate in ONE OSRM table call
    (source = destination, destinations = properties) — far cheaper than N
    point-to-point calls. Mutates the property dicts in place.

    Fully graceful by design: a vague/empty destination, a destination that won't
    geocode, properties without coordinates, or any OSRM error simply leaves the
    properties untouched so ranking degrades to area distance. It NEVER raises —
    the commute term is a bonus signal, never a failure point.
    """
    # Reuse estimate_commute's vague-destination guard so we never geocode "office".
    from tools.broker.landmarks import _VAGUE_DESTINATIONS

    dest = (destination or "").strip()
    if not dest or dest.lower() in _VAGUE_DESTINATIONS:
        return

    try:
        dest_lat, dest_lng = await geocode_address(dest)
    except Exception as e:
        logger.warning("commute: destination geocode failed for %r: %s", dest, e)
        return
    if not dest_lat or not dest_lng:
        logger.info("commute: could not geocode destination %r — ranking by area", dest)
        return

    candidates = properties[:limit]
    # Fill in any missing property coordinates (concurrent) so commute can cover the
    # whole window, not just the few the search API returned with coords.
    try:
        await _geocode_properties(candidates, limit=limit)
    except Exception as e:
        logger.debug("commute: candidate geocoding hiccup (continuing): %s", e)

    targets = []  # (property, lat, lng)
    for p in candidates:
        plat, plng = _prop_coords(p)
        if plat is not None and plng is not None:
            targets.append((p, plat, plng))
    if not targets:
        logger.info("commute: no top-%d candidates have coordinates — ranking by area", limit)
        return

    # ONE OSRM table call: index 0 is the destination (source), 1..N the properties.
    coord_str = f"{dest_lng},{dest_lat}" + "".join(
        f";{lng},{lat}" for _, lat, lng in targets
    )
    try:
        data = await asyncio.wait_for(
            http_get(
                f"https://maps.rentok.com/table/v1/driving/{coord_str}",
                params={"sources": "0", "api_key": settings.OSRM_API_KEY},
            ),
            timeout=COMMUTE_RANK_TIMEOUT_S,
        )
    except Exception as e:
        logger.warning("commute: OSRM table call failed — ranking by area: %s", e)
        return

    durations = (data or {}).get("durations") or [[]]
    row = durations[0] if durations else []
    assigned = 0
    for i, (p, _, _) in enumerate(targets, start=1):
        if i < len(row) and row[i] is not None:
            try:
                p["_commute_min"] = int(round(float(row[i]) / 60))
                assigned += 1
            except (ValueError, TypeError):
                continue
    logger.info("commute: assigned drive time to %d/%d candidates (dest=%r)",
                assigned, len(targets), dest)


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
    # NOTE: `unit_types_available`, `pg_available_for` and `sharing_types_enabled` are NOT
    # sent to the search API. Ground truth (RentOk backend): these are free-text, per-property
    # enums — pg_available_for is an exact IN-match on stored phrases (e.g. "Male & Female"),
    # unit_types_available is an array-overlap on uppercase tokens (e.g. SINGLESHARING).
    # Client-guessed values like "All Boys" / "double sharing" never match → silent 0 results,
    # and they over-exclude the predominantly "Any" co-living inventory. We instead return the
    # full candidate set and rank/filter post-search (gender is hard-filtered below via
    # gender_compatible; unit/sharing type rank via utils/scoring.py).

    logger.debug("search payload: %s", payload)

    # Step 4: Search with progressive relaxation — surface MORE results
    MIN_RESULTS_THRESHOLD = 5

    properties = await _call_search_api(payload)
    # None ⇒ hard failure (couldn't reach the listings API), NOT "no inventory".
    # Surfacing a false "nothing available here" would be lying to the user, so we
    # tell the truth: the problem is on our side and they should retry.
    if properties is None:
        return (
            "I'm having trouble reaching our property listings right now — "
            "this is a temporary issue on our end, not a lack of options. "
            "Please try again in a moment."
        )
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
        # unit_types_available deliberately omitted — see note on the base payload above.
        logger.debug("relaxation round 1 payload: %s", r1_payload)
        # A relaxation round that hard-fails just yields no extra results — we
        # already have the base set, so treat None as an empty top-up here.
        r1_results = await _call_search_api(r1_payload) or []
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
        r2_results = await _call_search_api(r2_payload) or []
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
        record_signal(search_ran=True, result_count=0)
        return "No properties are currently available in this region."

    logger.info("found %d properties", len(properties))

    # Enrich top results: images + geocoding are independent I/O over the same
    # top-N (disjoint keys), so run them concurrently instead of back-to-back.
    await _enrich_top_results(properties, limit=5)

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
    # Load property outcome signals (conversion/no-show) — observably, never blind.
    prop_signals = _load_property_signals(properties)

    def _score(p: dict, commute_aware: bool = False) -> float:
        prop_data = {
            "rent": p.get("p_rent_starts_from", p.get("rent", 0)),
            "distance": p.get("p_distance", p.get("distance")),
            "amenities": p.get("p_common_amenities", p.get("p_amenities", "")),
            "property_type": p.get("p_property_type", ""),
            "pg_available_for": p.get("p_pg_available_for", ""),
        }
        if commute_aware:
            # None when this property had no commute computed → match_score falls
            # back to the distance term for it (mixed windows rank consistently).
            prop_data["commute_minutes"] = p.get("_commute_min")
        pid = p.get("p_property_id", p.get("property_id", ""))
        signals = prop_signals.get(pid, {})
        return calc_match_score(prop_data, scoring_prefs,
                                deal_breakers=deal_breakers, property_signals=signals)

    for p in properties:
        p["_custom_score"] = _score(p)

    # Sort by custom score (descending) to surface best matches first
    properties.sort(key=lambda p: p.get("_custom_score", 0), reverse=True)

    # Gender is a HARD constraint — a renter physically cannot book an
    # opposite-gender-only PG. Unlike amenities (soft, ranked above), exclude
    # incompatible inventory rather than surfacing unbookable options ranked a
    # few points lower. "Any"/co-living and unknown values stay permissive, so the
    # predominantly "Any"-tagged stock is never over-filtered.
    # NOTE: the structured p_pg_available_for tag is unreliable on this inventory —
    # girls-only PGs (e.g. "... KURLA GIRL'S") are routinely tagged "Any" — so the
    # filter also reads the deliberate GIRL'S/BOY'S gender label in the property
    # NAME, which overrides the tag. Co-living "BOY'S/GIRL'S" names stay bookable.
    if pg_available_for:
        compatible = [p for p in properties
                      if gender_compatible_listing(pg_available_for,
                                                   p.get("p_pg_available_for", ""),
                                                   p.get("p_pg_name", ""))]
        removed = len(properties) - len(compatible)
        if compatible:
            if removed:
                logger.info("gender filter removed %d incompatible properties (pref=%s)",
                            removed, pg_available_for)
            properties = compatible
        elif removed:
            # Every candidate is opposite-gender → be honest instead of padding
            # the list with options the user cannot actually book.
            logger.info("gender filter removed ALL %d properties (pref=%s)",
                        removed, pg_available_for)
            record_signal(search_ran=True, result_count=0)
            return (
                f"I couldn't find any properties matching your requirement "
                f"({pg_available_for}) in this area — the available options here "
                f"are for a different gender. Want me to widen the search or try a nearby area?"
            )

    # R1 — commute-based re-rank. If the user told us their daily destination
    # (office/college), re-rank the top (bookable) candidates by REAL driving time
    # to that place instead of crow-flies distance from the searched area. This is
    # the marquee right-first signal. Optional: with no destination the block is
    # skipped entirely (no extra calls), so non-commute users see UNCHANGED ranking.
    commute_dest = (prefs.get("commute_from") or "").strip()
    commute_label = ""
    if commute_dest:
        await _compute_commute_minutes(properties, commute_dest, limit=COMMUTE_RANK_TOPN)
        if any(p.get("_commute_min") is not None for p in properties):
            for p in properties:
                p["_custom_score"] = _score(p, commute_aware=True)
            properties.sort(key=lambda p: p.get("_custom_score", 0), reverse=True)
            commute_label = _short_dest_label(commute_dest)
            logger.info("commute re-rank applied (dest=%r)", commute_dest)

    # Final result set resolved (post-score, post gender hard-filter) — record
    # the real count so egress can shape honest UI (scarcity only from truth).
    record_signal(search_ran=True, result_count=len(properties))

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
        # Property contact lives in p_personal_contact; p_phone_number is usually
        # absent. The shortlist API requires a non-empty property_contact, so prefer
        # the populated field. Server-side only — never rendered to users.
        phone = p.get("p_personal_contact") or p.get("p_phone_number") or ""
        min_token = p.get("p_min_token_amount", 1000)
        microsite_url = p.get("p_microsite_url", p.get("microsite_url", ""))
        match_score = p.get("_custom_score", p.get("p_match_score", p.get("match_score", "")))
        # Normalise at write time → cache always contains clean strings,
        # no downstream tool needs to know the raw API shape.
        amenities_raw = parse_amenities(p.get("p_common_amenities", p.get("p_amenities", "")))
        sharing_types_data = parse_sharing_types(p.get("p_sharing_types_enabled", []))
        # Structured sharing + full image list power the detail sheet (§3.2). The sheet
        # needs an ARRAY of {label,price} (the display string above renders blank there)
        # and the full gallery, not just the cover. Both come from data already fetched.
        sharing_types_struct = parse_sharing_types_structured(p.get("p_sharing_types_enabled", []))
        images_list = p.get("_images") or ([image] if image else [])

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
            "sharing_types_list": sharing_types_struct,  # structured for the detail sheet
            "images": images_list,                       # full gallery for the detail sheet
        }
        # R1 — carry the real commute time so the card can show "X min to <dest>".
        # Additive: only present when a commute was actually computed this search.
        _cmin = p.get("_commute_min")
        if _cmin is not None and commute_label:
            info["commute_minutes"] = _cmin
            info["commute_label"] = commute_label
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

    # P4: record the native carousel on the signal slate so egress emits a structured
    # carousel unit that SUPERSEDES the regex-scraped one (stripped in chat.py). Built
    # from the same top-5 property_template WhatsApp shows; byte-compatible with the FE
    # card + detail sheet, but sourced from structured data instead of the broker's prose.
    # Build the full ranked carousel once: top-5 go on the signal (this turn's native
    # carousel); the full list (up to 15) is cached so show_more_properties can page the
    # next batch NATIVELY (no prose scraping). Caching resets the paging cursor to 5.
    _full_items, _carousel_center = build_carousel_items(property_template, lat, lng, limit=15)
    if _full_items:
        record_signal(carousel_items=_full_items[:5], carousel_map_center=_carousel_center)
    set_search_carousel(user_id, _full_items, _carousel_center)

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
