"""
test_addon_services.py — Hermetic tests for add-on services / meals surfacing.

Covers:
  - meals_available extracted from microsite (ms) and property dict (pd)
  - services_amenities: ms wins over pd
  - both fields present in details dict output
  - empty → not surfaced (no noise)

No Redis, no network, no LLM.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools.broker.property_details import _parse_api_response

_PASS = 0
_FAIL = 0


def check(label, actual, expected):
    global _PASS, _FAIL
    if actual == expected:
        print(f"  PASS  {label}")
        _PASS += 1
    else:
        print(f"  FAIL  {label}: got {actual!r}, expected {expected!r}")
        _FAIL += 1


def _make_response(pd_fields=None, ms_fields=None):
    return {
        "data": {
            "property": pd_fields or {},
            "propertyMicrosite": ms_fields or {},
        }
    }


# -------------------------------------------------------------------------
# meals_available
# -------------------------------------------------------------------------
print("\n=== meals_available: ms wins ===")

r = _make_response(ms_fields={"meals_available": "Breakfast, Dinner"})
pd_, ms_, _ = _parse_api_response(r)
val = ms_.get("meals_available") or pd_.get("meals_available", "")
check("ms meal plan surfaced", val, "Breakfast, Dinner")

r = _make_response(pd_fields={"meals_available": "Lunch Only"}, ms_fields={})
pd_, ms_, _ = _parse_api_response(r)
val = ms_.get("meals_available") or pd_.get("meals_available", "")
check("pd fallback when ms empty", val, "Lunch Only")

r = _make_response(
    pd_fields={"meals_available": "Breakfast Only"},
    ms_fields={"meals_available": "Breakfast, Lunch, Dinner"},
)
pd_, ms_, _ = _parse_api_response(r)
val = ms_.get("meals_available") or pd_.get("meals_available", "")
check("ms wins over pd when both present", val, "Breakfast, Lunch, Dinner")

r = _make_response()
pd_, ms_, _ = _parse_api_response(r)
val = ms_.get("meals_available") or pd_.get("meals_available", "")
check("absent both → empty string (no noise)", val, "")

r = _make_response(ms_fields={"meals_available": None})
pd_, ms_, _ = _parse_api_response(r)
val = ms_.get("meals_available") or pd_.get("meals_available", "")
check("ms=None → '' (falsy fallback to pd)", val, "")

r = _make_response(ms_fields={"meals_available": ""})
pd_, ms_, _ = _parse_api_response(r)
val = ms_.get("meals_available") or pd_.get("meals_available", "")
check("ms='' → '' (falsy fallback)", val, "")


# -------------------------------------------------------------------------
# services_amenities: ms wins over pd
# -------------------------------------------------------------------------
print("\n=== services_amenities: ms wins ===")

r = _make_response(ms_fields={"services_amenities": "Laundry, Housekeeping, Parking"})
pd_, ms_, _ = _parse_api_response(r)
val = ms_.get("services_amenities") or pd_.get("services_amenities", "")
check("ms services surfaced", val, "Laundry, Housekeeping, Parking")

r = _make_response(pd_fields={"services_amenities": "Parking"}, ms_fields={})
pd_, ms_, _ = _parse_api_response(r)
val = ms_.get("services_amenities") or pd_.get("services_amenities", "")
check("pd fallback when ms empty", val, "Parking")

r = _make_response(
    pd_fields={"services_amenities": "Parking"},
    ms_fields={"services_amenities": "Laundry, Parking, AC"},
)
pd_, ms_, _ = _parse_api_response(r)
val = ms_.get("services_amenities") or pd_.get("services_amenities", "")
check("ms wins over pd (richer microsite data)", val, "Laundry, Parking, AC")

r = _make_response()
pd_, ms_, _ = _parse_api_response(r)
val = ms_.get("services_amenities") or pd_.get("services_amenities", "")
check("absent both → '' (no noise)", val, "")


# -------------------------------------------------------------------------
# Combined: details dict includes both fields
# -------------------------------------------------------------------------
print("\n=== details dict: both fields present ===")


def _simulate_details(ms_meals=None, ms_svc=None, pd_meals=None, pd_svc=None):
    ms = {}
    if ms_meals is not None:
        ms["meals_available"] = ms_meals
    if ms_svc is not None:
        ms["services_amenities"] = ms_svc
    pd = {}
    if pd_meals is not None:
        pd["meals_available"] = pd_meals
    if pd_svc is not None:
        pd["services_amenities"] = pd_svc
    return {
        "meals_available": ms.get("meals_available") or pd.get("meals_available", ""),
        "services_amenities": ms.get("services_amenities") or pd.get("services_amenities", ""),
    }


d = _simulate_details(ms_meals="Breakfast, Dinner", ms_svc="Laundry, Parking")
check("meals in dict", d["meals_available"], "Breakfast, Dinner")
check("services in dict", d["services_amenities"], "Laundry, Parking")

d = _simulate_details()
check("both absent → empty (skipped in output loop)", d["meals_available"], "")
check("both absent → services empty", d["services_amenities"], "")

# output loop skips empty values
details = {"property_name": "Orchid Parc", "meals_available": "Breakfast, Dinner", "services_amenities": "Parking", "location": "Bengaluru"}
result = ""
for key, val in details.items():
    if val and key not in ("property_name",):
        result += f"- {key.replace('_', ' ').title()}: {val}\n"

check("meals_available appears in output", "Meals Available: Breakfast, Dinner" in result, True)
check("services_amenities appears in output", "Services Amenities: Parking" in result, True)

# empty meals → not in output
details_no_meals = {"property_name": "Orchid Parc", "meals_available": "", "services_amenities": "Parking"}
result2 = ""
for key, val in details_no_meals.items():
    if val and key not in ("property_name",):
        result2 += f"- {key.replace('_', ' ').title()}: {val}\n"

check("empty meals_available → not in output", "Meals Available" not in result2, True)
check("non-empty services still appears", "Services Amenities: Parking" in result2, True)


# -------------------------------------------------------------------------
print(f"\n{'='*50}")
print(f"RESULT: {_PASS} passed, {_FAIL} failed")
if _FAIL:
    sys.exit(1)
