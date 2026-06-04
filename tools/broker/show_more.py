"""Native show-more pagination tool.

Pages the NEXT batch of the last search's ranked carousel from cache and records it on the
signal slate, so generate_ui_parts emits a NATIVE carousel (the same path as fresh search) —
no regex prose-scraping. On exhaustion it records no carousel and hints a radius widen (the
broker then calls search_properties(radius_flag=true)). Additive: if the broker skips this
tool and paginates in prose, the legacy scrape still renders the cards.
"""
from core.log import get_logger
from core.signals import record_signal
from db.redis_store import (
    set_search_carousel,   # noqa: F401  (re-exported so tests can seed the cache)
    get_search_carousel,
    get_carousel_cursor,
    set_carousel_cursor,
)

logger = get_logger("tools.show_more")

_BATCH = 5

TOOL_SCHEMA = {
    "name": "show_more_properties",
    "description": (
        "Show the NEXT batch of properties from the user's LAST search (pagination). Call this "
        "when the user asks to see more / other / next options and there may be unshown results "
        "from the last search. It renders the next few as cards automatically — do NOT re-list "
        "them in prose. If everything has already been shown it will tell you to widen the area; "
        "then call search_properties(radius_flag=true) instead."
    ),
    "input_schema": {"type": "object", "additionalProperties": False, "properties": {}, "required": []},
}


async def show_more_properties(user_id: str, **kwargs) -> str:
    cached = get_search_carousel(user_id) or {}
    items = cached.get("items") or []
    if not items:
        return ("I don't have an active search to page through yet — tell me the area, budget and "
                "who it's for, and I'll pull up options.")

    cursor = get_carousel_cursor(user_id)
    if cursor >= len(items):
        return ("That's everything I have in this area. Want me to widen the search to nearby "
                "areas? I can expand the radius.")

    batch = items[cursor:cursor + _BATCH]
    sig = {"carousel_items": batch}
    center = cached.get("map_center")
    if center:
        sig["carousel_map_center"] = center
    record_signal(**sig)
    set_carousel_cursor(user_id, cursor + len(batch))
    logger.info("show_more: served %d items (cursor %d→%d of %d) for %s",
                len(batch), cursor, cursor + len(batch), len(items), str(user_id)[:12])
    return "Here are a few more options for you."
