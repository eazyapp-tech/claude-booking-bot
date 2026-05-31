"""
test_tenant_isolation.py — Web-channel multi-tenant isolation regression test.

Proves the security invariant behind Wave 1 #1: the web channel resolves brand
identity SERVER-SIDE from the verified link token only, and NEVER from the
client-supplied brand_hash / pg_ids in the request body.

Deterministic: no Redis, no network, no LLM. The two server-side lookups
(get_brand_by_token, get_default_brand_config) are patched with an in-memory
brand directory so the trust logic in core.tenancy.resolve_web_brand is tested
in isolation. Run: `python test_tenant_isolation.py` (exit 0 = all pass).
"""

import os
import sys
from unittest.mock import patch

# resolve_web_brand → db.redis_store → config.settings (needs an API key at import).
# A dummy keeps the test self-contained and CI-runnable without real secrets.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

from core.tenancy import resolve_web_brand  # noqa: E402

# ── In-memory brand directory (stands in for Redis brand_config / brand_token) ──
BRAND_A = {
    "brand_hash": "aaaaaaaaaaaaaaaa",
    "pg_ids": ["a_pg_1", "a_pg_2"],
    "brand_name": "BrandA",
    "cities": "Mumbai",
    "areas": "Andheri",
}
BRAND_B = {
    "brand_hash": "bbbbbbbbbbbbbbbb",
    "pg_ids": ["b_pg_1", "b_pg_2"],
    "brand_name": "BrandB",
    "cities": "Pune",
    "areas": "Baner",
}
TOKENS = {"tokenA": BRAND_A, "tokenB": BRAND_B}


def _fake_get_brand_by_token(token):
    return TOKENS.get(token)


def _fake_default_is_a():
    return BRAND_A


def _fake_default_none():
    return None


# ── Test harness ───────────────────────────────────────────────────────────
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


def resolve(brand_token, account_values, default=_fake_default_is_a):
    with patch("core.tenancy.get_brand_by_token", _fake_get_brand_by_token), \
         patch("core.tenancy.get_default_brand_config", default):
        return resolve_web_brand(brand_token, account_values)


# ── Cases ────────────────────────────────────────────────────────────────
print("Web-channel tenant isolation\n")

# 1. CORE: valid token A + body claiming Brand B → must resolve A, never B.
bh, pg_ids, safe = resolve("tokenA", {"brand_hash": BRAND_B["brand_hash"], "pg_ids": BRAND_B["pg_ids"]})
check("token A + body claims B → brand_hash is A", bh == BRAND_A["brand_hash"], f"got {bh}")
check("token A + body claims B → pg_ids are A's", pg_ids == BRAND_A["pg_ids"], f"got {pg_ids}")
check("token A + body claims B → no B pg_ids leak", all(p not in pg_ids for p in BRAND_B["pg_ids"]))
check("token A → safe_account brand_hash is A", safe["brand_hash"] == BRAND_A["brand_hash"])
check("token A → safe_account pg_ids are A's", safe["pg_ids"] == BRAND_A["pg_ids"])

# 2. CORE: no token + body claiming Brand B → must resolve default (A), never B.
bh, pg_ids, _ = resolve("", {"brand_hash": BRAND_B["brand_hash"], "pg_ids": BRAND_B["pg_ids"]})
check("no token + body claims B → resolves default A", bh == BRAND_A["brand_hash"], f"got {bh}")
check("no token + body claims B → no B pg_ids leak", all(p not in pg_ids for p in BRAND_B["pg_ids"]))

# 3. Legitimate access: valid token B → resolves B.
bh, pg_ids, _ = resolve("tokenB", {})
check("token B → brand_hash is B", bh == BRAND_B["brand_hash"], f"got {bh}")
check("token B → pg_ids are B's", pg_ids == BRAND_B["pg_ids"], f"got {pg_ids}")

# 4. brand_token wins over account_values['token'] (defense against split signals).
bh, _, _ = resolve("tokenA", {"token": "tokenB"})
check("brand_token A beats account_values.token B → A", bh == BRAND_A["brand_hash"], f"got {bh}")

# 5. Legacy token-in-body path works, but body's claimed hash/pg_ids are ignored.
bh, pg_ids, _ = resolve("", {"token": "tokenB", "brand_hash": BRAND_A["brand_hash"], "pg_ids": ["spoof"]})
check("legacy account_values.token B → resolves B", bh == BRAND_B["brand_hash"], f"got {bh}")
check("legacy path ignores body brand_hash", bh != BRAND_A["brand_hash"])
check("legacy path ignores body pg_ids", "spoof" not in pg_ids and pg_ids == BRAND_B["pg_ids"], f"got {pg_ids}")

# 6. Invalid token + no default brand → brand-less ("", [], {}), never the client's claim.
bh, pg_ids, safe = resolve("nonexistent", {"brand_hash": BRAND_B["brand_hash"], "pg_ids": BRAND_B["pg_ids"]}, default=_fake_default_none)
check("invalid token + no default → empty brand_hash", bh == "", f"got {bh}")
check("invalid token + no default → empty pg_ids", pg_ids == [], f"got {pg_ids}")
check("invalid token + no default → empty safe dict", safe == {}, f"got {safe}")

# 7. None account_values is handled gracefully.
bh, pg_ids, _ = resolve("tokenA", None)
check("None account_values → resolves token A", bh == BRAND_A["brand_hash"], f"got {bh}")


# ── Summary ─────────────────────────────────────────────────────────────────
print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
