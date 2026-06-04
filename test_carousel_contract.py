"""P4 — One-contract carousel regression (structured-supersedes-scraped).

Deterministic assertions (no Redis/network/LLM): proves the native property
carousel is emitted from structured search data with the EXACT field map the
live FE card + detail sheet read — byte-compatible with today's regex-scraped
`message_parser._build_carousel_parts` item — and that emission/supersession is
wired so exactly one carousel renders per turn.

Run: `./.venv/bin/python test_carousel_contract.py` (exit 0 = pass).
"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")
import sys

_p = _f = 0


def ck(n, c, d=""):
    global _p, _f
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else " -> " + repr(d)))
    _p += bool(c)
    _f += (not c)


# ── Fixtures: the `info` dict shape search.py builds (set_property_info_map payload) ──
def _info(name, rent, **kw):
    base = {
        "property_name": name,
        "property_location": "Kurla West, Mumbai",
        "property_rent": rent,
        "pg_available_for": "Boys",
        "property_type": "PG",
        "property_image": "https://img/cover.webp",
        "property_link": "https://micro/site",
        "property_lat": "19.07",
        "property_long": "72.88",
        "match_score": 87.4,
        "distance": "2.3 km",
        "amenities": "WiFi, AC, Food",
        "images": ["https://img/1.webp", "https://img/2.webp"],
        "sharing_types_list": [{"label": "Double sharing", "price": "₹9000/mo"}],
    }
    base.update(kw)
    return base


# Keys every legacy carousel item ALWAYS carries (card + map consumers).
_CORE_KEYS = ("name", "location", "rent", "gender", "distance",
              "image", "link", "lat", "lng", "score", "amenities")


def t_item_field_map():
    from tools.broker.search import build_carousel_items
    infos = [_info("FitLife", "9000"), _info("Lake View", "11000")]
    items, center = build_carousel_items(infos, 19.07, 72.88, limit=5)

    ck("emits one item per info", len(items) == 2, len(items))
    it = items[0]
    for k in _CORE_KEYS:
        ck(f"item always carries '{k}'", k in it, sorted(it))

    ck("name <- property_name", it["name"] == "FitLife", it.get("name"))
    ck("location <- property_location", it["location"] == "Kurla West, Mumbai", it.get("location"))
    ck("rent FORMATTED rupee/mo (not raw 9000)", it["rent"] == "₹9,000/mo", it.get("rent"))
    ck("gender <- pg_available_for", it["gender"] == "Boys", it.get("gender"))
    ck("distance passthrough", it["distance"] == "2.3 km", it.get("distance"))
    ck("image <- property_image (cover)", it["image"] == "https://img/cover.webp", it.get("image"))
    ck("link <- property_link", it["link"] == "https://micro/site", it.get("link"))
    ck("lat/lng <- property_lat/long", it["lat"] == "19.07" and it["lng"] == "72.88", (it.get("lat"), it.get("lng")))
    ck("score rounded int-string <- match_score", it["score"] == "87", it.get("score"))
    ck("amenities passthrough string", it["amenities"] == "WiFi, AC, Food", it.get("amenities"))
    # Sheet-only enrichment — present (and exact) when source has it
    ck("images = full gallery (sheet)", it.get("images") == ["https://img/1.webp", "https://img/2.webp"], it.get("images"))
    ck("sharing = structured list (sheet)", it.get("sharing") == [{"label": "Double sharing", "price": "₹9000/mo"}], it.get("sharing"))
    ck("map_center <- search center floats", center == {"lat": 19.07, "lng": 72.88}, center)


def t_limit_format_and_graceful_fallbacks():
    from tools.broker.search import build_carousel_items
    infos = [_info(f"P{i}", str(9000 + i * 500)) for i in range(8)]
    items, _ = build_carousel_items(infos, 19.07, 72.88, limit=5)
    ck("limit caps to top-N (5)", len(items) == 5, len(items))
    ck("ranked order preserved (P0 first)", items[0]["name"] == "P0", items[0].get("name"))

    # rent formatting robustness
    ck("rent with comma normalizes", build_carousel_items([_info("X", "9,000")], 0, 0)[0][0]["rent"] == "₹9,000/mo")
    ck("rent with ₹ prefix normalizes", build_carousel_items([_info("X", "₹9000")], 0, 0)[0][0]["rent"] == "₹9,000/mo")
    ck("non-numeric rent passes through", build_carousel_items([_info("X", "On request")], 0, 0)[0][0]["rent"] == "On request")

    # Bare legacy-style info (predates sheet keys) → no crash, sheet keys ABSENT (parity with _sheet_enrichment {})
    bare = {
        "property_name": "Bare", "property_location": "L", "property_rent": "8000",
        "pg_available_for": "Any", "property_image": "", "property_link": "",
        "property_lat": "", "property_long": "", "match_score": "", "distance": "", "amenities": "",
    }
    items3, c3 = build_carousel_items([bare], "", "")
    it3 = items3[0]
    for k in _CORE_KEYS:
        ck(f"bare info still carries core '{k}'", k in it3, sorted(it3))
    ck("missing gallery → 'images' key ABSENT (parity)", "images" not in it3, sorted(it3))
    ck("missing sharing → 'sharing' key ABSENT (parity)", "sharing" not in it3, sorted(it3))
    ck("blank match_score → score ''", it3["score"] == "", it3.get("score"))
    ck("no center + no coords → map_center None", c3 is None, c3)

    # map_center fallback: no search center but items have coords → average
    items4, c4 = build_carousel_items([_info("A", "9000", property_lat="19.0", property_long="72.0"),
                                       _info("B", "9000", property_lat="19.2", property_long="72.4")], "", "")
    ck("map_center falls back to property-coord average",
       c4 is not None and abs(c4["lat"] - 19.1) < 1e-6 and abs(c4["lng"] - 72.2) < 1e-6, c4)


def t_native_unit_emission():
    """generate_ui_parts emits a native carousel/listing unit from the signal."""
    from core.ui_parts import generate_ui_parts
    items = [{"name": "FitLife", "location": "Kurla", "rent": "₹9,000/mo", "image": "https://i/c.webp",
              "gender": "Boys", "score": "87", "amenities": "WiFi", "lat": "19.07", "lng": "72.88"}]
    signals = {"search_ran": True, "result_count": 1,
               "carousel_items": items, "carousel_map_center": {"lat": 19.07, "lng": 72.88}}
    parts = generate_ui_parts("Here are some great options.", "broker", "u1", "en", signals=signals)
    car = [u for u in parts if u.get("kind") == "carousel" and u.get("data", {}).get("payload") == "listing"]
    ck("emits exactly one native carousel/listing unit", len(car) == 1, [u.get("kind") for u in parts])
    if car:
        d = car[0]["data"]
        ck("unit carries items[] verbatim", d.get("items") == items, d.get("items"))
        ck("unit carries map_center", d.get("map_center") == {"lat": 19.07, "lng": 72.88}, d.get("map_center"))
        ck("unit surface=inline", car[0].get("surface") == "inline", car[0].get("surface"))
        ck("unit state=result", car[0].get("state") == "result", car[0].get("state"))
    # No carousel_items signal → no listing carousel emitted (e.g. show-more turn handled by legacy path)
    parts2 = generate_ui_parts("Next 5 options...", "broker", "u1", "en",
                               signals={"search_ran": False, "result_count": 0})
    ck("no signal → no native listing carousel", not any(u.get("data", {}).get("payload") == "listing"
                                                          for u in parts2 if u.get("kind") == "carousel"), parts2)


def t_supersession_guard():
    """chat.py drops the scraped property_carousel when a native one will be emitted."""
    from core.message_parser import drop_scraped_carousel
    parsed = [
        {"type": "text", "markdown": "Here are options"},
        {"type": "property_carousel", "properties": [{"name": "FitLife"}], "map_center": {"lat": 1, "lng": 2}},
        {"type": "text", "markdown": "Want details?"},
    ]
    # signal present → strip the scraped carousel, keep text
    kept = drop_scraped_carousel(parsed, has_native_carousel=True)
    ck("supersede: scraped property_carousel removed", not any(p.get("type") == "property_carousel" for p in kept), kept)
    ck("supersede: surrounding text preserved", [p for p in kept if p.get("type") == "text"] ==
       [{"type": "text", "markdown": "Here are options"}, {"type": "text", "markdown": "Want details?"}], kept)
    # no native carousel (show-more turn) → keep the scraped carousel (legacy fallback)
    kept2 = drop_scraped_carousel(parsed, has_native_carousel=False)
    ck("no native → scraped carousel retained (legacy pagination)",
       any(p.get("type") == "property_carousel" for p in kept2), kept2)


def t_early_return_keeps_scraped():
    """REGRESSION GUARD: if generate_ui_parts early-returns (api_error / empty / partial)
    despite carousel_items being on the signal, NO native carousel is emitted — so the
    caller MUST keep the scraped carousel. Strip ⟺ a native carousel is ACTUALLY emitted,
    never gated on the signal alone (which would strip-without-replace → user loses cards)."""
    from core.ui_parts import generate_ui_parts, has_native_listing_carousel
    from core.message_parser import drop_scraped_carousel
    from core.channel_adapter import adapt
    items = [{"name": "FitLife", "rent": "₹9,000/mo", "location": "Kurla"}]

    # api_error fires the early-return even though a search recorded carousel_items this turn
    out_err = generate_ui_parts("had trouble", "broker", "u1", "en",
                                signals={"api_error": True, "error_message": "x", "carousel_items": items})
    ck("api_error early-return emits NO listing carousel", not has_native_listing_carousel(out_err), out_err)
    parsed = [{"type": "text", "markdown": "intro"},
              {"type": "property_carousel", "properties": [{"name": "FitLife"}]}]
    kept = drop_scraped_carousel(parsed, has_native_listing_carousel(out_err))
    ck("scraped carousel RETAINED when no native emitted (no strip-without-replace)",
       any(p.get("type") == "property_carousel" for p in kept), kept)

    # partial-booking early-return + carousel_items → also no native → keep scraped
    out_part = generate_ui_parts("held", "broker", "u1", "en",
                                 signals={"booking_held": True, "crm_synced": False, "carousel_items": items})
    ck("partial-booking early-return emits NO listing carousel", not has_native_listing_carousel(out_part), out_part)

    # positive control: a clean search emission DOES carry a native carousel, and the
    # detector survives the web channel adapter (passthrough).
    out_ok = generate_ui_parts("here are options", "broker", "u1", "en",
                               signals={"search_ran": True, "result_count": 1, "carousel_items": items})
    ck("clean search → has_native True", has_native_listing_carousel(out_ok), out_ok)
    ck("has_native survives adapt(web)", has_native_listing_carousel(adapt(out_ok, "web")), True)
    ck("media carousel is NOT counted as a listing carousel",
       not has_native_listing_carousel([{"kind": "carousel", "state": "result",
                                          "data": {"payload": "media", "items": []}, "surface": "inline"}]), True)


def t_comparison_scraper_removed():
    """The legacy comparison_table prose-scraper is GONE. A markdown pipe-table is no longer
    turned into a {type:comparison_table} (D2's native comparison is the only comparison path);
    it renders as plain markdown text instead (the FE renders the table via marked). Deleting
    the scraper also removes a latent double-render (scraped table + native comparison)."""
    import core.message_parser as mp
    from core.message_parser import parse_message_parts
    md = ("Here's how they stack up:\n\n"
          "| Feature | Sunrise | Moon |\n|---|---|---|\n| Rent | 8000 | 9000 |\n| WiFi | Yes | No |\n\n"
          "Sunrise is the better value.")
    parts = parse_message_parts(md, "u_cmp")
    types = [p.get("type") for p in parts]
    ck("no legacy comparison_table from a pipe-table", not any(t == "comparison_table" for t in types), types)
    ck("pipe-table falls back to text part(s)", any(t == "text" for t in types), types)
    ck("_parse_comparison_segments deleted (dead code removed)", not hasattr(mp, "_parse_comparison_segments"), "still present")
    ck("_table_segment_to_part deleted (dead code removed)", not hasattr(mp, "_table_segment_to_part"), "still present")


if __name__ == "__main__":
    print("== build_carousel_items field map =="); t_item_field_map()
    print("== limit / formatting / fallbacks =="); t_limit_format_and_graceful_fallbacks()
    print("== native unit emission =="); t_native_unit_emission()
    print("== supersession guard =="); t_supersession_guard()
    print("== early-return keeps scraped (no strip-without-replace) =="); t_early_return_keeps_scraped()
    print("== comparison scraper removed =="); t_comparison_scraper_removed()
    print(f"\n{_p} passed, {_f} failed")
    sys.exit(1 if _f else 0)
