"""
test_signals.py — request-scoped truth signals recorded at tool seams, read at egress.

Standalone script (matches the repo's gate convention: `check()`/`section_*()`
+ a `__main__` runner; no pytest dependency, runs under `python test_signals.py`).
Assertions mirror Plan 3 Task 4 verbatim (pytest cases converted to the harness).

Run: `python test_signals.py` (exit 0 = pass). No network/Redis/LLM.
"""
import asyncio
import os
import sys

# core.ui_parts imports config (via db.redis_store), which requires a non-empty
# ANTHROPIC_API_KEY. setdefault is not enough: some shells export the var as an
# empty string, which pydantic-settings (env_ignore_empty) treats as missing.
os.environ["ANTHROPIC_API_KEY"] = os.environ.get("ANTHROPIC_API_KEY") or "test-key-not-used"

from core.signals import reset_signals, record_signal, current_signals  # noqa: E402
from core.ui_parts import generate_ui_parts  # noqa: E402

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


def section_reset_clears_the_slate():
    reset_signals()
    record_signal(result_count=0)
    snap = current_signals()
    check("reset: record then read matches one of the two accepted shapes",
          snap == {"search_ran": False, "result_count": 0} or snap == {"result_count": 0},
          repr(snap))
    reset_signals()
    check("reset: clears back to empty", current_signals() == {}, repr(current_signals()))


def section_record_merges_not_replaces():
    reset_signals()
    record_signal(booking_held=True)
    record_signal(crm_synced=False)
    check("record: merges across calls (not replaces)",
          current_signals() == {"booking_held": True, "crm_synced": False},
          repr(current_signals()))


def section_current_signals_returns_a_copy():
    reset_signals()
    record_signal(api_error=True)
    snap = current_signals()
    snap["api_error"] = False
    check("current: returns a copy — mutating it does not change the slate",
          current_signals()["api_error"] is True, repr(current_signals()))


def section_half_succeeded_write_emits_partial_receipt():
    reset_signals()
    record_signal(booking_held=True, crm_synced=False)
    units = generate_ui_parts("Visit booked for Saturday 4pm.", agent="booking",
                              user_id="u1", locale="en", signals=current_signals())
    check("partial: emits confirmation/partial unit",
          any(u["kind"] == "confirmation" and u["state"] == "partial" for u in units),
          repr(units))


def section_zero_result_search_emits_empty_rail():
    reset_signals()
    record_signal(search_ran=True, result_count=0)
    units = generate_ui_parts("No properties matched in that area.", agent="broker",
                              user_id="u1", locale="en", signals=current_signals())
    check("empty: emits status_rail/empty unit",
          any(u["kind"] == "status_rail" and u["state"] == "empty" for u in units),
          repr(units))


def section_signals_survive_asyncio_gather():
    async def _tool_a():
        record_signal(search_ran=True, result_count=0)
    async def _tool_b():
        record_signal(api_error=True)
    async def _drive():
        reset_signals()
        await asyncio.gather(_tool_a(), _tool_b())   # child tasks, copied contexts
        return current_signals()
    result = asyncio.run(_drive())
    check("gather: search_ran propagates from child task", result.get("search_ran") is True,
          f"got {result}")
    check("gather: result_count propagates from child task", result.get("result_count") == 0,
          f"got {result}")
    check("gather: api_error propagates from a sibling child task", result.get("api_error") is True,
          f"got {result}")


if __name__ == "__main__":
    section_reset_clears_the_slate()
    section_record_merges_not_replaces()
    section_current_signals_returns_a_copy()
    section_half_succeeded_write_emits_partial_receipt()
    section_zero_result_search_emits_empty_rail()
    section_signals_survive_asyncio_gather()
    print(f"\n{'='*48}\n  {_passed} passed, {_failed} failed\n{'='*48}")
    sys.exit(1 if _failed else 0)
