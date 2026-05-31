"""
test_wave_a.py — Wave A "stop lying" product-quality regression test.

Proves the honesty + idempotency fixes that stop the bot from reporting
fabricated outcomes to users:

  1. user_error()        — user-facing error text never leaks internals
     (URLs, HTTP codes, tracebacks, raw exception text); the real exception is
     logged, not shown.
  2. IDEMPOTENT_TOOLS    — verify_payment is NOT burst-deduped (it takes no
     args → a constant key would replay a stale payment result); every genuine
     write-path tool still is.
  3. _call_search_api    — distinguishes a hard API failure (None) from a
     genuine empty result set ([]), and search_properties surfaces the truthful
     "trouble reaching listings" message on failure instead of the false
     "nothing available in this region".
  4. _create_external_lead — inspects the CRM response body; flips to failure
     only on an explicit failure marker, never on a clean 200.
  5. cancel_booking      — treats a clean 200 as success but respects an
     explicit failure body, leaks nothing on transport error, and FULLY clears
     the reserve_bed idempotency entry (lock AND cached result) so a
     reserve→cancel→reserve sequence runs fresh instead of replaying "reserved".

Deterministic: an in-memory fake replaces the Redis client. No network, no LLM.
Run: `python test_wave_a.py`.
"""

import asyncio
import json
import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

import db.redis._base as _base  # noqa: E402
import db.redis.idempotency as idem  # noqa: E402


class _FakeRedis:
    """Just enough of the redis client for the idempotency primitives."""
    def __init__(self):
        self.store = {}

    def set(self, key, val, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = val
        return True

    def setex(self, key, ttl, val):
        self.store[key] = val
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)


_fake = _FakeRedis()
_base._r = lambda: _fake
idem._r = lambda: _fake

from config import settings  # noqa: E402

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


def reset_redis():
    _fake.store.clear()


def arun(coro):
    return asyncio.run(coro)


class _CapturingLogger:
    def __init__(self):
        self.records = []

    def error(self, *args, **kwargs):
        self.records.append(args)


# --------------------------------------------------------------------------- #
# 1. user_error() never leaks internals
# --------------------------------------------------------------------------- #
print("\n[1] user_error — no internal leakage")
from utils.api import user_error  # noqa: E402

_secret = "https://apiv2.rentok.com/booking?token=SEKRET123 -> 500 Traceback (most recent call last)"
_log = _CapturingLogger()
msg = user_error("cancel your booking", RuntimeError(_secret), logger=_log)

check("1a friendly text present", "Sorry, I couldn't cancel your booking" in msg, msg)
for leak in ("https", "token", "SEKRET123", "500", "Traceback", "RuntimeError", "apiv2"):
    check(f"1b no leak of '{leak}'", leak not in msg, f"leaked in: {msg}")
check("1c real exception is logged", len(_log.records) == 1 and _secret in str(_log.records[0]))
# No logger / no exc → still safe, no crash, still friendly
msg2 = user_error("schedule your visit")
check("1d works without logger/exc", "Sorry, I couldn't schedule your visit" in msg2)


# --------------------------------------------------------------------------- #
# 2. IDEMPOTENT_TOOLS membership
# --------------------------------------------------------------------------- #
print("\n[2] IDEMPOTENT_TOOLS — verify_payment excluded, write tools included")
from core.tool_boundary import IDEMPOTENT_TOOLS, idempotency_key  # noqa: E402

check("2a verify_payment NOT deduped", "verify_payment" not in IDEMPOTENT_TOOLS)
for tool in ("reserve_bed", "cancel_booking", "save_visit_time", "save_call_time",
             "create_payment_link", "reschedule_booking"):
    check(f"2b {tool} IS deduped", tool in IDEMPOTENT_TOOLS)


# --------------------------------------------------------------------------- #
# 3. _call_search_api — None (failure) vs [] (genuine empty)
# --------------------------------------------------------------------------- #
print("\n[3] _call_search_api — failure-vs-empty + truthful messaging")
import tools.broker.search as search  # noqa: E402
import utils.retry as retry  # noqa: E402

# Bypass the Redis cache layer entirely for these unit checks.
search._get_search_cache = lambda payload: None
search._set_search_cache = lambda payload, results: None

# Empty pg_ids → hard failure (None), no network needed.
check("3a empty pg_ids → None", arun(search._call_search_api({"pg_ids": []})) is None)


def _patch_http_post(fn):
    retry.http_post = fn
    search.http_post = fn  # in case it was already bound at module import


async def _raise(*a, **k):
    raise RuntimeError("connection refused https://apiv2... 502")

_patch_http_post(_raise)
check("3b transport error → None", arun(search._call_search_api({"pg_ids": ["p1"]})) is None)


async def _inner_500(*a, **k):
    return {"data": {"status": 500, "message": "boom"}}

_patch_http_post(_inner_500)
check("3c inner 500 → None", arun(search._call_search_api({"pg_ids": ["p1"]})) is None)


async def _two_results(*a, **k):
    return {"data": {"data": {"results": [{"p_id": "1"}, {"p_id": "2"}]}}}

_patch_http_post(_two_results)
_res = arun(search._call_search_api({"pg_ids": ["p1"]}))
check("3d results → list of len 2", isinstance(_res, list) and len(_res) == 2)


async def _empty(*a, **k):
    return {"data": {"data": {"results": []}}}

_patch_http_post(_empty)
_res = arun(search._call_search_api({"pg_ids": ["p1"]}))
check("3e genuine empty → [] (not None)", _res == [])


# search_properties surfaces the truthful failure message on None, and the
# honest "no inventory" message only on a genuine empty.
async def _geocode_ok(_loc):
    return (19.07, 72.87)

search.get_preferences = lambda uid: {"location": "Kurla"}
search.geocode_address = _geocode_ok
search.get_whitelabel_pg_ids = lambda uid: ["p1"]
search.redis_save_preferences = lambda uid, prefs: None


async def _api_none(_payload):
    return None

search._call_search_api = _api_none
out = arun(search.search_properties("u1"))
check("3f failure → truthful 'trouble reaching' message",
      "trouble reaching our property listings" in out, out)
check("3g failure → does NOT claim no inventory",
      "No properties are currently available" not in out, out)


async def _api_empty(_payload):
    return []

search._call_search_api = _api_empty
out = arun(search.search_properties("u1"))
check("3h genuine empty → honest no-inventory message",
      "No properties are currently available in this region." in out, out)
check("3i genuine empty → not the failure message",
      "trouble reaching" not in out, out)


# --------------------------------------------------------------------------- #
# 4. _create_external_lead — body inspection (clean 200 ≠ proof of success)
# --------------------------------------------------------------------------- #
print("\n[4] _create_external_lead — explicit-failure-only")
import tools.booking.schedule_visit as sv  # noqa: E402
import db.redis_store as rs  # noqa: E402

# Stub all the pre-payload Redis reads so no real Redis is touched.
sv.get_user_phone = lambda uid: "9876543210"
sv.get_aadhar_user_name = lambda uid: "Test User"
sv.get_user_memory = lambda uid: {}
rs.get_preferences = lambda uid: {}
rs.get_aadhar_gender = lambda uid: "Any"


def _lead_call(response=None, exc=None):
    async def _fn(*a, **k):
        if exc:
            raise exc
        return response
    sv.http_post = _fn
    return arun(sv._create_external_lead("u1", "ez1", "pg1", "n1", "01/06/2026", "10:00 AM", "Physical visit"))


check("4a clean 200 (no flag) → True", _lead_call({}) is True)
check("4b clean 200 (success:true) → True", _lead_call({"success": True}) is True)
check("4c success:false → False", _lead_call({"success": False, "message": "rejected"}) is False)
check("4d status 500 → False", _lead_call({"status": 500}) is False)
check("4e status 'error' → False", _lead_call({"status": "error"}) is False)
check("4f transport error → False", _lead_call(exc=RuntimeError("boom")) is False)


# --------------------------------------------------------------------------- #
# 5. cancel_booking — honest success, idempotency fully cleared, no leak
# --------------------------------------------------------------------------- #
print("\n[5] cancel_booking — honesty + idem_clear + no leak")
import tools.booking.cancel as cancel  # noqa: E402
from db.redis.idempotency import _lock_key, _result_key  # noqa: E402

cancel._find_property = lambda uid, name: {"property_id": "pid1", "property_name": "Test PG"}


class _FakeResp:
    def __init__(self, json_data, status):
        self._j = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._j


class _FakeClient:
    def __init__(self, resp, exc):
        self._resp, self._exc = resp, exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        if self._exc:
            raise self._exc
        return self._resp


class _FakeHttpx:
    def __init__(self, resp=None, exc=None):
        self._resp, self._exc = resp, exc

    def AsyncClient(self, *a, **k):
        return _FakeClient(self._resp, self._exc)


def _cancel(json_data=None, status=200, exc=None):
    cancel.httpx = _FakeHttpx(_FakeResp(json_data, status) if exc is None else None, exc)
    return arun(cancel.cancel_booking(user_id="u1", property_name="Test PG"))


# Case A — clean 200 ⇒ success, and the stale reserve_bed idem entry is cleared.
reset_redis()
_idem_k = idempotency_key("u1", "reserve_bed", {"property_name": "Test PG"})
_fake.store[_lock_key(_idem_k)] = b"1"
_fake.store[_result_key(_idem_k)] = json.dumps("RESERVED: bed held")  # simulate a prior reservation
out = _cancel(json_data={})
check("5a clean 200 → success message", "cancelled successfully" in out, out)
check("5b idem lock cleared", _lock_key(_idem_k) not in _fake.store)
check("5c idem RESULT cleared (replay defeated)", _result_key(_idem_k) not in _fake.store,
      "stale reserved result would replay on re-reserve")

# Case B — explicit failure body ⇒ honest failure, not a false success.
out = _cancel(json_data={"success": False, "message": "no active booking"})
check("5d explicit failure → 'couldn't cancel'", "couldn't cancel" in out, out)
check("5e explicit failure → not 'successfully'", "successfully" not in out, out)

# Case C — transport error ⇒ friendly message, no leak.
out = _cancel(exc=RuntimeError("https://apiv2.rentok.com/x 500 Traceback SEKRET"))
check("5f transport error → friendly message", "Sorry, I couldn't cancel your booking" in out, out)
for leak in ("https", "500", "Traceback", "SEKRET", "apiv2"):
    check(f"5g no leak of '{leak}'", leak not in out, out)


# --------------------------------------------------------------------------- #
print(f"\n{'='*60}")
print(f"RESULTS: {_passed} passed, {_failed} failed")
print(f"{'='*60}")
sys.exit(1 if _failed else 0)
