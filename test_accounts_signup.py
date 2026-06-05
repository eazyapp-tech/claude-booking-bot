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

if __name__ == "__main__":
    test_store()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(0 if _failed == 0 else 1)
