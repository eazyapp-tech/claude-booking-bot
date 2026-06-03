"""
test_contract.py — canonical UI contract vocabulary + shape regression test.

Standalone script (matches the repo's gate convention: `check()`/`section_*()`
+ a `__main__` runner; no pytest dependency, runs under `python test_contract.py`).
Assertions mirror Plan 3 Task 1 verbatim.

Run: `python test_contract.py`.
"""
import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

from core.contract import (  # noqa: E402
    KINDS, STATES, SURFACES, make_unit, is_valid_unit,
)

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


def section_vocabulary():
    check("kinds match canon (10 locked)", KINDS == {
        "text", "carousel", "choice_list", "quick_replies", "action_buttons",
        "comparison", "map", "confirmation", "status_rail", "input_request",
    }, KINDS)
    check("states match canon incl awaiting_input (7 locked)", STATES == {
        "thinking", "streaming", "result", "partial",
        "awaiting_input", "error", "empty",
    }, STATES)
    # the input_request KIND and the awaiting_input STATE must not collide
    check("input_request is a KIND", "input_request" in KINDS)
    check("awaiting_input is a STATE", "awaiting_input" in STATES)
    check("input_request is NOT a STATE", "input_request" not in STATES)
    check("surfaces match canon", SURFACES == {"inline", "sheet"}, SURFACES)
    check("error is not empty", "error" in STATES and "empty" in STATES and "error" != "empty")


def section_shape():
    u = make_unit("text", "result", {"text": "hi"})
    check("make_unit defaults surface to inline",
          u == {"kind": "text", "state": "result", "data": {"text": "hi"}, "surface": "inline"}, u)
    u2 = make_unit("carousel", "result", {"payload": "listing", "items": []}, surface="sheet")
    check("make_unit accepts sheet surface", u2["surface"] == "sheet")
    check("is_valid_unit accepts a good unit",
          is_valid_unit(make_unit("status_rail", "error", {"variant": "err", "title": "x"})))
    check("is_valid_unit rejects unknown kind",
          not is_valid_unit({"kind": "banner", "state": "result", "data": {}, "surface": "inline"}))
    check("is_valid_unit rejects unknown state",
          not is_valid_unit({"kind": "text", "state": "input_request", "data": {}, "surface": "inline"}))
    check("is_valid_unit rejects non-dict data",
          not is_valid_unit({"kind": "text", "state": "result", "data": "nope", "surface": "inline"}))


if __name__ == "__main__":
    section_vocabulary()
    section_shape()
    print(f"\n{'='*48}\n  {_passed} passed, {_failed} failed\n{'='*48}")
    sys.exit(1 if _failed else 0)
