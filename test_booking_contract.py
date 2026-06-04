"""
test_booking_contract.py — Booking success-key contract regression (UAT P0).

Root-caused live against 45 UAT conversations + the verified Rentok contract:
EVERY visit/call booking reported "Booking failed / technical issue" to users
while the booking actually persisted at Rentok (→ "already scheduled" on retry),
and the funnel recorded ZERO visits despite save_visit_time running cleanly.

Cause: POST /bookingBot/add-booking signals success via inner `status:200`
(VERIFIED_RENTOK_CONTRACT.md:1268 — `{status:200, message:"User data saved
successfully", data:<row>}`), carrying NO top-level `success` key. The old
`if not data.get("success")` therefore failed on every genuine 200 success, and
returned BEFORE track_funnel("visit") fired. Inner `status:400` is the dedup
("already exists"), inner `status:500` is a real error.

Fix (mirrors the shipped shortlist S17 fix): accept either convention —
`success is True` OR inner `status in (200,"200")`; treat inner `status:400` as
dedup. Applied to save_visit_time, save_call_time, reserve_bed.

Deterministic: in-memory stubs replace Redis + httpx. No network, no LLM.
Run: `python test_booking_contract.py` (exit 0 = pass).
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


class _FakeResp:
    def __init__(self, json_data, status=200):
        self._j = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._j


# --------------------------------------------------------------------------- #
# save_visit_time / save_call_time — stub every Redis + follow-up side effect
# and the add-booking HTTP call. eazypg_id="" so the CRM-lead path is skipped
# (its contract is covered by test_wave_a.py), isolating the success-key branch.
# --------------------------------------------------------------------------- #
import tools.booking.schedule_visit as sv  # noqa: E402
import tools.booking.schedule_call as sc  # noqa: E402

_funnel_events = []


def _wire(mod):
    mod.get_user_phone = lambda uid: "9876543210"
    mod._find_property = lambda uid, name: {
        "property_id": "pid1",
        "property_name": name,
        "eazypg_id": "",          # skip CRM lead path
        "property_lat": "", "property_long": "",
    }
    mod.transcribe_date = lambda d: "10/06/2026"
    mod.get_user_brand = lambda uid: "brandhash"
    mod.track_funnel = lambda uid, ev, **k: _funnel_events.append(ev)


_wire(sv)
_wire(sc)
# save_visit_time-only side effects
sv.record_visit_scheduled = lambda *a, **k: None
sv.track_property_event = lambda *a, **k: None
sv.schedule_followup = lambda *a, **k: None
sv.create_followup_state = lambda *a, **k: None
sv.record_signal = lambda *a, **k: None


def _set_resp(mod, json_data, status=200):
    async def _fn(url, json=None, raw=False, **k):
        return _FakeResp(json_data, status)
    mod.http_post = _fn


def _visit(json_data, status=200):
    _funnel_events.clear()
    _set_resp(sv, json_data, status)
    return arun(sv.save_visit_time("u1", "Test PG", "10 June", "4:00 PM"))


def _call(json_data, status=200):
    _set_resp(sc, json_data, status)
    return arun(sc.save_call_time("u1", "Test PG", "10 June", "12:00 PM", "Phone Call"))


print("[1] save_visit_time — inner-status success contract")
r = _visit({"status": 200, "message": "User data saved successfully"})
check("1a status:200 → scheduled (THE UAT REGRESSION)", r.startswith("Visit scheduled successfully"), repr(r))
check("1b status:200 → funnel 'visit' fired", "visit" in _funnel_events, _funnel_events)
check("1c legacy success:true still works", _visit({"success": True}).startswith("Visit scheduled successfully"))
check("1d status:400 → honest 'already scheduled'", "already" in _visit({"status": 400}).lower())
check("1e status:400 → funnel NOT fired", "visit" not in _funnel_events)
r5 = _visit({"status": 500, "message": "server error"})
check("1f status:500 → honest failure", r5.startswith("Booking failed"), repr(r5))
check("1g status:500 → funnel NOT fired", "visit" not in _funnel_events)

print("\n[2] save_call_time — inner-status success contract")
rc = _call({"status": 200, "message": "User data saved successfully"})
check("2a status:200 → call scheduled", rc.startswith("Phone Call scheduled successfully"), repr(rc))
check("2b legacy success:true works", _call({"success": True}).startswith("Phone Call scheduled successfully"))
check("2c status:400 → already booked", "already" in _call({"status": 400}).lower())
check("2d status:500 → honest failure", _call({"status": 500}).startswith("Booking failed"))

# --------------------------------------------------------------------------- #
# reserve_bed — POST /bookingBot/reserveProperty, same /bookingBot/ family
# convention. Defensive widening: accept success OR inner status 200.
# --------------------------------------------------------------------------- #
print("\n[3] reserve_bed — inner-status success contract")
import tools.booking.reserve as reserve  # noqa: E402

reserve._find_property = lambda uid, name: {"property_id": "pid1", "property_name": name}
reserve.get_user_brand = lambda uid: "brandhash"
reserve.track_funnel = lambda *a, **k: None
reserve.track_property_event = lambda *a, **k: None
reserve.record_signal = lambda *a, **k: None


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        return self._resp


class _FakeHttpx:
    def __init__(self, resp):
        self._resp = resp

    def AsyncClient(self, *a, **k):
        return _FakeClient(self._resp)


def _reserve(json_data):
    reserve.httpx = _FakeHttpx(_FakeResp(json_data))
    return arun(reserve.reserve_bed("u1", "Test PG"))


check("3a status:200 → reserved (latent bug)", _reserve({"status": 200}).startswith("Bed reserved successfully"), repr(_reserve({"status": 200})))
check("3b legacy success:true works", _reserve({"success": True}).startswith("Bed reserved successfully"))
check("3c status:400 → honest failure", _reserve({"status": 400, "message": "no beds"}).startswith("Failed to reserve"))


print(f"\n{'='*50}\n  {_passed} passed, {_failed} failed\n{'='*50}")
sys.exit(0 if _failed == 0 else 1)
