"""
core/reconcile.py — Reconciliation verifier + cron loop (TOP-1PCT Initiative 3).

Detects the "silent success" bug class: the bot told a user a booking succeeded,
but no record actually landed in RentOk's source of truth. A durable Postgres
ledger (`reconciliation_claims`) records every confirmed-success claim at the
write seam; this module re-polls RentOk per pending claim on an hourly cron and
marks each verified / missing.

Design invariants (see docs/superpowers/specs/2026-06-02-reconciliation-design.md):
  * "missing" = sustained absence across N hourly polls, never a single check.
  * A read error / timeout never penalizes a claim (stays pending, no attempt bump).
  * The reserve verifier reuses the EXACT proven contract already called by
    `check_reserve_bed` (reserve.py) — zero new RentOk dependency.
  * The visit verifier is gated behind RECONCILE_VISIT_ENABLED. Until the
    getBookingPreferences contract is probe-verified it returns None (skip), so
    visit claims accumulate `pending` losslessly and can NEVER become a false
    "missing". If the flag is flipped before the contract is wired, the stub
    raises → read-error path → still no false missing.
"""

from typing import Optional

import httpx

from config import settings
from core.log import get_logger

logger = get_logger("core.reconcile")


async def _verify_reserve(user_id: str, property_id: str) -> bool:
    """Reuse the proven checkPropetyReserved contract (reserve.py:check_reserve_bed)."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{settings.RENTOK_API_BASE_URL}/bookingBot/checkPropetyReserved",
            json={"user_id": user_id, "property_id": property_id},
        )
        resp.raise_for_status()
        data = resp.json()
    return data.get("data") is True


async def _verify_visit(user_id: str, property_id: str) -> bool:
    """Visit verifier — DORMANT until the getBookingPreferences contract is probe-verified.

    Reaching this means RECONCILE_VISIT_ENABLED was flipped before the contract
    was wired (Task 1 probe). Raise so the loop treats it as a read error
    (claim stays pending, no attempt bump) rather than ever fabricating a
    'missing'. Wire the real request/response here only after the probe.
    """
    raise NotImplementedError(
        "visit verifier not wired — probe getBookingPreferences contract first "
        "(spec Task 1) before enabling RECONCILE_VISIT_ENABLED"
    )


async def verify_claim(claim: dict) -> Optional[bool]:
    """Dispatch a claim to its event's read endpoint.

    Returns:
        True  — record found in RentOk        → caller marks 'verified'
        False — record absent                  → caller bumps attempts
        None  — skip (gated/unknown event)     → caller leaves pending, no bump
    Raises on read error/timeout               → caller leaves pending, no bump
    """
    event = claim.get("event")
    if event == "reserve":
        return await _verify_reserve(claim["uid"], claim["property_id"])
    if event == "visit":
        if not settings.RECONCILE_VISIT_ENABLED:
            return None  # gated — accumulate pending until contract probe-verified
        return await _verify_visit(claim["uid"], claim["property_id"])
    logger.warning("verify_claim: unknown event %r (claim id=%s)", event, claim.get("id"))
    return None


async def run_reconcile_batch(claims, verify_fn, mark_fn, max_attempts: int) -> dict:
    """Pure, injectable reconcile loop over a batch of pending claims.

    verify_fn(claim) -> Optional[bool] (or raises); mark_fn(claim_id, status, attempts).
    Returns a stats dict. Deps are injected so this is hermetically testable.
    """
    verified = missing = retry_pending = read_errors = skipped = 0
    for c in claims:
        try:
            found = await verify_fn(c)
        except Exception as e:
            # Read error/timeout: do NOT penalize the claim — leave it pending,
            # attempts unchanged, so a flaky read never manufactures a breach.
            read_errors += 1
            logger.warning("reconcile read error for claim id=%s: %s", c.get("id"), e)
            continue
        if found is None:
            # Gated (visit) or unknown event — leave pending, no attempt bump.
            skipped += 1
            continue
        if found:
            await mark_fn(c["id"], "verified", c.get("attempts", 0))
            verified += 1
        else:
            attempts = c.get("attempts", 0) + 1
            if attempts >= max_attempts:
                await mark_fn(c["id"], "missing", attempts)
                missing += 1
            else:
                await mark_fn(c["id"], "pending", attempts)
                retry_pending += 1
    return {
        "checked": len(claims),
        "verified": verified,
        "missing": missing,
        "retry_pending": retry_pending,
        "read_errors": read_errors,
        "skipped": skipped,
    }
