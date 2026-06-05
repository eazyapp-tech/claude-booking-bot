"""B1 warm-lead handoff regression — the lead the property manager receives must
carry the *why*, not just name+date.

Backend-verified contract (rentok-backend):
  - POST /tenant/addLeadFromEazyPGID accepts `remarks` → persisted to
    Tenant.lead_remarks (entities/tenant.ts:268), surfaced to managers as the
    `comments` field in getAllLeads and as `Notes` in reports.
  - It also accepts `room_type` → Tenant.room_type.
  - Zod schema is non-strict: unknown fields pass through untouched.

These assertions are deterministic — no Redis, no network, no LLM. They drive the
REAL _build_lead_remarks() pure function and the REAL _create_external_lead()
payload assembly (http_post + Redis reads stubbed) to prove the bot actually
sends manager-visible context.

Run: `python test_lead_enrichment.py` (exit 0 = pass).
"""
import asyncio
import sys

import db.redis_store as store
from tools.booking import schedule_visit
from tools.booking.schedule_visit import _build_lead_remarks, _create_external_lead

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


# ── 1. _build_lead_remarks: pure distillation of captured intent ──────────────
def test_remarks_full_context():
    prefs = {
        "location": "Kurla West",
        "min_budget": 8000,
        "max_budget": 12000,
        "property_type": "PG",
        "unit_types_available": "Double sharing",
        "must_have_amenities": "AC, WiFi",
        "move_in_date": "15/06/2026",
    }
    mem = {"deal_breakers": ["no curfew", "not ground floor"]}
    r = _build_lead_remarks(prefs, mem)
    check("remarks: budget range present", "8000" in r and "12000" in r)
    check("remarks: area present", "Kurla West" in r)
    check("remarks: move-in present", "15/06/2026" in r)
    check("remarks: must-have present", "AC, WiFi" in r)
    check("remarks: sharing present", "Double sharing" in r)
    check("remarks: deal-breakers present", "no curfew" in r and "not ground floor" in r)
    check("remarks: source attribution present", "EazyPG" in r)
    check("remarks: single compact line (no newlines)", "\n" not in r)


def test_remarks_sparse_is_safe():
    # A near-empty profile must not crash and must not emit junk like "Budget ₹–"
    r = _build_lead_remarks({}, {})
    check("remarks sparse: returns a string", isinstance(r, str))
    check("remarks sparse: no dangling budget", "₹–" not in r and "₹-" not in r)
    check("remarks sparse: still attributes source", "EazyPG" in r)


def test_remarks_budget_variants():
    only_max = _build_lead_remarks({"max_budget": 10000}, {})
    check("remarks: max-only budget reads 'up to'", "up to" in only_max.lower() and "10000" in only_max)
    only_min = _build_lead_remarks({"min_budget": 7000}, {})
    check("remarks: min-only budget reads 'from'", "from" in only_min.lower() and "7000" in only_min)


# ── 2. _create_external_lead: payload actually carries remarks + room_type ────
def test_lead_payload_includes_context():
    captured = {}

    async def fake_http_post(url, json=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        return {}  # clean success (no failure marker)

    prefs = {
        "location": "Andheri",
        "min_budget": 9000,
        "max_budget": 15000,
        "unit_types_available": "Single",
        "must_have_amenities": "Attached bathroom",
    }

    orig = {
        "http_post": schedule_visit.http_post,
        "get_user_phone": schedule_visit.get_user_phone,
        "get_aadhar_user_name": schedule_visit.get_aadhar_user_name,
        "get_user_memory": schedule_visit.get_user_memory,
        "s_get_preferences": getattr(store, "get_preferences", None),
        "s_get_aadhar_gender": getattr(store, "get_aadhar_gender", None),
    }
    try:
        schedule_visit.http_post = fake_http_post
        schedule_visit.get_user_phone = lambda uid: "9876543210"
        schedule_visit.get_aadhar_user_name = lambda uid: "Asha"
        schedule_visit.get_user_memory = lambda uid: {"deal_breakers": ["no PG far from metro"]}
        store.get_preferences = lambda uid: prefs
        store.get_aadhar_gender = lambda uid: "Female"

        ok = asyncio.run(
            _create_external_lead("u1", "eazy123", "pg1", "pgn1", "16/06/2026", "11:00 AM", "Physical visit")
        )
        body = captured.get("json", {})
        check("lead: call succeeded", ok is True)
        check("lead: hits addLeadFromEazyPGID", "addLeadFromEazyPGID" in captured.get("url", ""))
        check("lead: lead_source canonical preserved", body.get("lead_source") == "bookingBot00")
        check("lead: remarks field present + non-empty", bool(body.get("remarks")))
        check("lead: remarks carries must-have", "Attached bathroom" in body.get("remarks", ""))
        check("lead: remarks carries deal-breaker", "no PG far from metro" in body.get("remarks", ""))
        check("lead: room_type populated from sharing", body.get("room_type") == "Single")
        check("lead: existing fields intact (name)", body.get("name") == "Asha")
        check("lead: existing fields intact (gender)", body.get("gender") == "Female")
    finally:
        schedule_visit.http_post = orig["http_post"]
        schedule_visit.get_user_phone = orig["get_user_phone"]
        schedule_visit.get_aadhar_user_name = orig["get_aadhar_user_name"]
        schedule_visit.get_user_memory = orig["get_user_memory"]
        if orig["s_get_preferences"]:
            store.get_preferences = orig["s_get_preferences"]
        if orig["s_get_aadhar_gender"]:
            store.get_aadhar_gender = orig["s_get_aadhar_gender"]


if __name__ == "__main__":
    test_remarks_full_context()
    test_remarks_sparse_is_safe()
    test_remarks_budget_variants()
    test_lead_payload_includes_context()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(0 if _failed == 0 else 1)
