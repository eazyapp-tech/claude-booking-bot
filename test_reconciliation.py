"""
test_reconciliation.py — Reconciliation safety-check regression test (TOP-1PCT Initiative 3).

Proves the reconcile ledger + cron loop logic that detects "silent success"
(bot claimed a booking succeeded but no record landed in RentOk):

  1. Loop outcomes      — found→verified, absent→bump, absent×MAX→missing,
                          read-error→stays-pending-no-bump, skip(None)→no-bump.
  2. Ledger semantics    — grace-period exclusion, cancelled exclusion,
                          attempt persistence across runs, brand-scoped count.
  3. Verifier dispatch   — reserve→checkPropetyReserved, visit gated→skip,
                          unknown event→skip.
  4. Multi-run behaviour  — absent-then-found→verified; sustained absence→missing.

Deterministic: an in-memory FakeStore mirrors the Postgres helper semantics and
the verifier is stubbed. No network, no DB, no LLM. Run: `python test_reconciliation.py`.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

from config import settings  # noqa: E402
import core.reconcile as reconcile  # noqa: E402
from core.reconcile import run_reconcile_batch, verify_claim  # noqa: E402

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


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# In-memory store mirroring db/postgres.py reconciliation helper semantics.
# --------------------------------------------------------------------------- #
class FakeStore:
    def __init__(self):
        self.rows = []
        self._id = 0

    async def insert_claim(self, uid, phone, property_id, event, brand_hash=None, claimed_at=None):
        self._id += 1
        self.rows.append({
            "id": self._id,
            "uid": uid,
            "phone": phone,
            "property_id": property_id,
            "event": event,
            "brand_hash": brand_hash,
            "status": "pending",
            "attempts": 0,
            "claimed_at": claimed_at or datetime.now(),
            "resolved_at": None,
        })
        return self._id

    async def get_pending_claims(self, grace_cutoff, limit=200):
        out = [r for r in self.rows
               if r["status"] == "pending" and r["claimed_at"] < grace_cutoff]
        out.sort(key=lambda r: r["claimed_at"])
        return [dict(r) for r in out[:limit]]

    async def mark_claim(self, claim_id, status, attempts):
        for r in self.rows:
            if r["id"] == claim_id:
                r["status"] = status
                r["attempts"] = attempts
                if status in ("verified", "missing", "cancelled"):
                    r["resolved_at"] = datetime.now()
                return

    async def mark_claims_cancelled(self, uid, property_id):
        for r in self.rows:
            if r["uid"] == uid and r["property_id"] == property_id and r["status"] == "pending":
                r["status"] = "cancelled"
                r["resolved_at"] = datetime.now()

    async def count_missing_claims(self, brand_hash=None):
        return sum(1 for r in self.rows
                   if r["status"] == "missing" and (brand_hash is None or r["brand_hash"] == brand_hash))

    def status_of(self, claim_id):
        return next(r["status"] for r in self.rows if r["id"] == claim_id)

    def attempts_of(self, claim_id):
        return next(r["attempts"] for r in self.rows if r["id"] == claim_id)


OLD = datetime.now() - timedelta(hours=1)        # past grace
FRESH = datetime.now()                            # inside grace
CUTOFF = datetime.now() - timedelta(minutes=30)   # RECONCILE_GRACE_MINUTES


# --------------------------------------------------------------------------- #
# 1. Pure loop outcomes
# --------------------------------------------------------------------------- #
def section_loop_outcomes():
    print("\n[1] Loop outcomes")

    async def go():
        store = FakeStore()
        cid = await store.insert_claim("u1", None, "p1", "reserve", "brandA", claimed_at=OLD)
        claims = await store.get_pending_claims(CUTOFF)
        stats = await run_reconcile_batch(claims, lambda c: _ret(True), store.mark_claim, 4)
        check("found → verified", store.status_of(cid) == "verified", store.status_of(cid))
        check("found stats", stats["verified"] == 1 and stats["checked"] == 1, stats)

        store = FakeStore()
        cid = await store.insert_claim("u1", None, "p1", "reserve", "brandA", claimed_at=OLD)
        claims = await store.get_pending_claims(CUTOFF)
        stats = await run_reconcile_batch(claims, lambda c: _ret(False), store.mark_claim, 4)
        check("absent → stays pending (below MAX)", store.status_of(cid) == "pending", store.status_of(cid))
        check("absent → attempts bumped to 1", store.attempts_of(cid) == 1, store.attempts_of(cid))
        check("absent stats", stats["retry_pending"] == 1, stats)

        store = FakeStore()
        cid = await store.insert_claim("u1", None, "p1", "reserve", "brandA", claimed_at=OLD)
        claims = await store.get_pending_claims(CUTOFF)
        await run_reconcile_batch(claims, lambda c: _raise(), store.mark_claim, 4)
        check("read error → stays pending", store.status_of(cid) == "pending", store.status_of(cid))
        check("read error → attempts NOT bumped", store.attempts_of(cid) == 0, store.attempts_of(cid))

        store = FakeStore()
        cid = await store.insert_claim("u1", None, "p1", "reserve", "brandA", claimed_at=OLD)
        claims = await store.get_pending_claims(CUTOFF)
        stats = await run_reconcile_batch(claims, lambda c: _ret(None), store.mark_claim, 4)
        check("skip (None) → stays pending", store.status_of(cid) == "pending", store.status_of(cid))
        check("skip → attempts NOT bumped", store.attempts_of(cid) == 0, store.attempts_of(cid))
        check("skip stats", stats["skipped"] == 1, stats)

    run(go())


# --------------------------------------------------------------------------- #
# 2. Multi-run: sustained absence → missing; absent-then-found → verified
# --------------------------------------------------------------------------- #
def section_multi_run():
    print("\n[2] Multi-run sustained absence / recovery")

    async def absent_n_runs(verify_fn, runs, max_attempts=4):
        store = FakeStore()
        cid = await store.insert_claim("u1", None, "p1", "reserve", "brandA", claimed_at=OLD)
        for _ in range(runs):
            claims = await store.get_pending_claims(CUTOFF)
            await run_reconcile_batch(claims, verify_fn, store.mark_claim, max_attempts)
        return store, cid

    async def go():
        # Always absent, MAX_ATTEMPTS=4 → 'missing' only after the 4th poll.
        store, cid = await absent_n_runs(lambda c: _ret(False), 3)
        check("3 absent polls (MAX=4) → still pending", store.status_of(cid) == "pending", store.status_of(cid))
        check("attempts == 3 after 3 polls", store.attempts_of(cid) == 3, store.attempts_of(cid))

        store, cid = await absent_n_runs(lambda c: _ret(False), 4)
        check("4th absent poll → missing", store.status_of(cid) == "missing", store.status_of(cid))
        check("missing surfaces in count", await store.count_missing_claims("brandA") == 1, "")

        # Absent on run 1, found on run 2 → verified (sustained-absence never reached).
        store = FakeStore()
        cid = await store.insert_claim("u1", None, "p1", "reserve", "brandA", claimed_at=OLD)
        seq = iter([False, True])
        await run_reconcile_batch(await store.get_pending_claims(CUTOFF),
                                  lambda c: _ret(next(seq)), store.mark_claim, 4)
        check("after absent run 1 → pending", store.status_of(cid) == "pending", store.status_of(cid))
        await run_reconcile_batch(await store.get_pending_claims(CUTOFF),
                                  lambda c: _ret(next(seq)), store.mark_claim, 4)
        check("found run 2 → verified", store.status_of(cid) == "verified", store.status_of(cid))

    run(go())


# --------------------------------------------------------------------------- #
# 3. Ledger semantics: grace, cancelled, brand-scoped count
# --------------------------------------------------------------------------- #
def section_ledger_semantics():
    print("\n[3] Ledger semantics")

    async def go():
        # Grace: a fresh claim is excluded; an old one is included.
        store = FakeStore()
        fresh = await store.insert_claim("u1", None, "p1", "reserve", "brandA", claimed_at=FRESH)
        old = await store.insert_claim("u2", None, "p2", "reserve", "brandA", claimed_at=OLD)
        pending = await store.get_pending_claims(CUTOFF)
        ids = {c["id"] for c in pending}
        check("fresh claim excluded by grace window", fresh not in ids, ids)
        check("old claim included past grace", old in ids, ids)

        # Cancelled: cancel marks pending→cancelled; it drops out of pending.
        store = FakeStore()
        cid = await store.insert_claim("u1", None, "p1", "reserve", "brandA", claimed_at=OLD)
        await store.mark_claims_cancelled("u1", "p1")
        check("cancel marks claim cancelled", store.status_of(cid) == "cancelled", store.status_of(cid))
        pending = await store.get_pending_claims(CUTOFF)
        check("cancelled claim excluded from pending (no false missing)", cid not in {c["id"] for c in pending}, "")
        # And a verifier run must never resurrect it.
        await run_reconcile_batch(pending, lambda c: _ret(False), store.mark_claim, 1)
        check("cancelled stays cancelled after a cron run", store.status_of(cid) == "cancelled", store.status_of(cid))

        # Brand-scoped count: only the queried brand's missing claims are counted.
        store = FakeStore()
        a = await store.insert_claim("u1", None, "p1", "reserve", "brandA", claimed_at=OLD)
        b = await store.insert_claim("u2", None, "p2", "reserve", "brandB", claimed_at=OLD)
        await store.mark_claim(a, "missing", 4)
        await store.mark_claim(b, "missing", 4)
        check("brand-scoped count isolates brandA", await store.count_missing_claims("brandA") == 1, "")
        check("brand-scoped count isolates brandB", await store.count_missing_claims("brandB") == 1, "")
        check("global count sees both", await store.count_missing_claims(None) == 2, "")

    run(go())


# --------------------------------------------------------------------------- #
# 4. Verifier dispatch (reserve proven; visit gated; unknown skipped)
# --------------------------------------------------------------------------- #
def section_verifier_dispatch():
    print("\n[4] Verifier dispatch")

    async def go():
        # reserve → routes to _verify_reserve (stubbed, no network).
        orig = reconcile._verify_reserve
        calls = {}

        async def fake_reserve(user_id, property_id):
            calls["reserve"] = (user_id, property_id)
            return True

        reconcile._verify_reserve = fake_reserve
        try:
            out = await verify_claim({"id": 1, "event": "reserve", "uid": "u1", "property_id": "p1"})
            check("reserve event dispatches to checkPropetyReserved verifier", out is True, out)
            check("reserve verifier receives uid+property_id", calls.get("reserve") == ("u1", "p1"), calls)
        finally:
            reconcile._verify_reserve = orig

        # visit gated off (default) → None (skip), verifier never invoked.
        old_flag = settings.RECONCILE_VISIT_ENABLED
        settings.RECONCILE_VISIT_ENABLED = False
        try:
            out = await verify_claim({"id": 2, "event": "visit", "uid": "u1", "property_id": "p1"})
            check("visit gated off → skip (None), never a false missing", out is None, out)
        finally:
            settings.RECONCILE_VISIT_ENABLED = old_flag

        # visit flag flipped before contract wired → stub raises → treated as read error upstream.
        settings.RECONCILE_VISIT_ENABLED = True
        raised = False
        try:
            await verify_claim({"id": 3, "event": "visit", "uid": "u1", "property_id": "p1"})
        except NotImplementedError:
            raised = True
        finally:
            settings.RECONCILE_VISIT_ENABLED = old_flag
        check("visit flag flipped pre-probe → raises (safe-fails to pending, not missing)", raised, "")

        # unknown event → None (skip).
        out = await verify_claim({"id": 4, "event": "payment", "uid": "u1", "property_id": "p1"})
        check("unknown event → skip (None)", out is None, out)

    run(go())


# --------------------------------------------------------------------------- #
# Small async return helpers (verify_fn must be awaitable).
# --------------------------------------------------------------------------- #
async def _ret(v):
    return v


async def _raise():
    raise httpx_timeout()


def httpx_timeout():
    return RuntimeError("simulated RentOk read timeout")


if __name__ == "__main__":
    section_loop_outcomes()
    section_multi_run()
    section_ledger_semantics()
    section_verifier_dispatch()
    print(f"\n{'='*48}\n  {_passed} passed, {_failed} failed\n{'='*48}")
    sys.exit(1 if _failed else 0)
