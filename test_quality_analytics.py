"""
test_quality_analytics.py — Regression tests for track_daily_quality + get_quality_trend.

Covers:
  1. track_daily_quality — accumulates sum+count with Redis pipeline (no sequential calls)
  2. track_daily_quality — dual-writes global + brand-scoped keys
  3. get_quality_trend — always returns exactly `days` items (even with no data)
  4. get_quality_trend — correct avg when data exists (bytes + string key variants)
  5. get_quality_trend — handles brand_hash=None (global key path)
  6. get_quality_trend — avg rounds to 1 decimal, count=0 guard returns None

Deterministic: in-memory fake replaces Redis. No network, no LLM.
Run: python test_quality_analytics.py
"""

import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("DATABASE_URL", "postgresql://fake:fake@localhost/fake")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

from datetime import date as _date, timedelta as _td  # noqa: E402

import db.redis._base as _base  # noqa: E402

_passed = 0
_failed = 0


def _day(n: int = 1) -> str:
    """A date string `n` days before today, in get_quality_trend's isoformat.

    Trend fixtures MUST be relative to today: get_quality_trend() scans the real
    last-`days` window from date.today(), so hardcoded calendar dates silently
    age out of the window and rot the test. n in 1..6 always falls inside the
    default 7-day window.
    """
    return (_date.today() - _td(days=n)).isoformat()


def check(name, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


# ---------------------------------------------------------------------------
# Fake Redis: supports hgetall / hincrby / expire / pipeline
# ---------------------------------------------------------------------------

class _FakePipeline:
    def __init__(self, store):
        self._store = store
        self._cmds = []

    def hincrby(self, key, field, amount):
        self._cmds.append(("hincrby", key, field, amount))
        return self

    def expire(self, key, ttl):
        self._cmds.append(("expire", key, ttl))
        return self

    def execute(self):
        for cmd in self._cmds:
            if cmd[0] == "hincrby":
                _, key, field, amount = cmd
                if key not in self._store:
                    self._store[key] = {}
                cur = int(self._store[key].get(field, 0))
                self._store[key][field] = str(cur + amount)
        return [True] * len(self._cmds)


class _FakeRedis:
    def __init__(self):
        self.store = {}
        self._pipeline_calls = 0

    def hincrby(self, key, field, amount):
        if key not in self.store:
            self.store[key] = {}
        cur = int(self.store[key].get(field, 0))
        self.store[key][field] = str(cur + amount)

    def hgetall(self, key):
        return dict(self.store.get(key, {}))

    def expire(self, key, ttl):
        pass

    def pipeline(self):
        self._pipeline_calls += 1
        return _FakePipeline(self.store)


# ---------------------------------------------------------------------------
# Patch Redis client: must patch analytics module's local `_r` reference
# analytics.py does `from db.redis._base import _r` at load time, so we must
# patch the name in its module namespace, not in _base.
# ---------------------------------------------------------------------------

_fake = _FakeRedis()

import db.redis.analytics as _analytics_mod  # noqa: E402

_analytics_mod._r = lambda: _fake  # type: ignore

from db.redis.analytics import track_daily_quality, get_quality_trend  # noqa: E402

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

print("Quality Analytics — track_daily_quality + get_quality_trend\n")

# ── 1. pipeline() called (not sequential hincrby) ────────────────────────────
_fake.store.clear()
_fake._pipeline_calls = 0
track_daily_quality(brand_hash="abc123", score=80, day="2026-06-01")
check("track: uses pipeline() not sequential calls",
      _fake._pipeline_calls >= 1, f"pipeline_calls={_fake._pipeline_calls}")

# ── 2. dual-write: both global and brand-scoped keys written ─────────────────
check("track: global key written",
      "quality_daily:2026-06-01" in _fake.store,
      f"keys={list(_fake.store.keys())}")
check("track: brand-scoped key written",
      "quality_daily:abc123:2026-06-01" in _fake.store,
      f"keys={list(_fake.store.keys())}")

# ── 3. dual-write values are correct ─────────────────────────────────────────
gk = _fake.store.get("quality_daily:2026-06-01", {})
bk = _fake.store.get("quality_daily:abc123:2026-06-01", {})
check("track: global sum=80", int(gk.get("sum", 0)) == 80, f"sum={gk.get('sum')}")
check("track: global count=1", int(gk.get("count", 0)) == 1, f"count={gk.get('count')}")
check("track: brand sum=80", int(bk.get("sum", 0)) == 80, f"sum={bk.get('sum')}")
check("track: brand count=1", int(bk.get("count", 0)) == 1, f"count={bk.get('count')}")

# ── 4. accumulate: calling again adds to existing sum ────────────────────────
track_daily_quality(brand_hash="abc123", score=60, day="2026-06-01")
gk2 = _fake.store.get("quality_daily:2026-06-01", {})
check("track: sum accumulates (80+60=140)", int(gk2.get("sum", 0)) == 140, f"sum={gk2.get('sum')}")
check("track: count accumulates (1+1=2)", int(gk2.get("count", 0)) == 2, f"count={gk2.get('count')}")

# ── 5. brand_hash=None only writes global key (no brand-scoped key) ──────────
_fake.store.clear()
track_daily_quality(brand_hash=None, score=50, day="2026-06-02")
keys_written = list(_fake.store.keys())
check("track: no brand_hash → only global key",
      all(":" not in k.replace("quality_daily:", "", 1) for k in keys_written),
      f"keys={keys_written}")

# ── 6. get_quality_trend always returns exactly `days` items with no data ─────
_fake.store.clear()
trend = get_quality_trend(brand_hash="abc123", days=7)
check("trend: returns 7 items with no data", len(trend) == 7, f"len={len(trend)}")
check("trend: all avg=None with no data",
      all(t["avg"] is None for t in trend),
      f"avgs={[t['avg'] for t in trend]}")
check("trend: all items have 'date' key", all("date" in t for t in trend), "")

# ── 7. trend: correct avg when data present (string values, decode_responses=True sim) ──
_fake.store.clear()
_fake.store[f"quality_daily:abc123:{_day(2)}"] = {"sum": "150", "count": "3"}  # avg=50.0
_fake.store[f"quality_daily:abc123:{_day(1)}"] = {"sum": "200", "count": "4"}  # avg=50.0
trend2 = get_quality_trend(brand_hash="abc123", days=7)
check("trend: 7 items returned when some have data", len(trend2) == 7, f"len={len(trend2)}")
# Find entries with data
data_entries = [t for t in trend2 if t["avg"] is not None]
check("trend: exactly 2 entries have avg", len(data_entries) == 2, f"data_entries={len(data_entries)}")

# ── 8. trend: correct avg computation (rounds to 1 decimal) ──────────────────
_fake.store.clear()
_fake.store[f"quality_daily:abc123:{_day(1)}"] = {"sum": "221", "count": "3"}  # 221/3 = 73.666… → 73.7
trend3 = get_quality_trend(brand_hash="abc123", days=7)
entry = next((t for t in trend3 if t["date"] == _day(1)), None)
check("trend: avg rounds to 1 decimal",
      entry is not None and entry["avg"] == 73.7,
      f"got avg={entry['avg'] if entry else 'MISSING'}")

# ── 9. trend: bytes values handled safely (decode_responses=False simulation) ──
_fake.store.clear()
_fake.store[f"quality_daily:abc123:{_day(1)}"] = {b"sum": b"80", b"count": b"2"}  # avg=40.0
trend4 = get_quality_trend(brand_hash="abc123", days=7)
entry4 = next((t for t in trend4 if t["date"] == _day(1)), None)
check("trend: handles bytes keys/values without TypeError",
      entry4 is not None and entry4["avg"] == 40.0,
      f"got avg={entry4['avg'] if entry4 else 'MISSING'}")

# ── 10. trend: no brand_hash uses global key ────────────────────────────────
_fake.store.clear()
_fake.store[f"quality_daily:{_day(1)}"] = {"sum": "100", "count": "5"}  # avg=20.0
trend5 = get_quality_trend(brand_hash=None, days=7)
entry5 = next((t for t in trend5 if t["date"] == _day(1)), None)
check("trend: brand_hash=None reads global key",
      entry5 is not None and entry5["avg"] == 20.0,
      f"got avg={entry5['avg'] if entry5 else 'MISSING'}")

# ---------------------------------------------------------------------------
print(f"\n{_passed + _failed} checks — {_passed} PASS / {_failed} FAIL")
if _failed:
    sys.exit(1)
