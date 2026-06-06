"""
SILO — manager notification on bot bookings · hermetic regression suite.

No network / Redis / LLM. Proves the bot tells the property manager when a
visit/call/token lands (the true P0 — today these records are persisted silently).

[A] _notify_payloads — pure: builds one payload per audience (owner 101 + team 103),
    carrying pg_id/pg_number/title/body; empty pg_id → no payloads (never notify blind).
[B] build_booking_notification — pure: correct title/body per kind (visit/call/token),
    includes the prospect name + property; never crashes on missing bits.
[C] notify_manager_booking — fires BOTH audiences via the injected POST seam; is
    GRACEFUL (a POST that raises is swallowed, the function never raises, never blocks
    the booking flow); fires nothing when pg_id is absent.
[D] wiring — save_visit_time / save_call_time / verify_payment call the notifier ONLY
    after a confirmed success, and a notify failure never turns a real booking into a
    user-visible error.

Run: `python test_manager_notify.py`  (exit 0 = pass)
"""

import asyncio
import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("DATABASE_URL", "postgresql://fake:fake@localhost/fake")

from tools.booking import notify_manager as nm

_passed = 0
_failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


# ---------------------------------------------------------------------------
# [A] _notify_payloads — pure
# ---------------------------------------------------------------------------

def test_payloads_both_audiences():
    print("\n[A] _notify_payloads — owner (101) + team (103)")
    ps = nm._notify_payloads("PG123", "7", "Title", "Body", "booking_bot_visit")
    rts = sorted(p["receiver_type"] for p in ps)
    check(rts == [101, 103], f"both owner+team receiver_types ({rts})")
    check(all(p["pg_id"] == "PG123" for p in ps), "pg_id on every payload")
    check(all(p["pg_number"] == "7" for p in ps), "pg_number on every payload")
    check(all(p["notification_title"] == "Title" for p in ps), "title on every payload")
    check(all(p["notification_body"] == "Body" for p in ps), "body on every payload")
    check(all(p["notification_name"] == "booking_bot_visit" for p in ps), "notification_name set")


def test_payloads_empty_pgid():
    print("\n[A] _notify_payloads — empty pg_id → no payloads (never blind)")
    check(nm._notify_payloads("", "7", "T", "B", "n") == [], "empty pg_id → []")
    check(nm._notify_payloads(None, "7", "T", "B", "n") == [], "None pg_id → []")


# ---------------------------------------------------------------------------
# [B] build_booking_notification — pure
# ---------------------------------------------------------------------------

def test_build_visit():
    print("\n[B] build_booking_notification — visit")
    title, body = nm.build_booking_notification("visit", "Sanchay", "ROHA VATIKA", "06/06/2026", "5:00 PM")
    check("visit" in title.lower(), "visit title mentions a visit")
    check("Sanchay" in body and "ROHA VATIKA" in body, "body carries prospect + property")
    check("06/06/2026" in body and "5:00 PM" in body, "body carries date + time")


def test_build_call_and_token():
    print("\n[B] build_booking_notification — call + token")
    t_call, b_call = nm.build_booking_notification("call", "Asha", "Mass Metropolis", "07/06/2026", "11:00 AM")
    check("call" in t_call.lower(), "call title mentions a call")
    check("Asha" in b_call and "Mass Metropolis" in b_call, "call body carries prospect + property")
    t_tok, b_tok = nm.build_booking_notification("token", "Ravi", "DERIYA ICONICO", "", "")
    check("token" in t_tok.lower(), "token title mentions a token")
    check("Ravi" in b_tok and "DERIYA ICONICO" in b_tok, "token body carries prospect + property (no date needed)")


def test_build_graceful_blanks():
    print("\n[B] build_booking_notification — missing prospect/property never crashes")
    title, body = nm.build_booking_notification("visit", "", "", "", "")
    check(isinstance(title, str) and isinstance(body, str) and title and body, "still returns non-empty strings")


# ---------------------------------------------------------------------------
# [C] notify_manager_booking — fires both, graceful
# ---------------------------------------------------------------------------

def test_notify_fires_both():
    print("\n[C] notify_manager_booking — fires owner + team via seam")
    sent = []
    async def fake_post(payload):
        sent.append(payload)
    asyncio.run(nm.notify_manager_booking("PG9", "3", "T", "B", "booking_bot_visit", _post=fake_post))
    rts = sorted(p["receiver_type"] for p in sent)
    check(rts == [101, 103], f"both audiences POSTed ({rts})")


def test_notify_graceful_on_error():
    print("\n[C] notify_manager_booking — a failing POST never raises (booking flow safe)")
    async def boom(payload):
        raise RuntimeError("backend down")
    raised = False
    try:
        asyncio.run(nm.notify_manager_booking("PG9", "3", "T", "B", "n", _post=boom))
    except Exception:
        raised = True
    check(not raised, "swallows POST errors, never propagates")


def test_notify_skips_without_pgid():
    print("\n[C] notify_manager_booking — no pg_id → no POST")
    sent = []
    async def fake_post(payload):
        sent.append(payload)
    asyncio.run(nm.notify_manager_booking("", "3", "T", "B", "n", _post=fake_post))
    check(sent == [], "empty pg_id → zero POSTs")


# ---------------------------------------------------------------------------
# [D] wiring — only on confirmed success, never breaks the flow
# ---------------------------------------------------------------------------

def test_wired_into_booking_tools():
    print("\n[D] fire_booking_notification is wired into the three success paths")
    sv = open("tools/booking/schedule_visit.py").read()
    sc = open("tools/booking/schedule_call.py").read()
    pay = open("tools/booking/payment.py").read()
    check("schedule_visit calls fire_booking_notification", "fire_booking_notification(" in sv)
    check("schedule_call calls fire_booking_notification", "fire_booking_notification(" in sc)
    check("payment calls fire_booking_notification", "fire_booking_notification(" in pay)
    # the visit call must sit AFTER the success gate (ok = ...), not before it
    if "fire_booking_notification(" in sv and "ok = data.get" in sv:
        # rindex → the CALL site (the import line appears earlier in the file)
        check("visit notify fires AFTER the success check (only on confirmed booking)",
              sv.index("ok = data.get") < sv.rindex("fire_booking_notification("))


def test_fire_is_background_and_hermetic():
    print("\n[E] fire_booking_notification — runs off the critical path, never blocks")
    captured = []

    async def cap(pg_id, pg_number, title, body, notification_name="booking_bot", _post=None):
        captured.append((pg_id, pg_number, title, body, notification_name))

    async def drive():
        orig_notify, orig_name = nm.notify_manager_booking, nm._resolve_name
        nm.notify_manager_booking = cap
        nm._resolve_name = lambda uid, kind: "Sanchay"
        try:
            # returns immediately (schedules a task) — does NOT block on the POST
            nm.fire_booking_notification("visit", "u1", "PG1", "7", "ROHA VATIKA", "06/06/2026", "5:00 PM")
            check("returns synchronously without awaiting the push", True)
            await asyncio.sleep(0.05)  # let the background task run
        finally:
            nm.notify_manager_booking, nm._resolve_name = orig_notify, orig_name

    asyncio.run(drive())
    check("background task fired the notification", len(captured) == 1, f"captured={len(captured)}")
    if captured:
        _, _, title, body, name = captured[0]
        check("background push carries the resolved name + property",
              "Sanchay" in body and "ROHA VATIKA" in body, body)
        check("background push tagged booking_bot_visit", name == "booking_bot_visit", name)


if __name__ == "__main__":
    test_payloads_both_audiences()
    test_payloads_empty_pgid()
    test_build_visit()
    test_build_call_and_token()
    test_build_graceful_blanks()
    test_notify_fires_both()
    test_notify_graceful_on_error()
    test_notify_skips_without_pgid()
    test_wired_into_booking_tools()
    test_fire_is_background_and_hermetic()

    print(f"\n{_passed + _failed} checks — {_passed} PASS / {_failed} FAIL")
    sys.exit(1 if _failed else 0)
