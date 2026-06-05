"""
test_osrm_circuit.py — OSRM call wrapper + circuit breaker (C1).

maps.rentok.com (OSRM routing) can be down for extended periods (confirmed: the
EC2 host is unreachable at the network level). Rather than pay the per-call
timeout on EVERY commute interaction, core/osrm.py trips a breaker after a
failure and skips OSRM for a cooldown — going straight to the honest
straight-line fallback — then probes once after the cooldown to auto-detect
recovery. This keeps both estimate_commute and R1 ranking instant + honest
whether or not OSRM ever comes back, and self-heals when it does.

Deterministic: in-memory fake Redis, stubbed http_get. No network. No LLM.
Run: `python test_osrm_circuit.py`  (exit 0 = pass).
"""
import asyncio
import os
import sys
import time

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

import db.redis._base as _base  # noqa: E402


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, val):
        self.store[key] = val
        return True

    def set(self, key, val, ex=None, nx=False):
        self.store[key] = val
        return True

    def delete(self, key):
        self.store.pop(key, None)


_fake = _FakeRedis()
_base._r = lambda: _fake

import core.osrm as osrm  # noqa: E402

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


def _clear():
    _fake.store.clear()


# Track http_get invocations through the wrapper.
_calls = {"n": 0}


async def _ok(url, params=None):
    _calls["n"] += 1
    return {"durations": [[0, 600]]}


async def _boom(url, params=None):
    _calls["n"] += 1
    raise RuntimeError("OSRM down")


# --------------------------------------------------------------------------- #
print("\n[1] osrm_should_skip — breaker state")
_clear()
check("1a no key → not skipping (closed)", osrm.osrm_should_skip() is False)

osrm._trip()
check("1b after trip → skipping (open)", osrm.osrm_should_skip() is True)

# Cooldown elapsed → allow a probe (half-open).
_fake.store[osrm._BREAKER_KEY] = str(time.time() - 1)
check("1c cooldown elapsed → not skipping (half-open probe allowed)",
      osrm.osrm_should_skip() is False)

osrm._reset()
check("1d after reset → not skipping (closed)", osrm.osrm_should_skip() is False)


# --------------------------------------------------------------------------- #
print("\n[2] osrm_get — success closes the breaker")
_clear()
osrm.http_get = _ok
_calls["n"] = 0
data = arun(osrm.osrm_get("http://x", params={"a": "b"}))
check("2a returns the parsed data on success", data == {"durations": [[0, 600]]}, data)
check("2b http_get was actually called", _calls["n"] == 1, _calls["n"])
check("2c breaker stays closed after success", osrm.osrm_should_skip() is False)


# --------------------------------------------------------------------------- #
print("\n[3] osrm_get — failure trips the breaker, returns None (never raises)")
_clear()
osrm.http_get = _boom
_calls["n"] = 0
data = arun(osrm.osrm_get("http://x"))
check("3a returns None on failure (no raise)", data is None)
check("3b http_get was attempted", _calls["n"] == 1, _calls["n"])
check("3c breaker is now open", osrm.osrm_should_skip() is True)


# --------------------------------------------------------------------------- #
print("\n[4] osrm_get — open breaker SKIPS the call (no per-call timeout tax)")
# Breaker is open from [3]. A failing http_get must NOT be invoked.
osrm.http_get = _boom
_calls["n"] = 0
data = arun(osrm.osrm_get("http://x"))
check("4a returns None while open", data is None)
check("4b http_get was NOT called (skipped, no wait)", _calls["n"] == 0, _calls["n"])


# --------------------------------------------------------------------------- #
print("\n[5] recovery — after cooldown, a probe succeeds and closes the breaker")
# Force cooldown to have elapsed, point http_get at a healthy stub.
_fake.store[osrm._BREAKER_KEY] = str(time.time() - 1)
osrm.http_get = _ok
_calls["n"] = 0
data = arun(osrm.osrm_get("http://x"))
check("5a probe call goes through after cooldown", _calls["n"] == 1, _calls["n"])
check("5b probe returns data", data == {"durations": [[0, 600]]}, data)
check("5c breaker closed again (self-healed)", osrm.osrm_should_skip() is False)


# --------------------------------------------------------------------------- #
print("\n[6] timeout is honored (slow OSRM → None, breaker trips)")
_clear()
async def _hang(url, params=None):
    _calls["n"] += 1
    await asyncio.sleep(5)
    return {"x": 1}
osrm.http_get = _hang
_calls["n"] = 0
data = arun(osrm.osrm_get("http://x", timeout=0.2))
check("6a slow call → None (capped by timeout)", data is None)
check("6b breaker tripped after timeout", osrm.osrm_should_skip() is True)


# --------------------------------------------------------------------------- #
print("\n[7] Redis hiccup never blocks OSRM")
def _boom_redis():
    raise RuntimeError("redis down")
_base._r = _boom_redis
check("7a should_skip → False on Redis error (fail-open, never blocks)",
      osrm.osrm_should_skip() is False)
_base._r = lambda: _fake


# --------------------------------------------------------------------------- #
print(f"\n{'='*60}")
print(f"RESULT: {_passed} passed, {_failed} failed")
print(f"{'='*60}")
sys.exit(0 if _failed == 0 else 1)
