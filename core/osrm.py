"""OSRM call wrapper with a circuit breaker (C1).

maps.rentok.com (OSRM routing) can be down for long stretches — the host has been
unreachable at the network level (EC2 stopped/terminated), not just an app crash.
Calling it anyway makes every commute interaction wait out a timeout for nothing.

This wrapper trips a breaker after a failure and SKIPS OSRM for a cooldown window —
callers fall straight through to their honest straight-line fallback — then lets
exactly the next call after the cooldown probe for recovery. So both estimate_commute
and R1 ranking stay instant + honest whether OSRM is up, down, or flapping, and
self-heal the moment the host returns. No config flip, no redeploy.

All Redis touches fail-open: a Redis hiccup never blocks an OSRM attempt.
"""
import asyncio
import time

from utils.retry import http_get
from core.log import get_logger

logger = get_logger("core.osrm")

_BREAKER_KEY = "osrm:down_until"   # value = unix ts until which OSRM is skipped
_COOLDOWN_S = 600                  # skip OSRM for 10 min after it trips
_DEFAULT_TIMEOUT_S = 6             # hard cap per call (covers http_get's retries)


def _redis():
    # Re-imported per call so tests that patch db.redis._base._r are honored.
    from db.redis._base import _r
    return _r()


def osrm_should_skip() -> bool:
    """True while the breaker is open (skip OSRM, use the caller's fallback).

    Once the cooldown timestamp has passed this returns False so exactly the next
    call probes for recovery (half-open). Fails OPEN on any Redis error — a cache
    problem must never stop us from trying OSRM.
    """
    try:
        raw = _redis().get(_BREAKER_KEY)
        if not raw:
            return False
        return time.time() < float(raw)
    except Exception:
        return False


def _trip() -> None:
    """Open the breaker for the cooldown window."""
    try:
        until = time.time() + _COOLDOWN_S
        _redis().setex(_BREAKER_KEY, _COOLDOWN_S, str(until))
    except Exception:
        pass


def _reset() -> None:
    """Close the breaker (OSRM is healthy again)."""
    try:
        _redis().delete(_BREAKER_KEY)
    except Exception:
        pass


async def osrm_get(url: str, params: dict | None = None,
                   timeout: float = _DEFAULT_TIMEOUT_S):
    """Call OSRM through the breaker.

    Returns the parsed JSON dict on success, or None when the breaker is open or
    the call fails/times out — the caller then uses its straight-line fallback.
    Never raises. Success closes the breaker; failure opens it.
    """
    if osrm_should_skip():
        logger.debug("OSRM breaker open — skipping call, caller uses fallback")
        return None
    try:
        data = await asyncio.wait_for(http_get(url, params=params), timeout=timeout)
        _reset()
        return data
    except Exception as e:
        _trip()
        logger.info("OSRM call failed (%s) — breaker open for %ds", e, _COOLDOWN_S)
        return None
