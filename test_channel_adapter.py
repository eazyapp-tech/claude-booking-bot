"""
test_channel_adapter.py — channel-capability adapter (§5 degradation matrix).

Standalone script (matches the repo's gate convention: `check()`/`section_*()`
+ a `__main__` runner; no pytest dependency, runs under `python test_channel_adapter.py`).
Assertions mirror Task 3 verbatim (pytest cases converted to the harness).
Invariant under test: text always survives; rich components are accelerators, never load-bearing.

Run: `python test_channel_adapter.py`.
"""
import os
import sys

# core.channel_adapter only imports core.contract.make_unit (pure, json/pathlib),
# but set the key defensively to match the established convention: some shells
# export ANTHROPIC_API_KEY as an empty string, which pydantic-settings treats as
# missing if anything in the import chain ever touches config.
os.environ["ANTHROPIC_API_KEY"] = os.environ.get("ANTHROPIC_API_KEY") or "test-key-not-used"

from core.contract import make_unit  # noqa: E402
from core.channel_adapter import adapt, to_plain_text, CHANNELS  # noqa: E402

_passed = 0
_failed = 0


def check(name, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


def section_web_is_passthrough():
    units = [make_unit("carousel", "result", {"payload": "listing", "items": [{"name": "Maple"}]})]
    out = adapt(units, "web")
    check("web_is_passthrough: out == units (no downgrade)", out == units, repr(out))


def section_whatsapp_carousel_listing_becomes_list_rows_capped_at_10():
    items = [{"name": f"PG {i}"} for i in range(15)]
    units = [make_unit("carousel", "result", {"payload": "listing", "items": items})]
    out = adapt(units, "whatsapp")
    rows = [u for u in out if u["kind"] == "choice_list"]
    check("wa_listing: listing carousel degrades to choice_list", bool(rows), repr(out))
    if rows:
        check("wa_listing: options capped at 10",
              len(rows[0]["data"]["options"]) <= 10, repr(rows[0]["data"]["options"]))


def section_whatsapp_quick_replies_capped_at_3():
    units = [make_unit("quick_replies", "result", {"chips": ["a", "b", "c", "d", "e"]})]
    out = adapt(units, "whatsapp")
    qr = [u for u in out if u["kind"] == "quick_replies"]
    check("wa_qr: quick_replies unit present", bool(qr), repr(out))
    if qr:
        check("wa_qr: chips capped at 3", len(qr[0]["data"]["chips"]) <= 3, repr(qr[0]["data"]["chips"]))


def section_whatsapp_map_degrades_to_deeplink_text():
    units = [make_unit("map", "result", {"pins": [{"lat": 19.07, "lng": 72.87, "label": "Maple"}]})]
    out = adapt(units, "whatsapp")
    check("wa_map: no map unit survives", all(u["kind"] != "map" for u in out), repr(out))
    text = " ".join(u["data"].get("text", "") for u in out if u["kind"] == "text")
    check("wa_map: text contains google.com/maps deep-link", "google.com/maps" in text, repr(text))


def section_status_rail_error_survives_as_prefixed_text_on_whatsapp():
    units = [make_unit("status_rail", "error", {"variant": "err", "title": "Listings down", "retry": True})]
    out = adapt(units, "whatsapp")
    joined = " ".join(u["data"].get("text", u["data"].get("title", "")) for u in out)
    check("wa_status: error title survives in text", "Listings down" in joined, repr(joined))


def section_plain_text_flattens_everything_to_text():
    units = [
        make_unit("carousel", "result", {"payload": "listing", "items": [{"name": "Maple"}, {"name": "Oak"}]}),
        make_unit("quick_replies", "result", {"chips": ["Book", "More"]}),
    ]
    out = adapt(units, "plain")
    check("plain: everything flattened to text units",
          all(u["kind"] == "text" for u in out), repr([u["kind"] for u in out]))


def section_text_always_survives_every_channel():
    units = [make_unit("text", "result", {"text": "Hello"})]
    for ch in CHANNELS:
        out = adapt(units, ch)
        survives = any(u["kind"] == "text" and "Hello" in u["data"].get("text", "") for u in out)
        check(f"text_survives[{ch}]: 'Hello' text unit present", survives, repr(out))


def section_to_plain_text_numbers_listing_items():
    u = make_unit("carousel", "result", {"payload": "listing", "items": [{"name": "Maple"}, {"name": "Oak"}]})
    txt = to_plain_text(u)
    check("to_plain_text: numbers + names present",
          "1." in txt and "Maple" in txt and "2." in txt and "Oak" in txt, repr(txt))


def section_empty_units_list_is_safe():
    for ch in ("web", "whatsapp", "plain"):
        check(f"empty_units[{ch}]: adapt([], ch) == []", adapt([], ch) == [], repr(ch))


def section_to_plain_text_graceful_on_empty_data():
    cases = [
        ("map", make_unit("map", "result", {})),
        ("carousel_empty_listing", make_unit("carousel", "result", {"payload": "listing", "items": []})),
        ("comparison", make_unit("comparison", "result", {})),
    ]
    for name, u in cases:
        raised = False
        result = None
        try:
            result = to_plain_text(u)
        except Exception as e:  # noqa: BLE001
            raised = True
            result = repr(e)
        check(f"to_plain_text_empty[{name}]: no exception + returns str",
              (not raised) and isinstance(result, str), repr(result))


def section_whatsapp_comparison_degrades_to_text():
    units = [make_unit("comparison", "result", {"names": ["A", "B"], "rows": [["x", "y"]]})]
    out = adapt(units, "whatsapp")
    check("wa_comparison: all returned units are text",
          all(u["kind"] == "text" for u in out), repr([u["kind"] for u in out]))
    text = " ".join(u["data"].get("text", "") for u in out)
    check("wa_comparison: text contains 'A' and 'x'", "A" in text and "x" in text, repr(text))


def section_whatsapp_malformed_carousel_degrades_to_text():
    # No payload key → not a valid listing/visit/media carousel. Must NOT pass
    # through as a raw carousel (locks in Fix #1: "carousel" removed from WA caps).
    units = [make_unit("carousel", "result", {"items": [{"name": "Z"}]})]
    out = adapt(units, "whatsapp")
    check("wa_malformed_carousel: no carousel unit survives",
          all(u["kind"] != "carousel" for u in out), repr([u["kind"] for u in out]))


def main():
    section_web_is_passthrough()
    section_whatsapp_carousel_listing_becomes_list_rows_capped_at_10()
    section_whatsapp_quick_replies_capped_at_3()
    section_whatsapp_map_degrades_to_deeplink_text()
    section_status_rail_error_survives_as_prefixed_text_on_whatsapp()
    section_plain_text_flattens_everything_to_text()
    section_text_always_survives_every_channel()
    section_to_plain_text_numbers_listing_items()
    section_empty_units_list_is_safe()
    section_to_plain_text_graceful_on_empty_data()
    section_whatsapp_comparison_degrades_to_text()
    section_whatsapp_malformed_carousel_degrades_to_text()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
