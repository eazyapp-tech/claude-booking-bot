"""
test_server_stop.py — server-authoritative Stop regression test.

Proves Wave 1 cheap win #2: a web client's Stop is honored server-side via the
Phase-C cancel flag (not just a client-side fetch abort), the POST /chat/stop
endpoint sets that flag, and /chat/stream clears any stale flag at the start of
a fresh run so a prior Stop cannot cancel the next turn.

Deterministic: an in-memory fake replaces the Redis client. No network, no LLM.
Run: `python test_server_stop.py`.
"""

import asyncio
import inspect
import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

import db.redis.conversation as conv  # noqa: E402


class _FakeRedis:
    """Just enough of the redis client for the cancel-flag primitives."""
    def __init__(self):
        self.store = {}

    def set(self, key, val, ex=None):
        self.store[key] = val

    def delete(self, key):
        self.store.pop(key, None)

    def exists(self, key):
        return 1 if key in self.store else 0


_fake = _FakeRedis()
conv._r = lambda: _fake  # monkeypatch the connection accessor

from db.redis.conversation import (  # noqa: E402
    set_cancel_requested, clear_cancel_requested, is_cancel_requested,
)
from routers import chat  # noqa: E402
from routers.chat import chat_stop, StopRequest  # noqa: E402

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


print("Server-authoritative Stop\n")

# ── 1. Cancel-flag round-trip ────────────────────────────────────────────────
uid = "web-user-1"
check("no flag initially", is_cancel_requested(uid) is False)
set_cancel_requested(uid)
check("set → flag present", is_cancel_requested(uid) is True)
clear_cancel_requested(uid)
check("clear → flag gone", is_cancel_requested(uid) is False)

# ── 2. POST /chat/stop sets the flag for the user ────────────────────────────
clear_cancel_requested(uid)
result = asyncio.run(chat_stop(StopRequest(user_id=uid)))
check("stop endpoint returns ok", result == {"status": "ok"})
check("stop endpoint set the cancel flag", is_cancel_requested(uid) is True)

# ── 3. Stop is per-user (does not bleed across users) ────────────────────────
other = "web-user-2"
clear_cancel_requested(other)
asyncio.run(chat_stop(StopRequest(user_id=uid)))
check("other user not flagged by a stop targeting uid",
      is_cancel_requested(other) is False)

# ── 4. Empty user_id is rejected ─────────────────────────────────────────────
from fastapi import HTTPException  # noqa: E402
try:
    asyncio.run(chat_stop(StopRequest(user_id="")))
    check("empty user_id rejected", False, "no exception raised")
except HTTPException as e:
    check("empty user_id rejected (400)", e.status_code == 400)

# ── 5. Route is registered + stream clears stale flag at start ───────────────
paths = [getattr(r, "path", None) for r in chat.router.routes]
check("/chat/stop route registered", "/chat/stop" in paths)
src = inspect.getsource(chat.chat_stream)
check("chat_stream clears stale flag before running",
      "clear_cancel_requested(req.user_id)" in src)

# ── 6. Engine streaming path honors the flag between tool rounds ─────────────
import core.claude as claude  # noqa: E402
stream_src = inspect.getsource(claude.AnthropicEngine.run_agent_stream)
check("engine checks is_cancel_requested in the streaming tool loop",
      "is_cancel_requested(user_id)" in stream_src)
check("engine clears the flag when it trips (no lingering cancel)",
      "clear_cancel_requested(user_id)" in stream_src)

# ── Summary ──────────────────────────────────────────────────────────────────
print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
