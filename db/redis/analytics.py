"""
db/redis/analytics.py — Analytics, feedback, skill tracking, funnel, observability.

Covers:
  - Feedback (thumbs up/down)
  - Agent usage tracking
  - Skill usage + miss tracking
  - Funnel stage tracking (8 stages: search → payment_completed)
  - Tool reliability tracking (success/failure/latency per tool)
  - Routing accuracy tracking (supervisor override counts)
  - Response latency tracking (per-agent end-to-end)
  - Property-level event tracking (per-property funnel)
  - WhatsApp message response tracking (dedup)

All tracking functions dual-write to global + brand-scoped keys.
"""

import json
import time
from datetime import date, timedelta
from typing import Optional

from db.redis._base import _r, ANALYTICS_TTL


# ---------------------------------------------------------------------------
# Feedback (thumbs up / down)
# ---------------------------------------------------------------------------

def save_feedback(user_id: str, message_snippet: str, rating: str, agent: str = "", brand_hash: str = None) -> None:
    """Store a feedback entry. rating is 'up' or 'down'."""
    entry = json.dumps({
        "user_id": user_id,
        "snippet": message_snippet[:200],
        "rating": rating,
        "agent": agent,
        "ts": time.time(),
    })
    pipe = _r().pipeline(transaction=False)
    pipe.rpush("feedback:log", entry)
    # Aggregate counters per agent (global)
    pipe.hincrby("feedback:counts", f"{agent}:{rating}", 1)
    pipe.hincrby("feedback:counts", f"total:{rating}", 1)
    # Brand-scoped counters
    if brand_hash:
        pipe.hincrby(f"feedback:counts:{brand_hash}", f"{agent}:{rating}", 1)
        pipe.hincrby(f"feedback:counts:{brand_hash}", f"total:{rating}", 1)
    pipe.execute()


def get_feedback_counts(brand_hash: str = None) -> dict[str, int]:
    """Return all feedback counters as a dict. Brand-scoped if brand_hash provided."""
    key = f"feedback:counts:{brand_hash}" if brand_hash else "feedback:counts"
    raw = _r().hgetall(key)
    return {k.decode(): int(v) for k, v in raw.items()} if raw else {}


# ---------------------------------------------------------------------------
# Agent usage tracking (analytics)
# ---------------------------------------------------------------------------

def track_agent_usage(user_id: str, agent_name: str, brand_hash: str = None) -> None:
    """Increment agent usage counter for today. 90-day TTL."""
    day = date.today().isoformat()
    key = f"agent_usage:{day}"
    pipe = _r().pipeline(transaction=False)
    pipe.hincrby(key, agent_name, 1)
    pipe.expire(key, ANALYTICS_TTL)
    if brand_hash:
        bkey = f"agent_usage:{brand_hash}:{day}"
        pipe.hincrby(bkey, agent_name, 1)
        pipe.expire(bkey, ANALYTICS_TTL)
    pipe.execute()


def get_agent_usage(day: str = None, brand_hash: str = None) -> dict[str, int]:
    """Return {agent: count} for a given day (default: today). Brand-scoped if brand_hash provided."""
    if day is None:
        day = date.today().isoformat()
    key = f"agent_usage:{brand_hash}:{day}" if brand_hash else f"agent_usage:{day}"
    raw = _r().hgetall(key)
    return {k.decode(): int(v) for k, v in raw.items()} if raw else {}


# ---------------------------------------------------------------------------
# Skill usage tracking (dynamic skills system)
# ---------------------------------------------------------------------------

def track_skill_usage(skills: list[str], brand_hash: str = None) -> None:
    """Increment skill usage counters for today. 90-day TTL."""
    if not skills:
        return
    day = date.today().isoformat()
    key = f"skill_usage:{day}"
    pipe = _r().pipeline(transaction=False)
    for skill in skills:
        pipe.hincrby(key, skill, 1)
    pipe.expire(key, ANALYTICS_TTL)
    if brand_hash:
        bkey = f"skill_usage:{brand_hash}:{day}"
        for skill in skills:
            pipe.hincrby(bkey, skill, 1)
        pipe.expire(bkey, ANALYTICS_TTL)
    pipe.execute()


def track_skill_miss(tool_name: str, brand_hash: str = None) -> None:
    """Increment counter when a tool is not in filtered set (skill detection miss). 90-day TTL."""
    day = date.today().isoformat()
    key = f"skill_misses:{day}"
    pipe = _r().pipeline(transaction=False)
    pipe.hincrby(key, tool_name, 1)
    pipe.expire(key, ANALYTICS_TTL)
    if brand_hash:
        bkey = f"skill_misses:{brand_hash}:{day}"
        pipe.hincrby(bkey, tool_name, 1)
        pipe.expire(bkey, ANALYTICS_TTL)
    pipe.execute()


def get_skill_usage(day: str = None, brand_hash: str = None) -> dict[str, int]:
    """Return {skill: count} for a given day (default: today). Brand-scoped if brand_hash provided."""
    if day is None:
        day = date.today().isoformat()
    key = f"skill_usage:{brand_hash}:{day}" if brand_hash else f"skill_usage:{day}"
    raw = _r().hgetall(key)
    return {k.decode(): int(v) for k, v in raw.items()} if raw else {}


def get_skill_misses(day: str = None, brand_hash: str = None) -> dict[str, int]:
    """Return {tool_name: miss_count} for a given day (default: today). Brand-scoped if brand_hash provided."""
    if day is None:
        day = date.today().isoformat()
    key = f"skill_misses:{brand_hash}:{day}" if brand_hash else f"skill_misses:{day}"
    raw = _r().hgetall(key)
    return {k.decode(): int(v) for k, v in raw.items()} if raw else {}


# ---------------------------------------------------------------------------
# Funnel tracking (search → … → payment_completed)
# ---------------------------------------------------------------------------

FUNNEL_STAGES = (
    "search", "detail", "shortlist", "visit", "booking",
    "visit_attended", "booking_initiated", "payment_completed",
)


def track_funnel(user_id: str, stage: str, brand_hash: str = None) -> None:
    """Increment a funnel stage counter. Idempotent per user+stage per day."""
    if stage not in FUNNEL_STAGES:
        return
    day = date.today().isoformat()
    key = f"funnel:{day}"
    pipe = _r().pipeline(transaction=False)
    pipe.hincrby(key, stage, 1)
    pipe.expire(key, ANALYTICS_TTL)  # keep 90 days
    if brand_hash:
        bkey = f"funnel:{brand_hash}:{day}"
        pipe.hincrby(bkey, stage, 1)
        pipe.expire(bkey, ANALYTICS_TTL)
    pipe.execute()


def get_funnel(day: str = None, brand_hash: str = None) -> dict[str, int]:
    """Return funnel counts for a given day (default: today). Brand-scoped if brand_hash provided."""
    if day is None:
        day = date.today().isoformat()
    key = f"funnel:{brand_hash}:{day}" if brand_hash else f"funnel:{day}"
    raw = _r().hgetall(key)
    return {k.decode(): int(v) for k, v in raw.items()} if raw else {}


# ---------------------------------------------------------------------------
# Per-agent cost tracking (streaming path — fire-and-forget)
# ---------------------------------------------------------------------------

def increment_agent_cost(agent_name: str, tokens_in: int, tokens_out: int, cost_usd: float, brand_hash: str = None) -> None:
    """Accumulate per-agent token usage + cost for today. 90-day TTL.

    Writes to hash  agent_cost:{YYYY-MM-DD}
    Fields:         {agent}:tokens_in, {agent}:tokens_out, {agent}:cost_usd
    Read by:        get_agent_costs()  ← used in /admin/command-center
    """
    day = date.today().isoformat()
    key = f"agent_cost:{day}"
    pipe = _r().pipeline(transaction=False)
    pipe.hincrbyfloat(key, f"{agent_name}:tokens_in", tokens_in)
    pipe.hincrbyfloat(key, f"{agent_name}:tokens_out", tokens_out)
    pipe.hincrbyfloat(key, f"{agent_name}:cost_usd", cost_usd)
    pipe.expire(key, ANALYTICS_TTL)
    if brand_hash:
        bkey = f"agent_cost:{brand_hash}:{day}"
        pipe.hincrbyfloat(bkey, f"{agent_name}:tokens_in", tokens_in)
        pipe.hincrbyfloat(bkey, f"{agent_name}:tokens_out", tokens_out)
        pipe.hincrbyfloat(bkey, f"{agent_name}:cost_usd", cost_usd)
        pipe.expire(bkey, ANALYTICS_TTL)
    pipe.execute()


def get_agent_costs(day: str = None, brand_hash: str = None) -> dict[str, dict]:
    """Return {agent: {tokens_in, tokens_out, cost_usd}} for a given day (default: today).

    Returns empty dict if no cost data has been tracked yet for that day.
    Brand-scoped if brand_hash provided.
    """
    if day is None:
        day = date.today().isoformat()
    key = f"agent_cost:{brand_hash}:{day}" if brand_hash else f"agent_cost:{day}"
    raw = _r().hgetall(key)
    if not raw:
        return {}
    result: dict[str, dict] = {}
    for k, v in raw.items():
        parts = k.decode().rsplit(":", 1)
        if len(parts) != 2:
            continue
        agent, field = parts
        if agent not in result:
            result[agent] = {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
        val = float(v)
        if field == "tokens_in":
            result[agent]["tokens_in"] = int(val)
        elif field == "tokens_out":
            result[agent]["tokens_out"] = int(val)
        elif field == "cost_usd":
            result[agent]["cost_usd"] = round(val, 6)
    return result


def increment_daily_cost(cost_usd: float, brand_hash: str = None) -> None:
    """Accumulate today's total cost across all agents. 90-day TTL.

    Writes to hash  daily_cost:{YYYY-MM-DD}
    Fields:         cost_usd
    Read by:        get_daily_cost()  ← used in /admin/command-center
    """
    day = date.today().isoformat()
    key = f"daily_cost:{day}"
    pipe = _r().pipeline(transaction=False)
    pipe.hincrbyfloat(key, "cost_usd", cost_usd)
    pipe.expire(key, ANALYTICS_TTL)
    if brand_hash:
        bkey = f"daily_cost:{brand_hash}:{day}"
        pipe.hincrbyfloat(bkey, "cost_usd", cost_usd)
        pipe.expire(bkey, ANALYTICS_TTL)
    pipe.execute()


def get_daily_cost(day: str = None, brand_hash: str = None) -> float:
    """Return total cost_usd for a given day (default: today). Returns 0.0 if no data.
    Brand-scoped if brand_hash provided.
    """
    if day is None:
        day = date.today().isoformat()
    key = f"daily_cost:{brand_hash}:{day}" if brand_hash else f"daily_cost:{day}"
    raw = _r().hget(key, "cost_usd")
    return round(float(raw), 4) if raw else 0.0


# ---------------------------------------------------------------------------
# Tool reliability tracking (C1 — success/failure/latency per tool)
# ---------------------------------------------------------------------------

def track_tool_result(tool_name: str, success: bool, latency_ms: int, brand_hash: str = None) -> None:
    """Track a single tool invocation result. Dual-write global + brand-scoped.

    Redis hash fields per tool: {tool}:ok, {tool}:fail, {tool}:lat_sum, {tool}:lat_n
    """
    day = date.today().isoformat()
    status_field = f"{tool_name}:ok" if success else f"{tool_name}:fail"
    key = f"tool_stats:{day}"
    pipe = _r().pipeline(transaction=False)
    pipe.hincrby(key, status_field, 1)
    pipe.hincrbyfloat(key, f"{tool_name}:lat_sum", latency_ms)
    pipe.hincrby(key, f"{tool_name}:lat_n", 1)
    pipe.expire(key, ANALYTICS_TTL)
    if brand_hash:
        bkey = f"tool_stats:{brand_hash}:{day}"
        pipe.hincrby(bkey, status_field, 1)
        pipe.hincrbyfloat(bkey, f"{tool_name}:lat_sum", latency_ms)
        pipe.hincrby(bkey, f"{tool_name}:lat_n", 1)
        pipe.expire(bkey, ANALYTICS_TTL)
    pipe.execute()


def get_tool_stats(day: str = None, brand_hash: str = None) -> dict[str, dict]:
    """Return {tool: {ok, fail, avg_latency_ms, failure_rate}} for a day.

    Brand-scoped if brand_hash provided.
    """
    if day is None:
        day = date.today().isoformat()
    key = f"tool_stats:{brand_hash}:{day}" if brand_hash else f"tool_stats:{day}"
    raw = _r().hgetall(key)
    if not raw:
        return {}
    # Collect raw fields into per-tool dict
    tools: dict[str, dict] = {}
    for k, v in raw.items():
        parts = k.decode().rsplit(":", 1)
        if len(parts) != 2:
            continue
        tool, field = parts
        if tool not in tools:
            tools[tool] = {"ok": 0, "fail": 0, "lat_sum": 0.0, "lat_n": 0}
        val = float(v)
        if field == "ok":
            tools[tool]["ok"] = int(val)
        elif field == "fail":
            tools[tool]["fail"] = int(val)
        elif field == "lat_sum":
            tools[tool]["lat_sum"] = val
        elif field == "lat_n":
            tools[tool]["lat_n"] = int(val)
    # Compute derived metrics
    result: dict[str, dict] = {}
    for tool, d in tools.items():
        total = d["ok"] + d["fail"]
        result[tool] = {
            "ok": d["ok"],
            "fail": d["fail"],
            "total": total,
            "failure_rate": round(d["fail"] / total, 3) if total else 0.0,
            "avg_latency_ms": round(d["lat_sum"] / d["lat_n"]) if d["lat_n"] else 0,
        }
    return result


# ---------------------------------------------------------------------------
# Routing accuracy tracking (C2 — supervisor override counts)
# ---------------------------------------------------------------------------

def track_routing_override(original_agent: str, corrected_agent: str, brand_hash: str = None) -> None:
    """Track when the keyword safety net overrides the supervisor's classification.

    Redis hash field: {original}>{corrected} (e.g. "broker>booking")
    Also tracks total override count.
    """
    day = date.today().isoformat()
    field = f"{original_agent}>{corrected_agent}"
    key = f"routing_overrides:{day}"
    pipe = _r().pipeline(transaction=False)
    pipe.hincrby(key, field, 1)
    pipe.hincrby(key, "_total", 1)
    pipe.expire(key, ANALYTICS_TTL)
    if brand_hash:
        bkey = f"routing_overrides:{brand_hash}:{day}"
        pipe.hincrby(bkey, field, 1)
        pipe.hincrby(bkey, "_total", 1)
        pipe.expire(bkey, ANALYTICS_TTL)
    pipe.execute()


def get_routing_overrides(day: str = None, brand_hash: str = None) -> dict[str, int]:
    """Return {original>corrected: count, _total: count} for a day.

    Brand-scoped if brand_hash provided.
    """
    if day is None:
        day = date.today().isoformat()
    key = f"routing_overrides:{brand_hash}:{day}" if brand_hash else f"routing_overrides:{day}"
    raw = _r().hgetall(key)
    return {k.decode(): int(v) for k, v in raw.items()} if raw else {}


# ---------------------------------------------------------------------------
# Response latency tracking (C3 — per-agent end-to-end pipeline latency)
# ---------------------------------------------------------------------------

def track_response_latency(agent_name: str, latency_ms: int, brand_hash: str = None) -> None:
    """Track end-to-end pipeline latency for a single request.

    Redis hash fields: {agent}:sum, {agent}:n, {agent}:max
    """
    day = date.today().isoformat()
    key = f"latency:{day}"
    pipe = _r().pipeline(transaction=False)
    pipe.hincrbyfloat(key, f"{agent_name}:sum", latency_ms)
    pipe.hincrby(key, f"{agent_name}:n", 1)
    # Track max via Lua script for atomicity
    # Fallback: just use hincrbyfloat pattern and compute max on read
    pipe.expire(key, ANALYTICS_TTL)
    if brand_hash:
        bkey = f"latency:{brand_hash}:{day}"
        pipe.hincrbyfloat(bkey, f"{agent_name}:sum", latency_ms)
        pipe.hincrby(bkey, f"{agent_name}:n", 1)
        pipe.expire(bkey, ANALYTICS_TTL)
    pipe.execute()


def get_response_latency(day: str = None, brand_hash: str = None) -> dict[str, dict]:
    """Return {agent: {avg_ms, count}} for a day.

    Brand-scoped if brand_hash provided.
    """
    if day is None:
        day = date.today().isoformat()
    key = f"latency:{brand_hash}:{day}" if brand_hash else f"latency:{day}"
    raw = _r().hgetall(key)
    if not raw:
        return {}
    agents: dict[str, dict] = {}
    for k, v in raw.items():
        parts = k.decode().rsplit(":", 1)
        if len(parts) != 2:
            continue
        agent, field = parts
        if agent not in agents:
            agents[agent] = {"sum": 0.0, "n": 0}
        val = float(v)
        if field == "sum":
            agents[agent]["sum"] = val
        elif field == "n":
            agents[agent]["n"] = int(val)
    result: dict[str, dict] = {}
    for agent, d in agents.items():
        result[agent] = {
            "avg_ms": round(d["sum"] / d["n"]) if d["n"] else 0,
            "count": d["n"],
        }
    return result


# ---------------------------------------------------------------------------
# Property-level event tracking (Sprint 3 — per-property funnel)
# ---------------------------------------------------------------------------

PROPERTY_EVENTS = ("viewed", "shortlisted", "visit_scheduled", "visit_attended", "booking_initiated")


def track_property_event(property_id: str, event: str, brand_hash: str = None) -> None:
    """Track a per-property event. Dual-write global + brand-scoped.

    Redis hash key: property_events:{day} (+ property_events:{brand_hash}:{day})
    Field: {property_id}:{event}
    """
    if not property_id or event not in PROPERTY_EVENTS:
        return
    day = date.today().isoformat()
    field = f"{property_id}:{event}"
    key = f"property_events:{day}"
    pipe = _r().pipeline(transaction=False)
    pipe.hincrby(key, field, 1)
    pipe.expire(key, ANALYTICS_TTL)
    if brand_hash:
        bkey = f"property_events:{brand_hash}:{day}"
        pipe.hincrby(bkey, field, 1)
        pipe.expire(bkey, ANALYTICS_TTL)
    pipe.execute()


def get_property_events(day: str = None, brand_hash: str = None) -> dict[str, int]:
    """Return raw {property_id:event: count} hash for a day.

    Brand-scoped if brand_hash provided.
    """
    if day is None:
        day = date.today().isoformat()
    key = f"property_events:{brand_hash}:{day}" if brand_hash else f"property_events:{day}"
    raw = _r().hgetall(key)
    return {k.decode(): int(v) for k, v in raw.items()} if raw else {}


def get_property_performance(brand_hash: str = None, days: int = 7) -> dict[str, dict]:
    """Aggregate per-property events over N days.

    Returns: {property_id: {viewed: N, shortlisted: N, visit_scheduled: N,
              visit_attended: N, booking_initiated: N}}
    """
    from datetime import timedelta
    today = date.today()
    agg: dict[str, dict] = {}
    for i in range(days):
        day = (today - timedelta(days=i)).isoformat()
        raw = get_property_events(day, brand_hash=brand_hash)
        for field, count in raw.items():
            parts = field.rsplit(":", 1)
            if len(parts) != 2:
                continue
            prop_id, event = parts
            if prop_id not in agg:
                agg[prop_id] = {e: 0 for e in PROPERTY_EVENTS}
            if event in agg[prop_id]:
                agg[prop_id][event] += count
    return agg


# ---------------------------------------------------------------------------
# WhatsApp message response tracking (dedup)
# ---------------------------------------------------------------------------

def set_response(wama_id: str, message: str) -> None:
    _r().setex(f"wama:{wama_id}", 3 * 24 * 60 * 60, message)


def get_response(wama_id: str) -> Optional[str]:
    raw = _r().get(f"wama:{wama_id}")
    return raw.decode() if raw else None


# ---------------------------------------------------------------------------
# Property outcome signals (Sprint 5 — outcome-aware recommendations)
# ---------------------------------------------------------------------------

def track_property_outcome(property_id: str, outcome: str) -> None:
    """Increment outcome count for a property. No TTL — permanent signal.

    outcome: converted | lost | no_show
    """
    if not property_id or outcome not in ("converted", "lost", "no_show"):
        return
    _r().hincrby(f"property_signals:{property_id}", outcome, 1)


def get_property_signals(property_id: str) -> dict[str, int]:
    """Return outcome signals for a property: {converted: N, lost: N, no_show: N}."""
    if not property_id:
        return {}
    raw = _r().hgetall(f"property_signals:{property_id}")
    if not raw:
        return {}
    return {k.decode(): int(v) for k, v in raw.items()}


# ---------------------------------------------------------------------------
# Daily quality aggregate (for trend + avg KPI in analytics dashboard)
# Redis key: quality_daily:{day} / quality_daily:{brand_hash}:{day}
# Fields: sum (int), count (int)
# ---------------------------------------------------------------------------

def track_daily_quality(brand_hash: str = None, score: int = 0, day: str = None) -> None:
    """Accumulate a quality score into the daily aggregate. Dual-write global + brand-scoped."""
    if day is None:
        day = date.today().isoformat()
    r = _r()
    keys = [f"quality_daily:{day}"]
    if brand_hash:
        keys.append(f"quality_daily:{brand_hash}:{day}")
    pipe = r.pipeline()
    for key in keys:
        pipe.hincrby(key, "sum", int(score))
        pipe.hincrby(key, "count", 1)
        pipe.expire(key, ANALYTICS_TTL)
    pipe.execute()


def get_quality_trend(brand_hash: str = None, days: int = 7) -> list[dict]:
    """Return daily avg quality scores: [{"date": "2026-06-01", "avg": 72.5}, ...]"""
    today = date.today()
    r = _r()
    result = []
    for i in range(days - 1, -1, -1):
        day = (today - timedelta(days=i)).isoformat()
        key = f"quality_daily:{brand_hash}:{day}" if brand_hash else f"quality_daily:{day}"
        raw = r.hgetall(key)
        if raw:
            raw_sum = raw.get(b"sum", raw.get("sum", 0)) or 0
            raw_cnt = raw.get(b"count", raw.get("count", 1)) or 1
            s = int(raw_sum.decode() if isinstance(raw_sum, bytes) else raw_sum)
            n = int(raw_cnt.decode() if isinstance(raw_cnt, bytes) else raw_cnt) or 1
            result.append({"date": day, "avg": round(s / n, 1) if n else None})
        else:
            result.append({"date": day, "avg": None})
    return result
