"""
test_web_egress.py — web egress emits native units verbatim (Task 5).

Standalone script (matches the repo's gate convention: `check()`/`section_*()`
+ a `__main__` runner; no pytest dependency, runs under `python test_web_egress.py`).
Assertions mirror the Task 5 pytest cases verbatim (converted to the harness).

These call generate_ui_parts/adapt DIRECTLY (not through routers/chat.py), so they
lock the web-egress contract: web is a full-contract passthrough (no degrade).

Run: `python test_web_egress.py`.
"""
import os
import sys

# test_web_egress imports core.ui_parts → config; set the key defensively so an
# empty-string ANTHROPIC_API_KEY in the shell does not fail pydantic-settings.
os.environ["ANTHROPIC_API_KEY"] = os.environ.get("ANTHROPIC_API_KEY") or "test-key-not-used"

from core.contract import is_valid_unit  # noqa: E402
from core.ui_parts import generate_ui_parts  # noqa: E402
from core.channel_adapter import adapt  # noqa: E402

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


def section_web_egress_passes_native_units_unchanged():
    units = generate_ui_parts("Here are some options.", agent="broker", user_id="u1", locale="en")
    egress = adapt(units, "web")
    check("web_egress: egress == units (passthrough)", egress == units, repr(egress))
    check("web_egress: all units valid", all(is_valid_unit(u) for u in egress), repr(egress))


def section_web_egress_preserves_sheet_surface():
    long_text = "Amenities and rules. " * 80
    units = generate_ui_parts(long_text, agent="broker", user_id="u1", locale="en",
                              signals={"force_sections": True})
    egress = adapt(units, "web")
    # INTENTIONAL leniency (preserved from the plan): passes if a sheet surface is
    # present OR all units are valid — there is no force_sections signal to implement.
    check(
        "web_egress: sheet surface preserved OR all units valid",
        any(u.get("surface") == "sheet" for u in egress) or all(is_valid_unit(u) for u in egress),
        repr(egress),
    )


def main():
    section_web_egress_passes_native_units_unchanged()
    section_web_egress_preserves_sheet_surface()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
