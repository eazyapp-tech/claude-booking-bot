"""Payment-link first-reservation race regression.

Deterministic, no network/LLM/Redis. Stubs payment.py's module deps and drives
the REAL create_payment_link to prove:

  R1  A brand-new tenant whose get-tenant_uuid is empty at first becomes
      resolvable after the lead is created + a couple of retries → a real
      payment link is produced and set_payment_info is called (the bug: the old
      code did ONE immediate re-fetch and gave up, failing every first booking).
  R2  If the UUID never resolves, create_payment_link returns an honest
      "Could not generate payment link" — never a fabricated link.
  R3  The retry count is bounded (get-tenant_uuid is not called more than
      1 initial + _UUID_RETRIES times).

Run: python test_payment_uuid_retry.py   (exit 0 = pass)
"""
import asyncio

import tools.booking.payment as payment
import tools.booking.schedule_visit as schedule_visit

_passed = 0


def check(cond, label):
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        raise SystemExit(1)


# No real sleeping in tests.
payment._UUID_RETRY_DELAY = 0

_PROP = {
    "property_name": "ROHA VATIKA 1406 BOY'S KURLA",
    "eazypg_id": "4000043334D",
    "pg_id": "UaDCGP3dzzZRgVIzBDgXb5ry5ng2",
    "pg_number": "4",
    "property_min_token_amount": 1000,
}


def _install(uuid_ready_on_call, *, lead_raises=False):
    """Stub payment.py deps. get-tenant_uuid returns a uuid only once the call
    counter reaches `uuid_ready_on_call` (use a huge number to never resolve)."""
    state = {"uuid_calls": 0, "set_payment": None}

    payment.get_user_phone = lambda uid: "9000012345"
    payment._find_property = lambda uid, name: dict(_PROP)
    payment.check_rentok_response = lambda *a, **k: None
    payment.set_payment_info = lambda *a, **k: state.__setitem__("set_payment", a)
    payment.schedule_followup = lambda *a, **k: None

    async def _lead(*a, **k):
        if lead_raises:
            raise RuntimeError("lead boom")
        return True
    schedule_visit._create_external_lead = _lead

    async def _http_get(url, params=None):
        if "get-tenant_uuid" in url:
            state["uuid_calls"] += 1
            uuid = "uuid-123" if state["uuid_calls"] >= uuid_ready_on_call else ""
            return {"status": 200, "data": {"tenant_uuid": uuid}}
        if "lead-payment-link" in url:
            return {"status": 200, "data": {"link": "xWojPW", "pg_name": "ROHA VATIKA 1406 BOY'S KURLA"}}
        return {}
    payment.http_get = _http_get
    return state


def run():
    # R1 — empty on the first 3 calls (initial + 2 retries), resolves on the 4th.
    state = _install(uuid_ready_on_call=4)
    out = asyncio.run(payment.create_payment_link("u1", "ROHA VATIKA 1406 BOY'S KURLA"))
    check("pay.rentok.com/p/xWojPW" in out, "R1 retry recovers UUID → real payment link")
    check("1000" in out, "R1 surfaces the ₹1000 token amount")
    check(state["set_payment"] is not None, "R1 set_payment_info persisted")

    # R3 — bounded: 1 initial fetch + up to _UUID_RETRIES retries.
    check(state["uuid_calls"] <= 1 + payment._UUID_RETRIES, "R3 UUID lookups stay bounded")

    # R2 — never resolves → honest failure, no fabricated link.
    state = _install(uuid_ready_on_call=999)
    out = asyncio.run(payment.create_payment_link("u2", "ROHA VATIKA 1406 BOY'S KURLA"))
    check("Could not generate payment link" in out, "R2 exhausted retries → honest failure")
    check("pay.rentok.com" not in out, "R2 never fabricates a link on failure")
    check(state["set_payment"] is None, "R2 no payment info persisted on failure")

    print(f"\n{_passed}/{_passed} assertions passed")


if __name__ == "__main__":
    run()
