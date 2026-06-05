"""Hermetic regression for self-serve brand accounts (no Redis/network/LLM)."""
import os, sys
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

# ---- in-memory fake Redis -------------------------------------------------
import db.redis._base as _base

class _FakePipe:
    def __init__(self, store): self.store = store; self._ops = []
    def set(self, k, v): self._ops.append((k, v)); return self
    def execute(self):
        for k, v in self._ops:
            self.store[k] = v.encode() if isinstance(v, str) else v
        self._ops = []

class _FakeRedis:
    def __init__(self): self.store = {}
    def get(self, k): return self.store.get(k)
    def set(self, k, v): self.store[k] = v.encode() if isinstance(v, str) else v
    def setex(self, k, ttl, v): self.store[k] = v.encode() if isinstance(v, str) else v
    def delete(self, k): self.store.pop(k, None)
    def exists(self, k): return 1 if k in self.store else 0
    def incr(self, k):
        v = int(self.store.get(k, 0)) + 1
        self.store[k] = str(v).encode()
        return v
    def expire(self, k, ttl): pass
    def pipeline(self): return _FakePipe(self.store)

_fake = _FakeRedis()
_base._r = lambda: _fake

# Rebind _r in the domain modules that imported it by value.
import db.redis.brand as _brand
import db.redis.accounts as _accounts
_brand._r = lambda: _fake
_accounts._r = lambda: _fake

# ---- tiny harness ---------------------------------------------------------
_passed = 0; _failed = 0
def check(name, cond):
    global _passed, _failed
    if cond: _passed += 1; print(f"  PASS {name}")
    else: _failed += 1; print(f"  FAIL {name}")

# ---- store tests ----------------------------------------------------------
from db.redis.accounts import (
    get_account, save_account, account_exists,
    set_email_verify_token, consume_email_verify_token,
)

def test_store():
    print("test_store")
    check("absent account is None", get_account("nobody@example.com") is None)
    save_account({"email": "a@b.com", "password_sha256": "x", "api_key": "eapg_k", "brand_hash": "h", "email_verified": False})
    check("account round-trips", get_account("a@b.com")["api_key"] == "eapg_k")
    check("email lookup is case-insensitive", get_account("A@B.COM")["api_key"] == "eapg_k")
    check("account_exists true", account_exists("a@b.com") is True)
    check("account_exists false", account_exists("z@z.com") is False)
    set_email_verify_token("tok123", "a@b.com")
    check("token resolves to email", consume_email_verify_token("tok123") == "a@b.com")
    check("token is single-use", consume_email_verify_token("tok123") is None)
    check("unknown token is None", consume_email_verify_token("nope") is None)

from config import settings
from core.demo_brand import build_demo_config, provision_demo_brand
from db.redis.brand import get_brand_config, _brand_hash

def test_demo_brand():
    print("test_demo_brand")
    settings.DEMO_PG_IDS = ["pg_demo_1", "pg_demo_2"]
    settings.DEMO_CITIES = ["Mumbai"]
    settings.DEMO_AREAS = ["Kurla"]
    cfg = build_demo_config("Acme PG")
    check("demo brand_name", cfg["brand_name"] == "Acme PG")
    check("demo pg_ids copied", cfg["pg_ids"] == ["pg_demo_1", "pg_demo_2"])
    check("demo is_demo flag", cfg["is_demo"] is True)
    check("demo status", cfg["status"] == "demo")
    check("demo has link token", bool(cfg["brand_link_token"]))
    check("blank name → default", build_demo_config("")["brand_name"] == "My Demo PG")

    api_key = "eapg_demotest"
    provisioned = provision_demo_brand(api_key, "Acme PG")
    stored = get_brand_config(api_key)
    check("provision persists brand_config", stored is not None)
    check("provision sets brand_hash", stored["brand_hash"] == _brand_hash(api_key))
    check("provision keeps pg_ids", stored["pg_ids"] == ["pg_demo_1", "pg_demo_2"])
    check("provision returns same token", provisioned["brand_link_token"] == stored["brand_link_token"])

from core.accounts import signup, validate_signup, _verify_password, check_signup_rate

def test_signup():
    print("test_signup")
    settings.DEMO_PG_IDS = ["pg_demo_1"]
    check("bad email rejected", validate_signup("nope", "password123") is not None)
    check("short password rejected", validate_signup("a@b.com", "short") is not None)
    check("valid input ok", validate_signup("a@b.com", "password123") is None)

    result, reason = signup("New@Brand.com", "password123", "Acme PG")
    check("signup ok", reason == "ok")
    check("api_key prefix", result["api_key"].startswith("eapg_"))
    check("returns link token", bool(result["brand_link_token"]))

    acct = get_account("new@brand.com")
    check("account stored (lowercased)", acct is not None)
    check("password stored hashed (salted scrypt)", bool(acct.get("password_hash")) and acct["password_hash"] != "password123")
    check("per-user salt stored", bool(acct.get("password_salt")))
    check("password verifies via scrypt", _verify_password("password123", acct["password_salt"], acct["password_hash"]) is True)
    check("wrong password fails verify", _verify_password("nope", acct["password_salt"], acct["password_hash"]) is False)
    check("no plaintext password stored", "password" not in acct)
    check("no reversible sha256 field", "password_sha256" not in acct)
    check("email starts unverified", acct["email_verified"] is False)

    # Same password for two users must yield different hashes (per-user salt).
    signup("salt2@brand.com", "password123", "Salt2")
    a1 = get_account("new@brand.com"); a2 = get_account("salt2@brand.com")
    check("identical passwords hash differently", a1["password_hash"] != a2["password_hash"])

    brand = get_brand_config(result["api_key"])
    check("demo brand provisioned", brand is not None and brand["is_demo"] is True)
    check("verify token issued", bool(result["verify_token"]))

    _, dup_reason = signup("new@brand.com", "password123", "Dup")
    check("duplicate email rejected", dup_reason == "exists")

    _, bad_reason = signup("bademail", "password123", "X")
    check("invalid signup reason", bad_reason.startswith("invalid:"))

from core.accounts import verify_login, verify_email

def test_login_and_verify():
    print("test_login_and_verify")
    settings.DEMO_PG_IDS = ["pg_demo_1"]
    result, reason = signup("login@brand.com", "password123", "Acme")
    assert reason == "ok"

    key, lr = verify_login("login@brand.com", "password123")
    check("correct login ok", lr == "ok")
    check("login returns the brand key", key == result["api_key"])
    check("returned key resolves to brand", get_brand_config(key) is not None)

    check("case-insensitive login", verify_login("LOGIN@BRAND.COM", "password123")[1] == "ok")
    check("wrong password rejected", verify_login("login@brand.com", "WRONG")[1] == "invalid")
    check("unknown user rejected", verify_login("ghost@x.com", "password123")[1] == "invalid")

    # email verification
    check("starts unverified", get_account("login@brand.com")["email_verified"] is False)
    check("verify ok", verify_email(result["verify_token"]) is True)
    check("now verified", get_account("login@brand.com")["email_verified"] is True)
    check("token single-use", verify_email(result["verify_token"]) is False)
    check("bad token rejected", verify_email("garbage") is False)

def test_login_precedence():
    print("test_login_precedence")
    # An account login should win without consulting the legacy env credential.
    import core.accounts as acc
    import core.admin_login as legacy
    settings.DEMO_PG_IDS = ["pg_demo_1"]
    res, _ = signup("prec@brand.com", "password123", "Acme")

    legacy_called = {"n": 0}
    orig = legacy.verify_admin_login
    legacy.verify_admin_login = lambda u, p: (legacy_called.__setitem__("n", legacy_called["n"] + 1) or (None, "invalid"))
    try:
        # Simulate the endpoint's resolution order.
        key, reason = acc.verify_login("prec@brand.com", "password123")
        if reason != "ok":
            key, reason = legacy.verify_admin_login("prec@brand.com", "password123")
        check("account login wins", reason == "ok" and key == res["api_key"])
        check("legacy not consulted on account hit", legacy_called["n"] == 0)
    finally:
        legacy.verify_admin_login = orig

def test_signup_rate_limit():
    print("test_signup_rate_limit")
    settings.SIGNUP_MAX_PER_WINDOW = 3
    ip = "203.0.113.7"
    allowed = [check_signup_rate(ip) for _ in range(5)]
    check("first N within limit are allowed", allowed[:3] == [True, True, True])
    check("over-limit attempts are blocked", allowed[3:] == [False, False])
    check("a different IP is unaffected", check_signup_rate("198.51.100.9") is True)

if __name__ == "__main__":
    test_store()
    test_demo_brand()
    test_signup()
    test_login_and_verify()
    test_login_precedence()
    test_signup_rate_limit()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(0 if _failed == 0 else 1)
