"""
test_admin_login.py — admin-panel login gate regression test.

Proves the security properties of the ID + password gate that replaced the
old "anyone with the panel URL is in / brand key shipped in the bundle" model:

  1. Secure default     — with NO password configured, verify_admin_login
     returns "unconfigured" (endpoint → 503). The gate fails CLOSED; it never
     silently lets anyone in.
  2. Valid credentials  — correct username + password returns the brand key.
  3. Bad credentials    — wrong username OR wrong password returns "invalid"
     and records a brute-force failure.
  4. Digest path        — ADMIN_LOGIN_PASSWORD_SHA256 (preferred) validates
     without any plaintext password in env.
  5. Key fallback       — when ADMIN_LOGIN_API_KEY is unset, the returned key
     falls back to DEFAULT_BRAND_API_KEY.
  6. Throttle           — after MAX_FAILS failures the user is locked out even
     with correct credentials ("throttled" → 429) ...
  7. Throttle fail-open — ... but a Redis hiccup never locks out a real admin.
  8. Misconfigured      — if the returned key resolves to no brand, login is
     refused ("misconfigured" → 503) instead of handing back a dead key.
  9. Clear on success   — a successful login resets the failure counter.
 10. Endpoint mapping   — POST /admin/login maps each reason to the right HTTP
     status (200 / 400 / 401 / 429 / 503).

Deterministic: an in-memory fake replaces Redis and the brand-config lookup.
No network, no LLM, no real Redis. Run: `python test_admin_login.py`.
"""

import asyncio
import hashlib
import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

import db.redis._base as _base  # noqa: E402
import db.redis.brand as brand  # noqa: E402


class _FakeRedis:
    """Just enough of the redis client for the throttle counter."""
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    def expire(self, key, ttl):
        return True

    def delete(self, key):
        self.store.pop(key, None)


_fake = _FakeRedis()
_base._r = lambda: _fake

# Brand-config lookup is what makes a returned key "real". Only OxOtel1234 resolves.
_valid_brand_keys = {"OxOtel1234"}
brand.get_brand_config = lambda k: {"brand_hash": "h"} if k in _valid_brand_keys else None

from config import settings  # noqa: E402
from core.admin_login import verify_admin_login  # noqa: E402

_PW = "s3cret-pw"
_PW_SHA = hashlib.sha256(_PW.encode()).hexdigest()


def configure(*, password=_PW, digest="", api_key="OxOtel1234", username="oxotel",
              max_fails=3, valid_keys=("OxOtel1234",)):
    """Reset settings + fake Redis to a known baseline for one scenario."""
    settings.ADMIN_LOGIN_USERNAME = username
    settings.ADMIN_LOGIN_PASSWORD = password
    settings.ADMIN_LOGIN_PASSWORD_SHA256 = digest
    settings.ADMIN_LOGIN_API_KEY = api_key
    settings.ADMIN_LOGIN_MAX_FAILS = max_fails
    settings.ADMIN_LOGIN_THROTTLE_SECONDS = 900
    _fake.store.clear()
    _valid_brand_keys.clear()
    _valid_brand_keys.update(valid_keys)
    _base._r = lambda: _fake  # restore (the fail-open test swaps it out)


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


print("Admin login gate\n")

# ── 1. Secure default: no password configured → unconfigured ─────────────────
configure(password="", digest="")
key, reason = verify_admin_login("oxotel", "anything")
check("no password configured → unconfigured (fails closed)", reason == "unconfigured" and key is None,
      f"got ({key!r}, {reason!r})")

# ── 2. Valid credentials → ok, returns brand key ─────────────────────────────
configure()
key, reason = verify_admin_login("oxotel", _PW)
check("valid credentials → ok", reason == "ok" and key == "OxOtel1234", f"got ({key!r}, {reason!r})")

# ── 3a. Wrong password → invalid + records a failure ─────────────────────────
configure()
key, reason = verify_admin_login("oxotel", "wrong")
check("wrong password → invalid", reason == "invalid" and key is None, f"got ({key!r}, {reason!r})")
check("wrong password recorded a brute-force failure",
      int(_fake.store.get("admin_login_fail:oxotel", 0)) == 1)

# ── 3b. Wrong username → invalid ─────────────────────────────────────────────
configure()
key, reason = verify_admin_login("intruder", _PW)
check("wrong username → invalid", reason == "invalid" and key is None, f"got ({key!r}, {reason!r})")

# ── 4. Digest path: only SHA256 set, no plaintext password in env ────────────
configure(password="", digest=_PW_SHA)
key, reason = verify_admin_login("oxotel", _PW)
check("sha256 digest path validates without plaintext", reason == "ok" and key == "OxOtel1234",
      f"got ({key!r}, {reason!r})")

# ── 5. Key fallback: ADMIN_LOGIN_API_KEY unset → DEFAULT_BRAND_API_KEY ────────
configure(api_key="", valid_keys=(settings.DEFAULT_BRAND_API_KEY,))
key, reason = verify_admin_login("oxotel", _PW)
check("empty ADMIN_LOGIN_API_KEY falls back to DEFAULT_BRAND_API_KEY",
      reason == "ok" and key == settings.DEFAULT_BRAND_API_KEY, f"got ({key!r}, {reason!r})")

# ── 6. Throttle: MAX_FAILS failures → locked out even with correct creds ──────
configure(max_fails=3)
for _ in range(3):
    verify_admin_login("oxotel", "wrong")
key, reason = verify_admin_login("oxotel", _PW)  # correct, but should be blocked
check("locked out after MAX_FAILS failures", reason == "throttled" and key is None,
      f"got ({key!r}, {reason!r})")

# ── 7. Throttle fail-open: Redis error must NOT lock out a real admin ─────────
configure(max_fails=1)
verify_admin_login("oxotel", "wrong")  # would normally trip the throttle


def _boom():
    raise RuntimeError("redis down")


_base._r = _boom  # every throttle read/write now raises
key, reason = verify_admin_login("oxotel", _PW)
check("Redis hiccup fails open (valid login still succeeds)", reason == "ok" and key == "OxOtel1234",
      f"got ({key!r}, {reason!r})")
_base._r = lambda: _fake  # restore

# ── 8. Misconfigured: returned key resolves to no brand → refused ────────────
configure(api_key="GhostKey9999", valid_keys=("OxOtel1234",))  # GhostKey9999 not valid
key, reason = verify_admin_login("oxotel", _PW)
check("dead brand key → misconfigured (not handed back)", reason == "misconfigured" and key is None,
      f"got ({key!r}, {reason!r})")

# ── 9. Clear on success: a good login resets the failure counter ─────────────
configure(max_fails=5)
verify_admin_login("oxotel", "wrong")
verify_admin_login("oxotel", "wrong")
check("failures accumulated before success", int(_fake.store.get("admin_login_fail:oxotel", 0)) == 2)
verify_admin_login("oxotel", _PW)  # success
check("successful login cleared the failure counter",
      _fake.store.get("admin_login_fail:oxotel") is None)

# ── 10. Endpoint reason → HTTP status mapping ────────────────────────────────
from fastapi import HTTPException  # noqa: E402
from routers.admin import admin_login  # noqa: E402


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _call(body):
    """Run the endpoint; return (status_or_200, payload_or_None)."""
    try:
        result = asyncio.run(admin_login(_FakeRequest(body)))
        return 200, result
    except HTTPException as e:
        return e.status_code, None


configure()
status, payload = _call({"username": "oxotel", "password": _PW})
check("endpoint 200 returns api_key", status == 200 and payload == {"api_key": "OxOtel1234"},
      f"got {status} {payload!r}")

status, _ = _call({"username": "", "password": ""})
check("endpoint 400 on missing fields", status == 400, f"got {status}")

status, _ = _call({"username": "oxotel", "password": "wrong"})
check("endpoint 401 on invalid credentials", status == 401, f"got {status}")

configure(password="", digest="")
status, _ = _call({"username": "oxotel", "password": "x"})
check("endpoint 503 when unconfigured", status == 503, f"got {status}")

configure(max_fails=1)
_call({"username": "oxotel", "password": "wrong"})  # trip throttle
status, _ = _call({"username": "oxotel", "password": _PW})
check("endpoint 429 when throttled", status == 429, f"got {status}")

configure(api_key="GhostKey9999", valid_keys=("OxOtel1234",))
status, _ = _call({"username": "oxotel", "password": _PW})
check("endpoint 503 when misconfigured", status == 503, f"got {status}")

# ── Summary ──────────────────────────────────────────────────────────────────
print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
