"""
test_shortlist_contract.py — Shortlist contract regression (S17 drop-off fix).

Locks the two independent, each-fatal production bugs that made EVERY shortlist
attempt fail for anonymous web users (root-caused live against the Rentok API):

  1. shortlist.py success check — POST /bookingBot/shortlist-booking-bot-property
     returns its outcome via an INNER `status` field (HTTP is always 200) and
     carries NO top-level `success` key. The old `if not data.get("success")`
     therefore reported failure on a genuine 200 success, so the bot narrated a
     "hiccup with the shortlist system" even though the property WAS shortlisted.
     Fix: accept either convention — `success is True` OR `status in (200,"200")`.

  2. search.py property-contact caching — the shortlist API rejects an empty
     `property_contact` with `{"status":400, "...required"}`. search.py cached the
     contact from `p_phone_number`, a key that does not exist on the property
     object; the real contact is `p_personal_contact`. So the cached contact was
     always "" → every shortlist call hit the 400 path. Fix: prefer
     `p_personal_contact`, falling back to `p_phone_number`.

Deterministic: in-memory fakes replace Redis + httpx. No network, no LLM.
Run: `python test_shortlist_contract.py` (exit 0 = pass).
"""

import asyncio
import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

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


def arun(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Shared httpx fake — captures the outgoing request body so we can assert what
# property_contact actually reaches the shortlist API.
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, json_data, status=200):
        self._j = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._j


class _FakeClient:
    def __init__(self, resp, exc, sink):
        self._resp, self._exc, self._sink = resp, exc, sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        self._sink["url"] = url
        self._sink["body"] = json
        if self._exc:
            raise self._exc
        return self._resp


class _FakeHttpx:
    def __init__(self, resp=None, exc=None, sink=None):
        self._resp, self._exc = resp, exc
        self.sink = sink if sink is not None else {}

    def AsyncClient(self, *a, **k):
        return _FakeClient(self._resp, self._exc, self.sink)


# --------------------------------------------------------------------------- #
# 1. shortlist.py — success signalled by inner status==200, NO `success` key
# --------------------------------------------------------------------------- #
print("\n[1] shortlist_property — accept inner status:200 (no `success` key)")
import tools.broker.shortlist as shortlist  # noqa: E402

# Stub every Redis / side-effect touchpoint so no real Redis is hit. The cached
# property carries a NON-EMPTY phone_number (the Bug-1 fix guarantees this).
shortlist.find_property = lambda uid, name: {
    "prop_id": "6087ef52-uuid",
    "pg_id": "UaDCGP3firebase",
    "property_name": "OXO ZEPHYR RABALE",
    "phone_number": "7977106781",
}
shortlist.get_user_phone = lambda uid: "9876543210"
shortlist.get_user_brand = lambda uid: "brandhash00"
shortlist.track_funnel = lambda *a, **k: None
shortlist.record_property_shortlisted = lambda *a, **k: None
shortlist.track_property_event = lambda *a, **k: None
shortlist.schedule_followup = lambda *a, **k: None


def _shortlist(json_data=None, exc=None, status=200):
    sink = {}
    shortlist.httpx = _FakeHttpx(
        _FakeResp(json_data, status) if exc is None else None, exc, sink
    )
    out = arun(shortlist.shortlist_property("u1", "OXO ZEPHYR RABALE"))
    return out, sink


# 1a — THE regression: real success envelope has status:200 and no `success` key.
out, sink = _shortlist({"status": 200, "message": "Property added to shortlist successfully", "data": {}})
check("1a inner status:200 (no success key) → success", "shortlisted successfully" in out, out)
check("1b success path does NOT report failure", "Could not shortlist" not in out, out)

# 1c — string "200" tolerated defensively.
out, _ = _shortlist({"status": "200", "message": "ok"})
check("1c inner status:'200' (string) → success", "shortlisted successfully" in out, out)

# 1d — legacy `{"success": true}` convention still treated as success.
out, _ = _shortlist({"success": True})
check("1d legacy success:true → success", "shortlisted successfully" in out, out)

# 1e/1f — genuine validation failure must be reported honestly, not as success.
out, _ = _shortlist({"status": 400, "message": "property_contact is required"})
check("1e status:400 → honest failure", "Could not shortlist" in out, out)
check("1f status:400 → not falsely 'successfully'", "shortlisted successfully" not in out, out)

# 1g — explicit success:false also honest failure.
out, _ = _shortlist({"success": False, "message": "rejected"})
check("1g success:false → honest failure", "Could not shortlist" in out, out)

# 1h — transport error → non-success message (no crash).
out, _ = _shortlist(exc=RuntimeError("connection refused"))
check("1h transport error → not a success", "shortlisted successfully" not in out, out)

# 1i — the cached non-empty contact is actually forwarded to the API.
out, sink = _shortlist({"status": 200, "message": "ok"})
check("1i property_contact forwarded from cache", sink.get("body", {}).get("property_contact") == "7977106781",
      f"body={sink.get('body')}")
check("1j property_id forwarded (prop_id preferred)", sink.get("body", {}).get("property_id") == "6087ef52-uuid",
      f"body={sink.get('body')}")

# 1k — empty cached contact still cannot succeed silently: the API would 400,
# and the tool must report that honestly (mirrors the pre-fix prod failure mode).
shortlist.find_property = lambda uid, name: {
    "prop_id": "pid", "pg_id": "pg", "property_name": "X", "phone_number": "",
}
out, sink = _shortlist({"status": 400, "message": "property_contact is required"})
check("1k empty contact + 400 → honest failure (not faked success)",
      "Could not shortlist" in out and "shortlisted successfully" not in out, out)


# --------------------------------------------------------------------------- #
# 2. search.py — caches property_contact from p_personal_contact, not the
#    (usually absent) p_phone_number.
# --------------------------------------------------------------------------- #
print("\n[2] search caches phone_number from p_personal_contact")
import tools.broker.search as search  # noqa: E402

_captured = {}


async def _noop_async(*a, **k):
    return None


# Stub every Redis / network / scoring side-effect; capture set_property_info_map.
search.get_preferences = lambda uid: {"location": "Navi Mumbai", "max_budget": 10000}
search.redis_save_preferences = lambda uid, prefs: None
search.get_whitelabel_pg_ids = lambda uid: ["pg1"]
search._enrich_with_images = _noop_async
search._geocode_properties = _noop_async
search.get_user_memory = lambda uid: {}
search.calc_match_score = lambda *a, **k: 80
search.get_property_info_map = lambda uid: []
search.set_property_info_map = lambda uid, m: _captured.update(map=m)
search.set_property_id_for_search = lambda uid, ids: None
search.set_last_search_results = lambda uid, r: None
search.save_property_template = lambda uid, t: None
search.track_funnel = lambda *a, **k: None
search.get_user_brand = lambda uid: "brandhash00"
search.record_property_viewed = lambda *a, **k: None
search.track_property_event = lambda *a, **k: None
search.update_user_memory = lambda *a, **k: None


async def _geocode_ok(_loc):
    return (19.137, 73.000)

search.geocode_address = _geocode_ok


async def _api_one(_payload):
    # A realistic OxOtel property: contact lives in p_personal_contact, and
    # p_phone_number is ABSENT (the exact prod shape that caused the empty cache).
    return [{
        "p_id": "6087ef52-uuid",
        "p_pg_id": "UaDCGP3firebase",
        "p_pg_name": "OXO ZEPHYR RABALE",
        "p_rent_starts_from": 7500,
        "p_personal_contact": "7977106781",
        "p_common_amenities": "WiFi, Meals",
        "p_pg_available_for": "Any",
    }]

search._call_search_api = _api_one

out = arun(search.search_properties("u1"))
_map = _captured.get("map", [])
check("2a set_property_info_map captured one entry", len(_map) == 1, f"map={_map}")
_entry = _map[0] if _map else {}
check("2b cached phone_number == p_personal_contact (non-empty)",
      _entry.get("phone_number") == "7977106781", f"entry={_entry}")
check("2c the shortlist contract is satisfied (contact is non-empty)",
      bool(_entry.get("phone_number")), f"entry={_entry}")


# A property with NEITHER contact field → empty string, no crash (graceful).
_captured.clear()


async def _api_no_contact(_payload):
    return [{
        "p_id": "p2", "p_pg_id": "pg2", "p_pg_name": "NO CONTACT PG",
        "p_rent_starts_from": 9000, "p_common_amenities": "", "p_pg_available_for": "Any",
    }]

search._call_search_api = _api_no_contact
arun(search.search_properties("u1"))
_entry2 = (_captured.get("map") or [{}])[0]
check("2d missing both contact fields → '' (no crash)", _entry2.get("phone_number") == "",
      f"entry={_entry2}")

# Legacy p_phone_number still honoured when p_personal_contact absent (fallback).
_captured.clear()


async def _api_legacy(_payload):
    return [{
        "p_id": "p3", "p_pg_id": "pg3", "p_pg_name": "LEGACY PG",
        "p_rent_starts_from": 9000, "p_phone_number": "9000000000",
        "p_common_amenities": "", "p_pg_available_for": "Any",
    }]

search._call_search_api = _api_legacy
arun(search.search_properties("u1"))
_entry3 = (_captured.get("map") or [{}])[0]
check("2e p_phone_number fallback honoured when p_personal_contact absent",
      _entry3.get("phone_number") == "9000000000", f"entry={_entry3}")


# --------------------------------------------------------------------------- #
print(f"\n{'=' * 60}")
print(f"  {_passed} passed, {_failed} failed")
print(f"{'=' * 60}")
sys.exit(1 if _failed else 0)
