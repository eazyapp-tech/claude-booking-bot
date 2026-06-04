"""Drift guard for contract_fixtures.json (P3). The committed fixtures are what the FE's
real-renderer test (eazypg-chat/tests/unit/backend-fixtures.test.js) runs through the ACTUAL
renderers. If an emitted unit's shape changes here without regenerating, the FE would be
testing stale output — so this fails until `python dump_contract_fixtures.py` is re-run.

Deterministic, no network/Redis/LLM. Run: python test_contract_fixtures.py
"""
import os, sys, json
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

from dump_contract_fixtures import build_fixtures, FIXTURES_PATH  # noqa: E402
from core.contract import is_valid_unit  # noqa: E402

_p = _f = 0
def ck(n, c, d=""):
    global _p, _f
    print(("  PASS  " if c else "  FAIL  ") + n + ("" if c else "  -> " + str(d)))
    _p += bool(c); _f += (not c)


def main():
    ck("fixtures file exists (run dump_contract_fixtures.py if missing)", os.path.exists(FIXTURES_PATH), FIXTURES_PATH)
    if not os.path.exists(FIXTURES_PATH):
        return
    committed = json.load(open(FIXTURES_PATH))
    fresh = build_fixtures()
    # Compare via canonical JSON so key order / formatting never causes a false diff.
    same = json.dumps(committed, sort_keys=True, ensure_ascii=False) == json.dumps(fresh, sort_keys=True, ensure_ascii=False)
    ck("committed fixtures == freshly generated (no drift; regenerate if this fails)", same,
       "stale — run: python dump_contract_fixtures.py")
    # Every emitted unit must be contract-valid (enum check) — the FE test proves rendering.
    ck("every fixture unit is contract-valid",
       all(is_valid_unit(u["unit"]) for u in committed.get("units", [])),
       [u["label"] for u in committed.get("units", []) if not is_valid_unit(u["unit"])])
    # Coverage floor: every contract kind appears at least once.
    from core.contract import KINDS
    seen = {u["unit"]["kind"] for u in committed.get("units", [])}
    ck("fixtures cover every contract kind", set(KINDS).issubset(seen), sorted(set(KINDS) - seen))


if __name__ == "__main__":
    main()
    print(f"\n{'='*52}\n  {_p} passed, {_f} failed\n{'='*52}")
    sys.exit(1 if _f else 0)
