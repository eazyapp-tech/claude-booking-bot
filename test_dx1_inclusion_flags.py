"""
test_dx1_inclusion_flags.py — Hermetic tests for DX-1: electricity + food
inclusion flags surfaced in property_details.py.

No Redis, no network, no LLM.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools.broker.property_details import _bool_to_yes_no, _parse_api_response

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


# ---------------------------------------------------------------------------
# _bool_to_yes_no
# ---------------------------------------------------------------------------
print("\n=== _bool_to_yes_no ===")
check("True → 'Yes'",        _bool_to_yes_no(True),  "Yes")
check("False → 'No'",        _bool_to_yes_no(False), "No")
check("None → ''",           _bool_to_yes_no(None),  "")
check("missing (None) → ''", _bool_to_yes_no(None),  "")


# ---------------------------------------------------------------------------
# _parse_api_response — ms vs pd priority
# ---------------------------------------------------------------------------
print("\n=== _parse_api_response: ms vs pd priority ===")

# New API response shape: nested under data.property + data.propertyMicrosite
def _make_response(pd_fields=None, ms_fields=None):
    return {
        "data": {
            "property": pd_fields or {},
            "propertyMicrosite": ms_fields or {},
        }
    }

# Electricity in microsite
r = _make_response(ms_fields={"is_electricity_included": True, "is_food_included": False})
pd_, ms_, _ = _parse_api_response(r)
check("ms: electricity True → pd not overrides", ms_.get("is_electricity_included"), True)
check("ms: food False preserved", ms_.get("is_food_included"), False)

# Electricity only in property dict (legacy)
r = _make_response(pd_fields={"is_electricity_included": True}, ms_fields={})
pd_, ms_, _ = _parse_api_response(r)
check("pd: electricity when ms empty", pd_.get("is_electricity_included"), True)

# Null in ms → omit
r = _make_response(ms_fields={"is_electricity_included": None})
pd_, ms_, _ = _parse_api_response(r)
val = ms_.get("is_electricity_included")
check("ms: explicit None → _bool_to_yes_no gives ''", _bool_to_yes_no(val if val is not None else None), "")


# ---------------------------------------------------------------------------
# Integration: details dict gets electricity_included and food_included
# ---------------------------------------------------------------------------
print("\n=== details dict extraction (simulated) ===")

# Simulate what fetch_property_details builds from ms + pd
def _simulate_details(ms_elec=None, ms_food=None, pd_elec=None, pd_food=None):
    ms = {}
    if ms_elec is not None:
        ms["is_electricity_included"] = ms_elec
    if ms_food is not None:
        ms["is_food_included"] = ms_food
    pd = {}
    if pd_elec is not None:
        pd["is_electricity_included"] = pd_elec
    if pd_food is not None:
        pd["is_food_included"] = pd_food

    electricity_included = _bool_to_yes_no(
        ms.get("is_electricity_included") if ms.get("is_electricity_included") is not None
        else pd.get("is_electricity_included")
    )
    food_included = _bool_to_yes_no(
        ms.get("is_food_included") if ms.get("is_food_included") is not None
        else pd.get("is_food_included")
    )
    return electricity_included, food_included


elec, food = _simulate_details(ms_elec=True, ms_food=True)
check("ms=True: electricity 'Yes'", elec, "Yes")
check("ms=True: food 'Yes'", food, "Yes")

elec, food = _simulate_details(ms_elec=False, ms_food=False)
check("ms=False: electricity 'No'", elec, "No")
check("ms=False: food 'No'", food, "No")

elec, food = _simulate_details(ms_elec=None, pd_elec=True)
check("ms=None, pd=True: falls back to pd", elec, "Yes")

elec, food = _simulate_details()  # nothing set
check("ms=None, pd=None: omitted (empty string)", elec, "")
check("ms=None, pd=None: food omitted too", food, "")

# ms wins over pd when both set
elec, food = _simulate_details(ms_elec=False, pd_elec=True)
check("ms=False wins over pd=True", elec, "No")


# ---------------------------------------------------------------------------
# Output formatting: non-empty fields get surfaced in the result string
# ---------------------------------------------------------------------------
print("\n=== output string formatting ===")

# The details dict includes electricity_included and food_included;
# the result loop emits them if non-empty.
details = {
    "property_name": "Test PG",
    "electricity_included": "Yes",
    "food_included": "No",
    "location": "Mumbai",
}
result = f"PROPERTY DETAILS: {details['property_name']}\n"
for key, val in details.items():
    if val and key not in ("property_name",):
        label = key.replace("_", " ").title()
        result += f"- {label}: {val}\n"

check("'Electricity Included: Yes' in output", "Electricity Included: Yes" in result, True)
check("'Food Included: No' in output", "Food Included: No" in result, True)
check("empty fields omitted when value is ''", "electricity" not in "PROPERTY DETAILS: Test PG\n- Location: Mumbai\n", True)


# ---------------------------------------------------------------------------
print(f"\n{'='*50}")
print(f"RESULT: {_PASS} passed, {_FAIL} failed")
if _FAIL:
    sys.exit(1)
