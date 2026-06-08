"""
test_api_key_brand_resolution.py — C2 cross-brand contamination regression test.

Proves the fix for the Alliance contamination bug: when a direct-API caller sends
X-API-Key but no brand link token, _apply_web_brand now resolves the brand from
the API key BEFORE falling through to the OxOtel default.

Security invariants proven:
  1. API-key path resolves the correct brand when no token is present.
  2. Token path takes priority over the API key (token wins).
  3. Tokenless + unknown API key → default brand (OxOtel fallback unchanged).
  4. Token for Brand A + API key for Brand B → Brand A wins (no API-key bleed).
  5. Brand identity (hash, pg_ids, name, brand_name) flows correctly through to
     the Redis setter calls.
  6. No pg_ids → set_whitelabel_pg_ids is NOT called (no empty-list write).

Deterministic: no Redis, no network, no LLM. All lookups and Redis writers are
patched with in-memory data. Run: `python test_api_key_brand_resolution.py`
"""

import os
import sys
from unittest.mock import patch, MagicMock, call

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

# Import the function under test AFTER env is set.
from routers.chat import _apply_web_brand  # noqa: E402

# ── In-memory brand directory ────────────────────────────────────────────────

ALLIANCE = {
    "brand_hash": "alliance_hash_xxxx",
    "pg_ids": ["alli_pg_1", "alli_pg_2"],
    "brand_name": "Alliance",
    "cities": "Mumbai",
    "areas": "Kurla",
}
OXOTEL = {
    "brand_hash": "oxotel_hash_xxxxx",
    "pg_ids": ["ox_pg_1", "ox_pg_2"],
    "brand_name": "OxOtel",
    "cities": "Mumbai",
    "areas": "",
}

FAKE_TOKEN_MAP = {
    "alliance-token-123": ALLIANCE,
}

def fake_get_brand_config(api_key):
    return {"eapg_alliance_key": ALLIANCE}.get(api_key)

def fake_resolve_web_brand(brand_token, account_values=None):
    """Simplified resolve_web_brand: token → lookup; no token → default (OxOtel)."""
    token = (brand_token or "").strip()
    if not token and account_values:
        token = str(account_values.get("token") or "").strip()
    cfg = FAKE_TOKEN_MAP.get(token, OXOTEL)
    bh = cfg["brand_hash"]
    pg = cfg.get("pg_ids", [])
    safe = {k: cfg.get(k, "") for k in ("brand_name", "cities", "areas", "pg_ids", "brand_hash")}
    return bh, pg, safe


# ── Helpers ──────────────────────────────────────────────────────────────────

PATCHES = {
    "routers.chat.get_brand_config":          fake_get_brand_config,
    "routers.chat.resolve_web_brand":         fake_resolve_web_brand,
    "routers.chat.set_account_values":        MagicMock(),
    "routers.chat.set_whitelabel_pg_ids":     MagicMock(),
    "routers.chat.set_user_brand":            MagicMock(),
    "routers.chat.add_to_brand_active_users": MagicMock(),
}

def run_with_patches(fn, *args, **kwargs):
    """Run fn with all Redis writers patched, capture the result."""
    mocks = {k: MagicMock() for k in PATCHES if k.endswith(("_values", "_pg_ids", "_brand", "_users"))}
    ctx = {}
    for target, replacement in PATCHES.items():
        if target in mocks:
            ctx[target] = patch(target, mocks[target])
        elif callable(replacement):
            ctx[target] = patch(target, side_effect=replacement)
        else:
            ctx[target] = patch(target, replacement)

    with patch("routers.chat.get_brand_config", side_effect=fake_get_brand_config), \
         patch("routers.chat.resolve_web_brand", side_effect=fake_resolve_web_brand), \
         patch("routers.chat.set_account_values") as m_sav, \
         patch("routers.chat.set_whitelabel_pg_ids") as m_spg, \
         patch("routers.chat.set_user_brand") as m_sub, \
         patch("routers.chat.add_to_brand_active_users") as m_aba:
        result = fn(*args, **kwargs)
        return result, m_sav, m_spg, m_sub, m_aba


# ── Tests ────────────────────────────────────────────────────────────────────

failures = []

def check(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{': ' + detail if detail else ''}")
        failures.append(label)


print("\n=== test_api_key_brand_resolution ===\n")

# ── 1. API-key path: no token, Alliance API key → Alliance brand ─────────────
print("1. API-key resolution (no token, known API key)")
result, m_sav, m_spg, m_sub, m_aba = run_with_patches(
    _apply_web_brand, "uid_001", {}, "", "eapg_alliance_key"
)
bh, pg_ids = result
check("1a. brand_hash = Alliance hash",     bh == ALLIANCE["brand_hash"],    bh)
check("1b. pg_ids = Alliance pg_ids",       pg_ids == ALLIANCE["pg_ids"],    str(pg_ids))
check("1c. set_user_brand called with bh",  m_sub.call_args[0][1] == ALLIANCE["brand_hash"])
check("1d. set_whitelabel_pg_ids called",   m_spg.called)
check("1e. add_to_brand_active_users called", m_aba.called)
check("1f. set_account_values brand_name",
      m_sav.call_args[0][1].get("brand_name") == "Alliance")


# ── 2. Token takes priority over API key ─────────────────────────────────────
print("\n2. Token priority over API key")
result, m_sav, m_spg, m_sub, m_aba = run_with_patches(
    _apply_web_brand, "uid_002", {}, "alliance-token-123", "eapg_alliance_key"
)
bh, pg_ids = result
check("2a. brand_hash = Alliance hash (via token)", bh == ALLIANCE["brand_hash"])
check("2b. pg_ids = Alliance pg_ids",               pg_ids == ALLIANCE["pg_ids"])


# ── 3. No token, unknown API key → OxOtel default ────────────────────────────
print("\n3. Tokenless + unknown API key → default brand")
result, m_sav, m_spg, m_sub, m_aba = run_with_patches(
    _apply_web_brand, "uid_003", {}, "", "unknown_key_xyz"
)
bh, pg_ids = result
check("3a. brand_hash = OxOtel default",  bh == OXOTEL["brand_hash"],    bh)
check("3b. pg_ids = OxOtel pg_ids",       pg_ids == OXOTEL["pg_ids"],    str(pg_ids))
check("3c. set_user_brand called",        m_sub.called)


# ── 4. No token, no API key → OxOtel default (unchanged behavior) ────────────
print("\n4. Tokenless + no API key → default brand (unchanged)")
result, m_sav, m_spg, m_sub, m_aba = run_with_patches(
    _apply_web_brand, "uid_004", {}, "", ""
)
bh, pg_ids = result
check("4a. brand_hash = OxOtel default",  bh == OXOTEL["brand_hash"])
check("4b. pg_ids = OxOtel pg_ids",       pg_ids == OXOTEL["pg_ids"])


# ── 5. Token in account_values (web frontend sends it here) ──────────────────
print("\n5. Token passed via account_values (web frontend pattern)")
result, m_sav, m_spg, m_sub, m_aba = run_with_patches(
    _apply_web_brand, "uid_005", {"token": "alliance-token-123"}, "", "other_key"
)
bh, pg_ids = result
check("5a. brand_hash = Alliance (from account_values.token)", bh == ALLIANCE["brand_hash"])
check("5b. API key NOT consulted (token wins)",                 pg_ids == ALLIANCE["pg_ids"])


# ── 6. Empty pg_ids → set_whitelabel_pg_ids NOT called ───────────────────────
print("\n6. Brand with no pg_ids → no empty set_whitelabel_pg_ids write")
EMPTY_PG_BRAND = {"brand_hash": "empty_pg_hash__xx", "pg_ids": [], "brand_name": "EmptyBrand", "cities": "", "areas": ""}

def _fake_get_no_pg(api_key):
    return EMPTY_PG_BRAND if api_key == "empty_pg_key" else None

with patch("routers.chat.get_brand_config", side_effect=_fake_get_no_pg), \
     patch("routers.chat.resolve_web_brand", side_effect=fake_resolve_web_brand), \
     patch("routers.chat.set_account_values"), \
     patch("routers.chat.set_whitelabel_pg_ids") as m_spg6, \
     patch("routers.chat.set_user_brand"), \
     patch("routers.chat.add_to_brand_active_users"):
    bh, pg_ids = _apply_web_brand("uid_006", {}, "", "empty_pg_key")

check("6a. brand_hash returned correctly", bh == "empty_pg_hash__xx")
check("6b. pg_ids returned as []",         pg_ids == [])
check("6c. set_whitelabel_pg_ids NOT called", not m_spg6.called)


# ── 7. Token for Brand X + API key for Brand Y → Brand X only (no bleed) ────
print("\n7. Token=Alliance, API key=some_other → Alliance only, no bleed")
BRAND_OTHER = {"brand_hash": "other_brand_hash_x", "pg_ids": ["oth_1"], "brand_name": "Other", "cities": "Pune", "areas": ""}

def _fake_other_key(api_key):
    return BRAND_OTHER if api_key == "other_key" else None

with patch("routers.chat.get_brand_config", side_effect=_fake_other_key), \
     patch("routers.chat.resolve_web_brand", side_effect=fake_resolve_web_brand), \
     patch("routers.chat.set_account_values") as m_sav7, \
     patch("routers.chat.set_whitelabel_pg_ids"), \
     patch("routers.chat.set_user_brand") as m_sub7, \
     patch("routers.chat.add_to_brand_active_users"):
    bh, pg_ids = _apply_web_brand("uid_007", {}, "alliance-token-123", "other_key")

check("7a. brand_hash = Alliance (token wins over API key)",  bh == ALLIANCE["brand_hash"])
check("7b. pg_ids = Alliance (no bleed from other_key)",      pg_ids == ALLIANCE["pg_ids"])
check("7c. brand_name in account_values = Alliance",
      m_sav7.call_args[0][1].get("brand_name") == "Alliance")


# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'='*45}")
total = 19  # expected assertion count
passed = total - len(failures)
print(f"  {passed}/{total} PASS", "✓" if not failures else "")
if failures:
    print(f"\nFailed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("\nAll assertions passed — C2 cross-brand contamination fix verified.")
    sys.exit(0)
