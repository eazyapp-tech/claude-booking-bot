"""
message_parser.py — Convert agent markdown into structured parts[].

Mirrors the frontend's renderRichMessage() regex cascade but produces
typed JSON objects that the frontend can render directly, eliminating
the need for client-side regex parsing.

Part types:
  - text:               { type, markdown }
  - property_carousel:  { type, properties: [{name, location, rent, gender, distance, image, link, lat, lng}], map_center }
  - comparison_table:   { type, headers, rows, winner }
"""

import re
from core.log import get_logger
from db.redis_store import get_property_info_map, get_preferences

logger = get_logger("core.message_parser")

# ── Raw-media-URL safety net ────────────────────────────────────────────
# When the broker drifts off the listing format (Haiku variance) and no
# carousel detector matches, the reply — including any raw "Image: <cdn-url>"
# lines — would otherwise render verbatim as plain text. Strip those so a user
# can NEVER see a raw azureedge/blob .mp4/.jpg URL in chat. Applied ONLY to
# leftover text parts (after block extraction, which consumes Image: lines into
# the card), so legitimate non-media links in prose are preserved.
_RAW_MEDIA_LINE = re.compile(
    r"(?im)^[ \t]*(?:image|video|photo|link)s?\s*:\s*https?://\S+[ \t]*$"
)
_BARE_MEDIA_URL = re.compile(
    r"(?i)\bhttps?://\S*(?:azureedge\.net|blob\.core\.windows\.net|rentok-?storage)\S*"
)


def _strip_raw_media_urls(md: str) -> str:
    """Remove leaked 'Image:/Link: <url>' lines and bare CDN media URLs from text."""
    if not md:
        return md
    md = _RAW_MEDIA_LINE.sub("", md)
    md = _BARE_MEDIA_URL.sub("", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def drop_scraped_carousel(parts: list[dict], has_native_carousel: bool) -> list[dict]:
    """P4 supersession rule: when a structured (native) property carousel is emitted
    for this turn (search ran → carousel_items on the signal), drop the regex-scraped
    `property_carousel` part so exactly ONE carousel renders — structured supersedes
    scraped. When no native carousel is emitted (e.g. a same-search 'show more' turn
    with no fresh search signal), the scraped carousel is the sole source and is kept.
    Surrounding text/comparison parts are always preserved."""
    if not has_native_carousel:
        return parts
    return [p for p in parts if p.get("type") != "property_carousel"]


def parse_message_parts(markdown: str, user_id: str) -> list[dict]:
    """Parse agent markdown into structured parts[].

    Applies the same detection cascade as the frontend's
    renderRichMessage(), but outputs JSON instead of HTML.
    Falls back to a single TextPart wrapping the full markdown.
    """
    if not markdown or not markdown.strip():
        return [{"type": "text", "markdown": markdown or ""}]

    # Comparison is emitted natively (D2 signal → generate_ui_parts); the legacy
    # pipe-table prose-scraper was removed. A markdown table now renders as text.

    # 1. Compact property format: **N. Name**\n📍 ...
    compact_matches = list(re.finditer(
        r"\*\*(\d+)\.\s+(.+?)\*\*\s*\n(📍.+)", markdown
    ))
    if compact_matches:
        return _build_carousel_parts(markdown, compact_matches, False, user_id)

    # 2b. Tolerant numbered format — Haiku drift: the number may sit OUTSIDE the
    #     bold ("1. **Name**") or the name may be unbolded ("1. Name"), as long as
    #     a 📍 meta line follows. Superset of (2); placed after so (2) wins first.
    tolerant_matches = list(re.finditer(
        r"(?:\*\*)?\s*(\d+)\.\s+(?:\*\*)?(.+?)(?:\*\*)?\s*\n\s*(📍[^\n]+)", markdown
    ))
    if tolerant_matches:
        return _build_carousel_parts(markdown, tolerant_matches, False, user_id)

    # 3. Legacy bold format: **N. Name** — ₹X
    legacy_matches = list(re.finditer(
        r"\*\*(\d+)\.\s*(.+?)\*\*\s*[—–\-]\s*(₹[\d,]+(?:\/\s*(?:month|mo))?)",
        markdown,
    ))
    if legacy_matches:
        return _build_carousel_parts(markdown, legacy_matches, True, user_id)

    # 4. H3-header format: ### 🏠 N. Name  or  ### N. Name
    h3_matches = list(re.finditer(
        r"^#{1,3}\s+[^\d\n]*(\d+)\.\s+(.+)$", markdown, re.MULTILINE
    ))
    if h3_matches:
        enrichment = _enrich_h3_matches(markdown, h3_matches)
        return _build_carousel_parts(markdown, h3_matches, True, user_id, enrichment)

    # 5. Keycap H3 format: ### 1️⃣ Name
    keycap_matches = list(re.finditer(
        r"^#{1,3}\s+(\d)\ufe0f\u20e3\s+(.+)$", markdown, re.MULTILINE
    ))
    if keycap_matches:
        enrichment = _enrich_h3_matches(markdown, keycap_matches)
        return _build_carousel_parts(markdown, keycap_matches, True, user_id, enrichment)

    # 6. Default — single text part (strip any raw media URLs the broker leaked
    #    into prose when it drifted off every listing format).
    return [{"type": "text", "markdown": _strip_raw_media_urls(markdown)}]


# ------------------------------------------------------------------
# Property carousel helpers
# ------------------------------------------------------------------

def _enrich_h3_matches(text: str, matches: list) -> dict:
    """Extract rent and location from H3/keycap blocks.

    Returns dict keyed by match index with {rent, location} values.
    (re.Match objects are immutable so we can't set attributes on them.)
    """
    enrichment = {}
    for i, m in enumerate(matches):
        block_start = m.start()
        block_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[block_start:block_end]

        price_m = (
            re.search(r"💰[^\n]*(₹[\d,]+)", block) or
            re.search(r"[Rr]ent[^\n]*(₹[\d,]+)", block)
        )
        rent_fall = re.search(r"₹[\d,]{4,}(?:/month|/mo)?", block)
        rent = price_m.group(1) if price_m else (rent_fall.group(0) if rent_fall else "")

        loc_m = re.search(r"📍\s*([^\n]+)", block)
        location = loc_m.group(1) if loc_m else ""

        enrichment[i] = {"rent": rent, "location": location}
    return enrichment


def _build_carousel_parts(
    text: str,
    matches: list,
    is_legacy: bool,
    user_id: str,
    enrichment: dict | None = None,
) -> list[dict]:
    """Build parts[] from property listing matches.

    Args:
        enrichment: optional dict from _enrich_h3_matches keyed by match index
    """

    # Load Redis property info for enrichment
    info_map = get_property_info_map(user_id)

    properties = []
    for i, match in enumerate(matches):
        name = match.group(2).strip()
        block_start = match.start()
        block_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[block_start:block_end]

        price = ""
        location = ""
        gender = ""
        distance = ""

        # H3/keycap enrichment data (if available)
        enr = (enrichment or {}).get(i, {})

        if is_legacy:
            # match.group(3) may exist (legacy bold) or we fall back to enrichment
            try:
                price = match.group(3).strip() if match.group(3) else ""
            except IndexError:
                price = ""
            if not price:
                price = enr.get("rent", "")
            if not price:
                pm = re.search(r"💰[^\n]*(₹[\d,]+)", block) or re.search(r"[Rr]ent[^\n]*(₹[\d,]+)", block)
                if pm:
                    price = pm.group(1)
            # Location from first non-header line
            loc_line = re.search(r"📍\s*([^\n]+)", block)
            if loc_line:
                location = loc_line.group(1).split("·")[0].strip()
            elif enr.get("location"):
                location = enr["location"].split("·")[0].strip()
        else:
            # Compact format: match.group(3) is the 📍 line
            meta_line = match.group(3).strip()
            parts = [p.strip() for p in re.sub(r"^📍\s*", "", meta_line).split("·")]
            location = parts[0] if parts else ""
            pm = re.search(r"₹[\d,]+(?:/mo(?:nth)?)?", meta_line)
            price = pm.group(0).replace("/month", "/mo") if pm else ""
            for p in parts:
                if re.match(r"^(Any|Boys|Girls|All Boys|All Girls|Mixed)", p, re.IGNORECASE):
                    gender = p
                elif re.search(r"~?[\d.]+\s*km", p, re.IGNORECASE):
                    distance = p

        # Extract image and link from block
        img_m = re.search(r"(?:Image:\s*|!\[[^\]]*\]\()(https?://[^\s)]+)", block, re.IGNORECASE)
        link_m = re.search(r"Link:\s*(https?://\S+)", block, re.IGNORECASE)
        image = img_m.group(1) if img_m else ""
        link = link_m.group(1) if link_m else ""

        # Enrich from Redis property info
        redis_info = _find_in_info_map(name, info_map)
        if redis_info:
            if not image:
                image = redis_info.get("property_image", "")
            if not link:
                link = redis_info.get("property_link", "")
            if not price:
                price = redis_info.get("property_rent", "")
            if not location:
                location = redis_info.get("property_location", "")
            if not gender:
                gender = redis_info.get("pg_available_for", "")

        # Lat/lng for map view
        lat = ""
        lng = ""
        if redis_info:
            lat = str(redis_info.get("property_lat", "")) if redis_info.get("property_lat") else ""
            lng = str(redis_info.get("property_long", "")) if redis_info.get("property_long") else ""

        # Match score + amenities for Carousel v2
        score = ""
        amenities = ""
        if redis_info:
            raw_score = redis_info.get("match_score", "")
            if raw_score not in ("", None):
                try:
                    score = str(round(float(raw_score)))
                except (ValueError, TypeError):
                    pass  # Non-numeric score from Redis — leave score as ""
            amenities = redis_info.get("amenities", "")

        properties.append({
            "name": name,
            "location": location,
            "rent": price,
            "gender": gender,
            "distance": distance,
            "image": image,
            "link": link,
            "lat": lat,
            "lng": lng,
            "score": score,
            "amenities": amenities,
            # Sheet-only richness (multi-image gallery + structured sharing) — additive,
            # rides on the stashed item the detail sheet composes from.
            **_sheet_enrichment(redis_info),
        })

    # Text before first match
    pre_text = text[:matches[0].start()].strip()

    # Text after last property block (find separator after last match)
    last_start = matches[-1].start()
    from_last = text[last_start:]
    close_sep = re.search(r"\n[-*]{3,}\s*(?:\n|$)", from_last)
    post_text = ""
    if close_sep:
        post_start = last_start + close_sep.start() + len(close_sep.group(0))
        post_text = text[post_start:].strip()
    else:
        double_nl = re.search(r"\n\n(?!\s*(?:📍|💰|👥|🏷|#{1,3}))", from_last)
        post_text = from_last[double_nl.start():].strip() if double_nl else ""
    # Clean up meta lines from post text
    post_text = re.sub(r"^(?:Image|Link|Match|Distance|For|Type):.*$", "", post_text, flags=re.MULTILINE | re.IGNORECASE)
    post_text = re.sub(r"\n{3,}", "\n\n", post_text).strip()

    # Compute map_center from stored search coords, or average property coords
    prefs = get_preferences(user_id)
    search_lat = prefs.get("search_lat", "")
    search_lng = prefs.get("search_lng", "")
    map_center = None
    if search_lat and search_lng:
        try:
            map_center = {"lat": float(search_lat), "lng": float(search_lng)}
        except (ValueError, TypeError):
            pass  # Malformed coords in Redis — fall through to property-average fallback below
    if not map_center:
        # Fallback: average of property coordinates
        valid_coords = [(float(p["lat"]), float(p["lng"])) for p in properties if p.get("lat") and p.get("lng")]
        if valid_coords:
            map_center = {
                "lat": sum(c[0] for c in valid_coords) / len(valid_coords),
                "lng": sum(c[1] for c in valid_coords) / len(valid_coords),
            }

    parts = []
    if pre_text:
        pre_text = _strip_raw_media_urls(pre_text)
        if pre_text:
            parts.append({"type": "text", "markdown": pre_text})
    carousel_part = {"type": "property_carousel", "properties": properties}
    if map_center:
        carousel_part["map_center"] = map_center
    parts.append(carousel_part)
    if post_text:
        post_text = _strip_raw_media_urls(post_text)
        if post_text:
            parts.append({"type": "text", "markdown": post_text})
    return parts


def _sheet_enrichment(redis_info: dict | None) -> dict:
    """Sheet-only enrichment fields lifted from the cached property info_map so the
    detail sheet (eazypg-chat property-sheet.js composePropertySheet) renders a
    multi-image gallery + a 'Choose sharing' section. These keys ride on the carousel
    item the frontend stashes, so they reach the sheet verbatim without changing the
    lean card view-model. Purely additive: returns {} for legacy cache entries that
    predate these keys, so the sheet degrades gracefully (sections hidden, no shells).
    """
    if not redis_info:
        return {}
    out = {}
    images = redis_info.get("images")
    if images:
        out["images"] = images
    sharing = redis_info.get("sharing_types_list")
    if sharing:
        out["sharing"] = sharing
    return out


def _find_in_info_map(name: str, info_map: list) -> dict | None:
    """Find a property in the Redis info map by name (case-insensitive, whitespace-normalized).

    Searches in REVERSE order so that newer entries (which may have
    additional fields like property_lat/property_long) take precedence
    over older entries for the same property.
    """
    if not info_map or not name:
        return None

    name_norm = re.sub(r"\s+", " ", name.strip().lower())

    for info in reversed(info_map):
        stored_name = info.get("property_name", "")
        stored_norm = re.sub(r"\s+", " ", stored_name.strip().lower())
        if stored_norm == name_norm:
            return info

    # Fuzzy: check if one contains the other (handles minor truncation)
    for info in reversed(info_map):
        stored_name = info.get("property_name", "")
        stored_norm = re.sub(r"\s+", " ", stored_name.strip().lower())
        if name_norm in stored_norm or stored_norm in name_norm:
            return info

    return None
