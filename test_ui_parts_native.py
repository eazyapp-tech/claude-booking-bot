"""
test_ui_parts_native.py — generate_ui_parts emits native {kind,state,data,surface} units.

Standalone script (matches the repo's gate convention: `check()`/`section_*()`
+ a `__main__` runner; no pytest dependency, runs under `python test_ui_parts_native.py`).
Assertions mirror Plan 3 Task 2 verbatim (pytest cases converted to the harness).

Run: `python test_ui_parts_native.py`.
"""
import os
import sys

# core.ui_parts imports config (via db.redis_store), which requires a non-empty
# ANTHROPIC_API_KEY. setdefault is not enough: some shells export the var as an
# empty string, which pydantic-settings (env_ignore_empty) treats as missing.
os.environ["ANTHROPIC_API_KEY"] = os.environ.get("ANTHROPIC_API_KEY") or "test-key-not-used"

from core.contract import is_valid_unit  # noqa: E402
from core.ui_parts import generate_ui_parts, make_error_part  # noqa: E402

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


def _all_valid(prefix, units):
    check(f"{prefix}: returns a list", isinstance(units, list), repr(type(units)))
    for i, u in enumerate(units):
        check(f"{prefix}: unit[{i}] is valid", is_valid_unit(u), repr(u))


def section_plain_text_emits_one_text_unit():
    units = generate_ui_parts("Hello, how can I help?", agent="default", user_id="u1", locale="en")
    _all_valid("plain_text", units)
    check("plain_text: has text/result unit",
          any(u["kind"] == "text" and u["state"] == "result" for u in units), repr(units))


def section_error_part_is_status_rail_error_not_empty():
    u = make_error_part("Listings service is down")
    check("error_part: valid unit", is_valid_unit(u), repr(u))
    check("error_part: kind is status_rail", u["kind"] == "status_rail", repr(u))
    check("error_part: state is error (never empty)", u["state"] == "error", repr(u))
    check("error_part: variant is err", u["data"]["variant"] == "err", repr(u))
    check("error_part: retry is True", u["data"].get("retry") is True, repr(u))


def section_partial_success_is_confirmation_partial():
    units = generate_ui_parts(
        "Your bed is held, but we couldn't sync your details — our team will follow up.",
        agent="booking", user_id="u1", locale="en",
        signals={"booking_held": True, "crm_synced": False},
    )
    _all_valid("partial", units)
    check("partial: has confirmation/partial unit",
          any(u["kind"] == "confirmation" and u["state"] == "partial" for u in units), repr(units))
    partial = next((u for u in units if u["kind"] == "confirmation" and u["state"] == "partial"), None)
    pdata = (partial or {}).get("data", {})
    check("partial: data carries honesty payload (ok+warn lists, body truthy)",
          isinstance(pdata.get("ok"), list) and pdata.get("ok")
          and isinstance(pdata.get("warn"), list) and pdata.get("warn")
          and bool(pdata.get("body")),
          repr(pdata))


def section_empty_listings_is_status_rail_empty_not_error():
    units = generate_ui_parts(
        "No matches in that area yet — want me to widen the search?",
        agent="broker", user_id="u1", locale="en",
        signals={"search_ran": True, "result_count": 0},
    )
    _all_valid("empty", units)
    rails = [u for u in units if u["kind"] == "status_rail"]
    check("empty: has a status_rail unit", bool(rails), repr(units))
    if rails:
        check("empty: state is empty (not error)", rails[0]["state"] == "empty", repr(rails[0]))
        check("empty: variant is not err", rails[0]["data"]["variant"] != "err", repr(rails[0]))


def section_every_unit_validates_for_each_agent():
    for agent in ("default", "broker", "booking", "profile"):
        units = generate_ui_parts("Some reply text.", agent=agent, user_id="u1", locale="en")
        _all_valid(f"agent[{agent}]", units)


if __name__ == "__main__":
    section_plain_text_emits_one_text_unit()
    section_error_part_is_status_rail_error_not_empty()
    section_partial_success_is_confirmation_partial()
    section_empty_listings_is_status_rail_empty_not_error()
    section_every_unit_validates_for_each_agent()
    print(f"\n{'='*48}\n  {_passed} passed, {_failed} failed\n{'='*48}")
    sys.exit(1 if _failed else 0)
