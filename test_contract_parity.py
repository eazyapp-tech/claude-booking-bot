"""
test_contract_parity.py — proves the derived contract module matches its own
contract.json (within-repo), and that the backend + frontend contract.json files
agree when both are checked out side-by-side (cross-repo). The cross-repo block
SKIPS (never fails) when the sibling eazypg-chat repo is absent — the two repos
are separately version-controlled and CI may run either in isolation.

Standalone script (repo's gate convention; no pytest). Assertions mirror Plan 3
Task 1. Run: `python test_contract_parity.py`.
"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

from core import contract  # noqa: E402

# This test file lives at the backend repo ROOT (the gate runs `python test_x.py`
# from there), so the backend json is ./core/contract.json and the sibling
# frontend json is ../eazypg-chat/src/contract.json.
_BACKEND_JSON = Path(__file__).resolve().parent / "core" / "contract.json"
_CANON = json.loads(_BACKEND_JSON.read_text(encoding="utf-8"))
_FRONTEND_JSON = Path(__file__).resolve().parent.parent / "eazypg-chat" / "src" / "contract.json"

_KEYS = ["kinds", "states", "surfaces", "carousel_payloads", "status_variants"]

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


def section_within_repo():
    check("module KINDS == contract.json kinds", contract.KINDS == frozenset(_CANON["kinds"]))
    check("module STATES == contract.json states", contract.STATES == frozenset(_CANON["states"]))
    check("module SURFACES == contract.json surfaces", contract.SURFACES == frozenset(_CANON["surfaces"]))
    check("module CAROUSEL_PAYLOADS == contract.json", contract.CAROUSEL_PAYLOADS == frozenset(_CANON["carousel_payloads"]))
    check("module STATUS_VARIANTS == contract.json", contract.STATUS_VARIANTS == frozenset(_CANON["status_variants"]))


def section_cross_repo():
    if not _FRONTEND_JSON.exists():
        print(f"  SKIP  cross-repo parity (sibling {_FRONTEND_JSON} not checked out)")
        return
    frontend = json.loads(_FRONTEND_JSON.read_text(encoding="utf-8"))
    for key in _KEYS:
        check(f"cross-repo {key} agree (backend == frontend)",
              sorted(_CANON[key]) == sorted(frontend[key]),
              f"backend={sorted(_CANON[key])} frontend={sorted(frontend.get(key))}")


if __name__ == "__main__":
    section_within_repo()
    section_cross_repo()
    print(f"\n{'='*48}\n  {_passed} passed, {_failed} failed\n{'='*48}")
    sys.exit(1 if _failed else 0)
