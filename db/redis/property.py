"""
db/redis/property.py — Property cache, search results, images, and shortlists.

Covers:
  - Property info map (search results cache)
  - Last search results (24h TTL)
  - Shortlisted properties
  - Property template (WhatsApp carousel)
  - Property image IDs and URLs
  - Property search ID buffer (10-min TTL)
"""

from typing import Optional

from db.redis._base import _r, _json_set, _json_get, PROPERTY_INFO_TTL, SEARCH_IDS_TTL, LAST_SEARCH_TTL


# ---------------------------------------------------------------------------
# Property info map (search results cache)
# ---------------------------------------------------------------------------

def set_property_info_map(user_id: str, info_map: list[dict]) -> None:
    _json_set(f"{user_id}:property_info_map", info_map, ex=PROPERTY_INFO_TTL)


def get_property_info_map(user_id: str) -> list[dict]:
    return _json_get(f"{user_id}:property_info_map", default=[])


# ---------------------------------------------------------------------------
# Last search results (cross-session context, 24h TTL)
# ---------------------------------------------------------------------------

def set_last_search_results(user_id: str, results: list[dict]) -> None:
    _json_set(f"{user_id}:last_search", results, ex=LAST_SEARCH_TTL)


def get_last_search_results(user_id: str) -> list[dict]:
    return _json_get(f"{user_id}:last_search", default=[])


# ---------------------------------------------------------------------------
# Shortlisted properties
# ---------------------------------------------------------------------------

def get_shortlisted_properties(user_id: str) -> list:
    """Return list of shortlisted property IDs.

    Reads from user_memory['properties_shortlisted'] — the same key that
    record_property_shortlisted() writes to. The old '{uid}:shortlisted'
    key was never written to, causing D3 to always return empty.
    """
    memory = _json_get(f"{user_id}:user_memory") or {}
    return memory.get("properties_shortlisted", [])


# ---------------------------------------------------------------------------
# Property template (carousel cards)
# ---------------------------------------------------------------------------

def save_property_template(user_id: str, template: list[dict]) -> None:
    _json_set(f"{user_id}:property_template", template)


def get_property_template(user_id: str) -> list[dict]:
    return _json_get(f"{user_id}:property_template", default=[])


def clear_property_template(user_id: str) -> None:
    _r().delete(f"{user_id}:property_template")


# ---------------------------------------------------------------------------
# Ranked search carousel + paging cursor (native show-more pagination)
# ---------------------------------------------------------------------------

def set_search_carousel(user_id: str, items: list[dict], map_center: Optional[dict]) -> None:
    """Cache the FULL ranked native carousel items (+ map_center) for the last search and
    reset the paging cursor to 5 (the top-5 are already shown by the fresh-search carousel).
    show_more_properties() pages the next batch from here — native, no prose scraping."""
    _json_set(f"{user_id}:search_carousel", {"items": items, "map_center": map_center}, ex=PROPERTY_INFO_TTL)
    set_carousel_cursor(user_id, 5)


def get_search_carousel(user_id: str) -> dict:
    return _json_get(f"{user_id}:search_carousel", default={})


def get_carousel_cursor(user_id: str) -> int:
    v = _json_get(f"{user_id}:carousel_cursor", default=5)
    try:
        return int(v)
    except (ValueError, TypeError):
        return 5


def set_carousel_cursor(user_id: str, n: int) -> None:
    _json_set(f"{user_id}:carousel_cursor", int(n), ex=PROPERTY_INFO_TTL)


# ---------------------------------------------------------------------------
# Property images
# ---------------------------------------------------------------------------

def set_property_images_id(user_id: str, images: list[str | None]) -> None:
    _json_set(f"{user_id}:property_images_id", images)


def get_property_images_id(user_id: str) -> list[str | None]:
    return _json_get(f"{user_id}:property_images_id", default=[])


def clear_property_images_id(user_id: str) -> None:
    _r().delete(f"{user_id}:property_images_id")


# ---------------------------------------------------------------------------
# Image URLs
# ---------------------------------------------------------------------------

def set_image_urls(user_id: str, urls: list[str]) -> None:
    _json_set(f"{user_id}:image_urls", urls)


def get_image_urls(user_id: str) -> list[str]:
    return _json_get(f"{user_id}:image_urls", default=[])


def clear_image_urls(user_id: str) -> None:
    _r().delete(f"{user_id}:image_urls")


# ---------------------------------------------------------------------------
# Property search tool IDs (temporary, 10min TTL)
# ---------------------------------------------------------------------------

def set_property_id_for_search(user_id: str, property_ids: list) -> None:
    _json_set(f"{user_id}:search_property_ids", property_ids, ex=SEARCH_IDS_TTL)


def get_property_id_for_search(user_id: str) -> list[str]:
    return _json_get(f"{user_id}:search_property_ids", default=[])


def clear_property_id_for_search(user_id: str) -> None:
    _r().delete(f"{user_id}:search_property_ids")
