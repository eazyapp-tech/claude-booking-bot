"""
test_contract_alignment.py — RentOk API contract-alignment regression test.

Locks in three fixes that were source-verified against the live rentok-backend
(route -> controller -> service) on 2026-05-31. Each section pins the bot to the
backend's ACTUAL contract so a future edit can't silently drift back to a shape
the API doesn't honour:

  1. get-room-details   — rooms come from POST /bookingBot/get-room-details with
     body {"eazypg_id": ...}; the room list is nested at data["data"]["rooms"];
     each room exposes name / sharing_type / rent and NO live bed count. The old
     GET /bookingBot/getAvailableRoomFromEazyPGID route does not exist (real 404).
     Backend truth: bookingBot.ts:969 getAvailableRoom.
  2. lead_source        — leads MUST be stamped "bookingBot00". The payment-link
     flow's tenant lookup (GET /tenant/get-tenant_uuid) resolves a tenant ONLY
     where lead_source="bookingBot00" AND status=3, so any other source string is
     invisible to the bot. Backend truth: tenant.ts:29705 getTenantUUIDForBookingBot.
  3. search payload     — pg_available_for (exact IN-match) and unit_types_available
     (array-overlap) filter on stored per-property enums; client-guessed values
     silently match nothing, so the bot must NOT send them and instead rank
     post-search. Backend truth: PropertyService.ts:451-476.

Deterministic: an in-memory fake replaces Redis; httpx / http_post are stubbed.
No network, no LLM. Run: `python test_contract_alignment.py`.
"""

import asyncio
import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

import db.redis._base as _base  # noqa: E402


class _FakeRedis:
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
# 1. get-room-details contract
# --------------------------------------------------------------------------- #
print("\n[1] room_details — POST get-room-details, data.data.rooms, no bed count")
import tools.broker.room_details as rd  # noqa: E402

check("1a URL is POST get-room-details",
      rd._ROOM_DETAILS_URL.endswith("/bookingBot/get-room-details"), rd._ROOM_DETAILS_URL)

# _extract_rooms pulls data["data"]["rooms"] and degrades safely.
check("1b extract nested rooms",
      rd._extract_rooms({"status": 200, "data": {"rooms": [{"id": 1}], "pg_name": "X"}}) == [{"id": 1}])
check("1c unknown id (data:{}) -> []", rd._extract_rooms({"status": 404, "data": {}}) == [])
check("1d data missing -> []", rd._extract_rooms({"status": 200}) == [])
check("1e data not a dict -> []", rd._extract_rooms({"data": []}) == [])

# Tool schema must not promise live bed availability.
_desc = rd.TOOL_SCHEMA["description"].lower()
check("1f schema drops 'beds_available'", "beds_available" not in _desc, _desc)
check("1g schema drops 'real-time' bed claim", "real-time" not in _desc, _desc)
check("1h schema says not live per-bed", "not live per-bed availability" in _desc, _desc)


# fetch_room_details must POST {"eazypg_id": ...} (never GET) and render the
# real fields (name / sharing_type / rent) without inventing a bed count.
class _FakeResp:
    def __init__(self, json_data, status=200):
        self._j, self.status_code = json_data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._j


class _FakeClient:
    def __init__(self, sink, resp):
        self._sink, self._resp = sink, resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        self._sink["url"] = url
        self._sink["json"] = json
        return self._resp

    async def get(self, *a, **k):
        self._sink["used_get"] = True
        return self._resp


class _FakeHttpx:
    def __init__(self, sink, resp):
        self._sink, self._resp = sink, resp

    def AsyncClient(self, *a, **k):
        return _FakeClient(self._sink, self._resp)


_sink = {}
rd.find_property = lambda uid, name: {"eazypg_id": "EZ123", "property_name": "Test PG"}
_room_body = {
    "status": 200,
    "message": "Success",
    "data": {
        "rooms": [
            {"id": 1, "name": "Room A", "sharing_type": "Double", "rent": 9500},
            {"id": 2, "name": "Room B", "sharing_type": "Single", "rent": 14000},
        ],
        "pg_name": "Test PG",
    },
}
rd.httpx = _FakeHttpx(_sink, _FakeResp(_room_body))
_out = arun(rd.fetch_room_details("u1", "Test PG"))

check("1i posts to get-room-details URL",
      _sink.get("url", "").endswith("/bookingBot/get-room-details"), _sink.get("url"))
check("1j sends json body {eazypg_id}", _sink.get("json") == {"eazypg_id": "EZ123"}, _sink.get("json"))
check("1k never uses GET", "used_get" not in _sink)
check("1l renders room name + rent", "Room A" in _out and "9500" in _out, _out)
check("1m renders sharing type", "Double sharing" in _out, _out)
check("1n no fabricated bed count", "beds available" not in _out.lower(), _out)
check("1o steers to visit for live vacancy", "schedule a visit" in _out.lower(), _out)

# Empty rooms (property-not-found / no live rooms) must NOT crash and must NOT
# claim rooms exist — falls back to a truthful "not showing right now" message.
_sink2 = {}
rd.httpx = _FakeHttpx(_sink2, _FakeResp({"status": 404, "message": "Property not found", "data": {}}))
rd.find_property = lambda uid, name: {
    "eazypg_id": "EZ123", "property_name": "Test PG",
    "sharing_types": [], "amenities": "", "property_rent": "",
}
_out2 = arun(rd.fetch_room_details("u1", "Test PG"))
check("1p empty rooms -> graceful, no crash", isinstance(_out2, str) and len(_out2) > 0)
check("1q empty rooms -> no invented rooms listing", "Rooms at" not in _out2, _out2)


# --------------------------------------------------------------------------- #
# 2. lead_source == "bookingBot00" (visit + payment lead-create paths)
# --------------------------------------------------------------------------- #
print("\n[2] lead_source — canonical 'bookingBot00' on every CRM lead")
import tools.booking.schedule_visit as sv  # noqa: E402
import db.redis_store as rs  # noqa: E402

sv.get_user_phone = lambda uid: "9876543210"
sv.get_aadhar_user_name = lambda uid: "Test User"
sv.get_user_memory = lambda uid: {}
rs.get_preferences = lambda uid: {}
rs.get_aadhar_gender = lambda uid: "Any"

_captured = {}


async def _capture_post(url, json=None):
    _captured["url"] = url
    _captured["json"] = json
    return {}  # clean 200, no failure marker -> success


sv.http_post = _capture_post
_ok = arun(sv._create_external_lead("u1", "ez1", "pg1", "n1", "01/06/2026", "10:00 AM", "Physical visit"))
check("2a visit lead succeeds", _ok is True)
check("2b visit lead_source is bookingBot00",
      _captured.get("json", {}).get("lead_source") == "bookingBot00", _captured.get("json"))
check("2c visit lead hits addLeadFromEazyPGID",
      _captured.get("url", "").endswith("/tenant/addLeadFromEazyPGID"), _captured.get("url"))

# Payment-flow lead-stamp is inside verify_payment; assert at source level that
# its lead is the canonical source and the old display string is fully gone.
with open(os.path.join(os.path.dirname(__file__), "tools", "booking", "payment.py")) as f:
    _pay_src = f.read()
check("2d payment lead_source is bookingBot00", '"lead_source": "bookingBot00"' in _pay_src)
check("2e payment drops old 'Booking Bot' source", '"lead_source": "Booking Bot"' not in _pay_src)
with open(os.path.join(os.path.dirname(__file__), "tools", "booking", "schedule_visit.py")) as f:
    _sv_src = f.read()
check("2f visit drops old 'Booking Bot' source", '"lead_source": "Booking Bot"' not in _sv_src)


# --------------------------------------------------------------------------- #
# 3. search payload omits pg_available_for / unit_types_available
# --------------------------------------------------------------------------- #
print("\n[3] search — client-guessed enum filters NOT sent to the API")
import tools.broker.search as search  # noqa: E402

search._get_search_cache = lambda payload: None
search._set_search_cache = lambda payload, results: None
search.get_preferences = lambda uid: {"location": "Kurla"}
search.get_whitelabel_pg_ids = lambda uid: ["p1"]
search.redis_save_preferences = lambda uid, prefs: None


async def _geocode_ok(_loc):
    return (19.07, 72.87)


search.geocode_address = _geocode_ok

_payloads = []


async def _capture_search(payload):
    _payloads.append(payload)
    return []  # genuine empty -> honest no-inventory message, exercises relaxation


search._call_search_api = _capture_search
_ = arun(search.search_properties("u1"))

check("3a at least one search payload built", len(_payloads) >= 1)
check("3b pg_ids IS sent (hard backend requirement)",
      all("pg_ids" in p for p in _payloads), _payloads)
for i, p in enumerate(_payloads):
    check(f"3c[{i}] pg_available_for NOT sent", "pg_available_for" not in p, p)
    check(f"3d[{i}] unit_types_available NOT sent", "unit_types_available" not in p, p)


# --------------------------------------------------------------------------- #
print(f"\n{'='*60}")
print(f"RESULTS: {_passed} passed, {_failed} failed")
print(f"{'='*60}")
sys.exit(1 if _failed else 0)
