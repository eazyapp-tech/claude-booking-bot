"""
test_tool_boundary.py — Wave 3 tool-boundary hardening regression test.

Proves the three guards wired into the single ToolExecutor.execute seam:
  1. Input validation   — malformed tool args are rejected with a clear,
     self-correcting message before the handler ever runs.
  2. Idempotency        — write-path tools burst-dedup: a re-fired identical
     call replays the cached result (handler runs once); an in-flight duplicate
     is told to wait; a failed call releases the lock so a retry can proceed;
     different args / read-only tools are never deduped.
  3. Per-tool timeout   — a handler exceeding TOOL_TIMEOUT_SECONDS is cancelled
     and surfaced as an error (lock released so a retry can proceed).

Deterministic: an in-memory fake replaces the Redis client. No network, no LLM.
Run: `python test_tool_boundary.py`.
"""

import asyncio
import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

import db.redis._base as _base  # noqa: E402
import db.redis.idempotency as idem  # noqa: E402


class _FakeRedis:
    """Just enough of the redis client for the idempotency primitives."""
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
_base._r = lambda: _fake      # _json_get/_json_set resolve _r() from _base at call time
idem._r = lambda: _fake       # idem_begin's direct SET NX uses the name bound in idempotency

from config import settings  # noqa: E402
from core.tool_executor import ToolExecutor  # noqa: E402
from tools.registry import register_tool  # noqa: E402

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


def reset_redis():
    _fake.store.clear()


# --------------------------------------------------------------------------- #
# 1. Input validation
# --------------------------------------------------------------------------- #
register_tool(
    "reserve_bed",
    {
        "name": "reserve_bed",
        "description": "test",
        "input_schema": {
            "type": "object",
            "properties": {"property_name": {"type": "string"}},
            "required": ["property_name"],
        },
    },
    handler=None,  # overwritten below; registry only supplies the schema for validation
)


def section_validation():
    print("\n[1] Input validation")
    reset_redis()
    calls = {"n": 0}

    async def reserve_handler(user_id, property_name):
        calls["n"] += 1
        return f"reserved {property_name}"

    ex = ToolExecutor()
    ex.register("reserve_bed", reserve_handler)

    out = asyncio.run(ex.execute("reserve_bed", {"property_name": "Kurla PG"}, "u1"))
    check("valid args execute handler", calls["n"] == 1 and "reserved" in out, out)

    out = asyncio.run(ex.execute("reserve_bed", {}, "u2"))
    check("missing required field rejected", "Invalid arguments" in out and "missing" in out, out)
    check("rejected call never reached handler", calls["n"] == 1, calls["n"])

    out = asyncio.run(ex.execute("reserve_bed", {"property_name": 123}, "u3"))
    check("wrong-type field rejected", "Invalid arguments" in out and "should be string" in out, out)


# --------------------------------------------------------------------------- #
# 2. Idempotency
# --------------------------------------------------------------------------- #
def section_idempotency():
    print("\n[2] Idempotency burst-dedup")
    reset_redis()
    calls = {"n": 0}

    async def reserve_handler(user_id, property_name):
        calls["n"] += 1
        return f"reserved {property_name} #{calls['n']}"

    ex = ToolExecutor()
    ex.register("reserve_bed", reserve_handler)

    out1 = asyncio.run(ex.execute("reserve_bed", {"property_name": "Kurla PG"}, "u1"))
    out2 = asyncio.run(ex.execute("reserve_bed", {"property_name": "Kurla PG"}, "u1"))
    check("first call runs handler", "#1" in out1, out1)
    check("identical re-fire replays cached result", out2 == out1, out2)
    check("handler ran exactly once for the duplicate", calls["n"] == 1, calls["n"])

    out3 = asyncio.run(ex.execute("reserve_bed", {"property_name": "Andheri PG"}, "u1"))
    check("different args run a fresh execution", "#2" in out3, out3)

    out4 = asyncio.run(ex.execute("reserve_bed", {"property_name": "Kurla PG"}, "u2"))
    check("different user is not deduped", "#3" in out4, out4)

    # In-flight duplicate: lock present, no cached result yet.
    reset_redis()
    from core.tool_boundary import idempotency_key
    k = idempotency_key("u9", "reserve_bed", {"property_name": "Held PG"})
    _fake.store[f"idem:lock:{k}"] = b"1"  # simulate another call mid-flight
    out = asyncio.run(ex.execute("reserve_bed", {"property_name": "Held PG"}, "u9"))
    check("in-flight duplicate told to wait", "still processing" in out, out)

    # Read-only tool is never deduped (not in IDEMPOTENT_TOOLS).
    reset_redis()
    rcalls = {"n": 0}

    async def search_handler(user_id, q):
        rcalls["n"] += 1
        return f"results {rcalls['n']}"

    register_tool("search_properties", {"name": "search_properties", "description": "t",
                                        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}},
                  handler=None)
    ex.register("search_properties", search_handler)
    asyncio.run(ex.execute("search_properties", {"q": "x"}, "u1"))
    asyncio.run(ex.execute("search_properties", {"q": "x"}, "u1"))
    check("read-only tool not deduped", rcalls["n"] == 2, rcalls["n"])


# --------------------------------------------------------------------------- #
# 3. Failure releases the lock (retry allowed)
# --------------------------------------------------------------------------- #
def section_failure_release():
    print("\n[3] Failure releases lock")
    reset_redis()
    calls = {"n": 0}

    async def flaky_handler(user_id, property_name):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("CRM down")
        return f"reserved {property_name}"

    ex = ToolExecutor()
    ex.register("reserve_bed", flaky_handler)

    out1 = asyncio.run(ex.execute("reserve_bed", {"property_name": "Retry PG"}, "u1"))
    check("failed call surfaces error", "CRM down" in out1, out1)
    from core.tool_boundary import idempotency_key
    k = idempotency_key("u1", "reserve_bed", {"property_name": "Retry PG"})
    check("lock released after failure", f"idem:lock:{k}" not in _fake.store, _fake.store)
    check("no result cached after failure", f"idem:result:{k}" not in _fake.store, _fake.store)

    out2 = asyncio.run(ex.execute("reserve_bed", {"property_name": "Retry PG"}, "u1"))
    check("retry after failure runs handler again and succeeds", "reserved" in out2 and calls["n"] == 2, out2)


# --------------------------------------------------------------------------- #
# 4. Per-tool timeout
# --------------------------------------------------------------------------- #
def section_timeout():
    print("\n[4] Per-tool timeout")
    reset_redis()
    original = settings.TOOL_TIMEOUT_SECONDS
    settings.TOOL_TIMEOUT_SECONDS = 0.05
    try:
        async def slow_handler(user_id, property_name):
            await asyncio.sleep(1.0)
            return "too late"

        ex = ToolExecutor()
        ex.register("reserve_bed", slow_handler)

        out = asyncio.run(ex.execute("reserve_bed", {"property_name": "Slow PG"}, "u1"))
        check("slow handler is cancelled and surfaced as error", "Error executing reserve_bed" in out, out)
        from core.tool_boundary import idempotency_key
        k = idempotency_key("u1", "reserve_bed", {"property_name": "Slow PG"})
        check("lock released after timeout", f"idem:lock:{k}" not in _fake.store, _fake.store)
    finally:
        settings.TOOL_TIMEOUT_SECONDS = original


if __name__ == "__main__":
    section_validation()
    section_idempotency()
    section_failure_release()
    section_timeout()
    print(f"\n{'='*48}\n  {_passed} passed, {_failed} failed\n{'='*48}")
    sys.exit(1 if _failed else 0)
