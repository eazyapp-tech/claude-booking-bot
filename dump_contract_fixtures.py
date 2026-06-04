"""Generate contract_fixtures.json — the REAL native units the backend emits, for every
kind x state, plus representative detail-sheet carousel items. This is the single source of
truth the FE's real-renderer test (eazypg-chat/tests/unit/backend-fixtures.test.js) runs
through the ACTUAL renderers, and that test_contract_fixtures.py drift-guards.

Run after changing any emitted unit shape:  python dump_contract_fixtures.py
"""
import os, json
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

from core.contract import make_unit  # noqa: E402
from core.ui_parts import (  # noqa: E402
    _to_native, make_error_part, make_empty_part, make_human_handoff_part,
)
from tools.broker.compare import build_comparison_items  # noqa: E402
from tools.broker.search import build_carousel_items  # noqa: E402

FIXTURES_PATH = os.path.join(os.path.dirname(__file__), "contract_fixtures.json")

# Representative `info` dicts (the set_property_info_map payload search.py builds), so the
# listing carousel + its detail-sheet item come from the REAL P4 emitter (build_carousel_items),
# never hand-typed — the FE real-renderer test then covers the actual field map and the drift
# guard bites if it changes. First entry is sheet-rich (gallery + structured sharing).
_LISTING_INFOS = [
    {"property_name": "Sunrise PG", "property_location": "Kurla", "property_rent": "8000",
     "pg_available_for": "Boys", "property_image": "https://img/sunrise.webp",
     "property_link": "https://micro/sunrise", "property_lat": "19.07", "property_long": "72.88",
     "match_score": 82, "distance": "0.5 km", "amenities": "WiFi, AC, Power Backup",
     "images": ["a.jpg", "b.jpg", "c.jpg"],
     "sharing_types_list": [{"label": "Double sharing", "price": "₹8000/mo"},
                            {"label": "Triple sharing", "price": "₹7000/mo"}]},
    {"property_name": "Moon PG", "property_location": "Powai", "property_rent": "9500",
     "pg_available_for": "Boys", "property_image": "https://img/moon.webp",
     "property_link": "https://micro/moon", "property_lat": "19.12", "property_long": "72.91",
     "match_score": 74, "distance": "0.8 km", "amenities": "WiFi"},
]
_CAR_ITEMS, _CAR_CENTER = build_carousel_items(_LISTING_INFOS, 19.07, 72.88, limit=5)

_CMP_INPUT = [
    {"name": "Sunrise PG", "location": "Kurla", "rent": "8000", "score": 82, "amenities": "WiFi",
     "food": "", "services": "", "type": "PG", "available_for": "Boys", "notice_period": "",
     "agreement_period": "", "token_amount": "2000", "distance": "500", "rooms": [], "total_beds": 4,
     "maps_link": "", "microsite": ""},
    {"name": "Moon PG", "location": "Kurla", "rent": "9500", "score": 74, "amenities": "WiFi",
     "food": "", "services": "", "type": "PG", "available_for": "Boys", "notice_period": "",
     "agreement_period": "", "token_amount": "", "distance": "800", "rooms": [], "total_beds": 2,
     "maps_link": "", "microsite": ""},
]


def build_fixtures() -> dict:
    """Every fixture is produced by the REAL backend emitter, never hand-typed."""
    units = [
        ("text/result", _to_native({"type": "text", "text": "Here are some options for you."})),
        ("text/result+sections", _to_native({"type": "expandable_sections", "sections": [
            {"id": "am", "title": "Amenities", "content_type": "pills", "items": ["WiFi", "AC"]}]})),
        ("carousel/result/listing", make_unit("carousel", "result",
            {"payload": "listing", "items": _CAR_ITEMS, "map_center": _CAR_CENTER})),
        ("carousel/result/media", _to_native({"type": "image_gallery", "property_name": "Sunrise PG",
            "images": [{"url": "a.jpg"}, {"url": "b.jpg"}]})),
        ("quick_replies/result", _to_native({"type": "quick_replies",
            "chips": [{"label": "Search PGs", "action": "show pgs"}]})),
        ("action_buttons/result", make_unit("action_buttons", "result", {
            "buttons": [{"label": "Book now", "action": "book", "style": "primary"}]})),
        ("comparison/result", make_unit("comparison", "result", {"items": build_comparison_items(_CMP_INPUT)})),
        ("status_rail/result/ok", _to_native({"type": "status_card", "status": "success",
            "title": "Visit Confirmed!", "subtitle": "Sunrise PG", "details": [{"text": "Tomorrow 5 PM"}]})),
        ("status_rail/result/warn", _to_native({"type": "status_card", "status": "warning",
            "title": "Heads up", "subtitle": "Limited beds"})),
        ("status_rail/empty/warn", make_empty_part("No matches in that area yet.")),
        ("status_rail/error/err", make_error_part("Listings service is down")),
        ("status_rail/partial/warn", make_unit("status_rail", "partial", {
            "variant": "warn", "title": "We'll follow up to confirm",
            "body": "Your request is saved, but a detail didn't sync. Our team will reach out shortly.",
            "retry": False})),
        ("status_rail/handoff/ok", make_human_handoff_part("OxOtel", "en")),
        ("confirmation/awaiting_input", _to_native({"type": "confirmation_card", "title": "Confirm your visit?",
            "subtitle": "Sunrise PG", "confirm_action": "Yes, book it", "cancel_action": "Not now",
            "details": [{"text": "Tomorrow 5 PM"}]})),
        ("map/result", make_unit("map", "result", {"pins": [{"name": "Sunrise", "lat": 19.07, "lng": 72.88}]})),
        ("choice_list/result", make_unit("choice_list", "result", {"options": [
            {"id": "1", "label": "Sunrise PG", "sub": "Kurla"}, {"id": "2", "label": "Moon PG", "sub": "Powai"}]})),
        ("input_request/awaiting_input", make_unit("input_request", "awaiting_input", {
            "input_type": "date", "prompt": "Pick a visit date"})),
    ]
    # Detail-sheet carousel items (composePropertySheet) — rich / thin / minimal.
    # "rich" IS the real P4 carousel item the FE stashes (build_carousel_items output),
    # proving the round-trip search-info → carousel item → detail sheet (gallery + sharing).
    sheet_items = [
        ("rich", _CAR_ITEMS[0]),
        ("thin", {"name": "Sunrise PG", "rent": "8000", "image": "a.jpg", "amenities": "WiFi"}),
        ("minimal", {"name": "Sunrise PG", "rent": "8000"}),
    ]
    return {
        "_generated_by": "dump_contract_fixtures.py",
        "units": [{"label": lbl, "unit": u} for lbl, u in units],
        "sheet_items": [{"label": lbl, "item": it} for lbl, it in sheet_items],
    }


if __name__ == "__main__":
    fixtures = build_fixtures()
    with open(FIXTURES_PATH, "w") as f:
        json.dump(fixtures, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {FIXTURES_PATH}: {len(fixtures['units'])} units + {len(fixtures['sheet_items'])} sheet items")
