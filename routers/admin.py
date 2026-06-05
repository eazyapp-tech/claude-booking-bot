"""
routers/admin.py — Admin and internal endpoints.

Routes:
  GET    /rate-limit/status
  GET    /admin/analytics
  GET    /admin/conversations
  GET    /admin/conversations/{uid}
  POST   /admin/conversations/{uid}/takeover
  POST   /admin/conversations/{uid}/resume
  POST   /admin/conversations/{uid}/message
  GET    /admin/command-center
  GET    /admin/leads
  GET    /admin/flags
  POST   /admin/flags
  POST   /admin/login
  GET    /admin/brand-config
  POST   /admin/brand-config
  POST   /admin/broadcast
  GET    /admin/properties
  GET    /admin/properties/{prop_id}/documents
  POST   /admin/properties/{prop_id}/documents
  DELETE /admin/properties/{prop_id}/documents/{doc_id}
  POST   /admin/backfill-brands
  POST   /admin/backfill-message-brand-hash
  POST   /admin/leads/{uid}/outcome
"""

import asyncio
import json as _json_module
import time as _time
import traceback
import uuid as uuid_lib
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from channels.whatsapp import send_text
from config import settings
from core.auth import CHAT_BASE_URL, require_admin_brand_key, require_brand_api_key, verify_api_key
from core.log import get_logger
from db import postgres as pg
from db.redis_store import (
    _r,
    clear_human_mode,
    get_active_users,
    get_agent_usage,
    get_brand_active_users,
    get_brand_active_users_count,
    get_brand_config,
    get_brand_config_by_hash,
    get_conversation,
    get_feedback_counts,
    get_funnel,
    get_human_mode,
    get_last_agent,
    get_preferences,
    get_session_cost,
    get_skill_misses,
    get_skill_usage,
    get_user_brand,
    get_user_memory,
    get_user_phone,
    save_conversation,
    set_brand_config,
    set_human_mode,
    get_agent_costs,
    get_daily_cost,
    get_tool_stats,
    get_routing_overrides,
    get_response_latency,
    get_property_performance,
    track_property_event,
    track_funnel,
    update_user_memory,
    get_quality_trend,
)

logger = get_logger("routers.admin")

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _r_score(uid: str):
    """Get uid score from active_users sorted set."""
    try:
        score = _r().zscore("active_users", uid)
        return float(score) if score is not None else None
    except Exception:
        return None


def _require_ownership(uid: str, brand_hash: str) -> None:
    """Raise 404 if `uid` does not belong to the given brand.

    Uses a lenient check: if the user has no brand tag yet (legacy user),
    the request is allowed. This avoids breaking admin operations for
    users who haven't been backfilled yet.
    """
    user_brand = get_user_brand(uid)
    if not user_brand or user_brand != brand_hash:
        raise HTTPException(status_code=404, detail="Conversation not found")


# ---------------------------------------------------------------------------
# Rate-limit status
# ---------------------------------------------------------------------------

@router.get("/rate-limit/status", dependencies=[Depends(verify_api_key)])
async def rate_limit_status(user_id: str):
    """Show current rate-limit usage for a given user."""
    from core.rate_limiter import get_rate_limit_status
    return get_rate_limit_status(user_id)


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

@router.get("/admin/analytics")
async def admin_analytics(days: int = 7, brand_hash: str = Depends(require_admin_brand_key)):
    """Return aggregated analytics data for the dashboard.

    Query params:
      days: integer number of days to look back (default 7, max 90)
    """
    today = date.today()
    days = max(1, min(days, 90))

    # --- Funnel: aggregate across date range (brand-scoped) ---
    funnel_totals: dict[str, int] = {}
    for i in range(days):
        day = (today - timedelta(days=i)).isoformat()
        for stage, count in get_funnel(day, brand_hash=brand_hash).items():
            funnel_totals[stage] = funnel_totals.get(stage, 0) + count

    # --- Feedback (brand-scoped) ---
    feedback = get_feedback_counts(brand_hash=brand_hash)

    # --- Message volume (from Postgres, brand-scoped) ---
    message_volume: dict[str, int] = {}
    try:
        start_date = today - timedelta(days=days - 1)
        message_volume = await pg.get_message_volume(
            start_date.isoformat(), today.isoformat(), brand_hash=brand_hash
        )
    except Exception as e:
        logger.warning("get_message_volume failed: %s", e)

    # --- Agent distribution: aggregate across date range (brand-scoped) ---
    agent_totals: dict[str, int] = {}
    for i in range(days):
        day = (today - timedelta(days=i)).isoformat()
        for agent, count in get_agent_usage(day, brand_hash=brand_hash).items():
            agent_totals[agent] = agent_totals.get(agent, 0) + count

    # --- Skill usage: aggregate across date range (brand-scoped) ---
    skill_totals: dict[str, int] = {}
    skill_miss_totals: dict[str, int] = {}
    for i in range(days):
        day = (today - timedelta(days=i)).isoformat()
        for skill, count in get_skill_usage(day, brand_hash=brand_hash).items():
            skill_totals[skill] = skill_totals.get(skill, 0) + count
        for tool, count in get_skill_misses(day, brand_hash=brand_hash).items():
            skill_miss_totals[tool] = skill_miss_totals.get(tool, 0) + count

    # --- Rate limit status (current snapshot) ---
    from core.rate_limiter import get_rate_limit_status
    rate_limits = {}
    try:
        rate_limits = get_rate_limit_status("__global__")
    except Exception as e:
        logger.warning("rate limit status fetch failed: %s", e)

    # --- Derived KPIs — brand-scoped user count & cost ---
    total_messages = sum(message_volume.values())
    active_users_count = get_brand_active_users_count(brand_hash)
    visits_booked = funnel_totals.get("visit", 0)
    new_leads = funnel_totals.get("search", 0)  # users who ran a property search (top-of-funnel)

    # Chronologically sorted daily message counts for the chart
    daily = [{"date": d, "count": c} for d, c in sorted(message_volume.items())]

    # Total cost: sum session_cost across brand's tracked users (best-effort)
    total_cost_usd = 0.0
    try:
        brand_uids = get_brand_active_users(brand_hash, offset=0, limit=500)
        for uid in brand_uids:
            total_cost_usd += get_session_cost(uid).get("cost_usd", 0.0)
    except Exception as e:
        logger.warning("cost aggregation failed: %s", e)

    # --- Tool reliability: aggregate across date range (brand-scoped) ---
    tool_stats_agg: dict[str, dict] = {}
    for i in range(days):
        day = (today - timedelta(days=i)).isoformat()
        for tool, stats in get_tool_stats(day, brand_hash=brand_hash).items():
            if tool not in tool_stats_agg:
                tool_stats_agg[tool] = {"ok": 0, "fail": 0, "lat_sum": 0, "lat_n": 0}
            tool_stats_agg[tool]["ok"] += stats.get("ok", 0)
            tool_stats_agg[tool]["fail"] += stats.get("fail", 0)
            tool_stats_agg[tool]["lat_sum"] += stats.get("ok", 0) * stats.get("avg_latency_ms", 0)
            tool_stats_agg[tool]["lat_n"] += stats.get("ok", 0) + stats.get("fail", 0)
    # Compute derived fields
    for tool, s in tool_stats_agg.items():
        total = s["ok"] + s["fail"]
        s["total"] = total
        s["failure_rate"] = round(s["fail"] / total, 3) if total else 0
        s["avg_latency_ms"] = round(s["lat_sum"] / s["lat_n"]) if s["lat_n"] else 0
        del s["lat_sum"]
        del s["lat_n"]

    # --- Routing overrides: aggregate across date range ---
    routing_agg: dict[str, int] = {}
    for i in range(days):
        day = (today - timedelta(days=i)).isoformat()
        for key, count in get_routing_overrides(day, brand_hash=brand_hash).items():
            routing_agg[key] = routing_agg.get(key, 0) + count
    total_routed = sum(agent_totals.values())
    override_total = routing_agg.get("_total", 0)
    routing_accuracy_pct = round((1 - override_total / total_routed) * 100, 1) if total_routed else 100.0

    # --- Response latency: aggregate across date range ---
    latency_agg: dict[str, dict] = {}
    for i in range(days):
        day = (today - timedelta(days=i)).isoformat()
        for agent, lat in get_response_latency(day, brand_hash=brand_hash).items():
            if agent not in latency_agg:
                latency_agg[agent] = {"sum_ms": 0, "count": 0}
            latency_agg[agent]["sum_ms"] += lat.get("avg_ms", 0) * lat.get("count", 0)
            latency_agg[agent]["count"] += lat.get("count", 0)
    for agent, s in latency_agg.items():
        s["avg_ms"] = round(s["sum_ms"] / s["count"]) if s["count"] else 0
        del s["sum_ms"]

    # --- Cost-per-conversion (INR, 1 USD = 95 INR) ---
    USD_TO_INR = 95
    total_cost_inr = round(total_cost_usd * USD_TO_INR, 2)
    cost_per_visit_inr = round((total_cost_usd / visits_booked) * USD_TO_INR, 2) if visits_booked else None
    bookings = funnel_totals.get("booking", 0)
    cost_per_booking_inr = round((total_cost_usd / bookings) * USD_TO_INR, 2) if bookings else None

    # --- Property performance: aggregate across date range (Sprint 3) ---
    property_perf = {}
    try:
        property_perf = get_property_performance(brand_hash=brand_hash, days=days)
    except Exception as e:
        logger.warning("property performance aggregation failed: %s", e)

    # --- Quality distribution + avg score: scan brand users (Sprint 4) ---
    quality_distribution = {"0-25": 0, "25-50": 0, "50-75": 0, "75-100": 0}
    avg_quality_score = None
    try:
        from db.redis.quality import get_conversation_quality
        q_uids = get_brand_active_users(brand_hash, offset=0, limit=500)
        q_sum, q_count = 0, 0
        for q_uid in q_uids:
            qd = get_conversation_quality(q_uid)
            qs = qd.get("score")
            if qs is not None:
                q_sum += qs
                q_count += 1
                if qs < 25:
                    quality_distribution["0-25"] += 1
                elif qs < 50:
                    quality_distribution["25-50"] += 1
                elif qs < 75:
                    quality_distribution["50-75"] += 1
                else:
                    quality_distribution["75-100"] += 1
        if q_count:
            avg_quality_score = round(q_sum / q_count, 1)
    except Exception as e:
        logger.warning("quality distribution scan failed: %s", e)

    # --- Error summary (Sprint 4, PostgreSQL) ---
    error_summary = {}
    try:
        error_summary = await pg.get_error_summary(brand_hash=brand_hash, days=days)
    except Exception as e:
        logger.warning("error summary failed: %s", e)

    # --- Derived KPIs ---
    conversion_rate = round((visits_booked / new_leads) * 100, 1) if new_leads else None

    # --- Quality trend (7-day sparkline) ---
    quality_trend = []
    try:
        quality_trend = get_quality_trend(brand_hash=brand_hash, days=7)
    except Exception as e:
        logger.warning("quality trend failed: %s", e)

    # --- Cost spike detection: flag if today > 2× 7-day avg ---
    cost_spike = None
    try:
        today_cost = get_daily_cost(day=today.isoformat(), brand_hash=brand_hash) or 0.0
        past_costs = [
            get_daily_cost(day=(today - timedelta(days=i)).isoformat(), brand_hash=brand_hash) or 0.0
            for i in range(1, 8)
        ]
        avg_7d = sum(past_costs) / 7
        today_inr = round(today_cost * USD_TO_INR, 2)
        avg_7d_inr = round(avg_7d * USD_TO_INR, 2)
        if avg_7d > 0 and today_cost > avg_7d * 2:
            cost_spike = {"today_inr": today_inr, "avg_7d_inr": avg_7d_inr}
    except Exception as e:
        logger.warning("cost spike check failed: %s", e)

    return {
        # KPI cards
        "total_messages": total_messages,
        "active_users": active_users_count,
        "visits_booked": visits_booked,
        "new_leads": new_leads,
        "total_cost_inr": total_cost_inr,
        "cost_per_visit_inr": cost_per_visit_inr,
        "cost_per_booking_inr": cost_per_booking_inr,
        # Chart data
        "daily": daily,
        "agents": agent_totals,
        "skills": {"usage": skill_totals, "misses": skill_miss_totals},
        # Observability (Sprint 1)
        "tool_stats": tool_stats_agg,
        "routing": {"overrides": routing_agg, "accuracy_pct": routing_accuracy_pct},
        "latency": latency_agg,
        # Property analytics (Sprint 3)
        "property_performance": property_perf,
        # Quality + errors (Sprint 4)
        "quality_distribution": quality_distribution,
        "error_summary": error_summary,
        # Analytics improvements
        "avg_quality_score": avg_quality_score,
        "conversion_rate": conversion_rate,
        "quality_trend": quality_trend,
        "cost_spike": cost_spike,
        # Extended data (kept for backward compat)
        "funnel": funnel_totals,
        "feedback": feedback,
        "messages": daily,
        "rate_limits": rate_limits,
        "meta": {
            "days": days,
            "generated_at": datetime.utcnow().isoformat(),
        },
    }


# ---------------------------------------------------------------------------
# Conversation browser
# ---------------------------------------------------------------------------

@router.get("/admin/conversations")
async def admin_conversations(
    offset: int = 0,
    limit: int = 50,
    filter: str = "",
    brand_hash: str = Depends(require_admin_brand_key),
):
    """Return paginated list of users sorted by most recent activity.

    Each entry contains enough metadata to render a conversation list row:
    uid, name, phone, last_message preview, last_agent, lead_score, human_mode,
    attention_flags.

    Query params:
      filter: "needs_attention" — return only users with non-empty attention flags
    """
    from core.attention import get_attention_flags
    from db.redis.quality import get_conversation_quality

    # Pull larger batch when filtering (need to scan then paginate)
    fetch_limit = max(limit * 4, 200) if filter == "needs_attention" else limit
    total_brand = get_brand_active_users_count(brand_hash)
    uids = get_brand_active_users(brand_hash, offset=0 if filter else offset, limit=fetch_limit if filter else limit)

    rows = []
    for uid in uids:
        mem = get_user_memory(uid)
        conv = get_conversation(uid)
        human_mode = get_human_mode(uid, brand_hash=brand_hash)
        attention_flags = get_attention_flags(uid)

        # Apply filter: skip users without attention flags
        if filter == "needs_attention" and not attention_flags:
            continue

        # Last message preview (last non-empty text message)
        last_msg = ""
        last_role = ""
        for msg in reversed(conv):
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                last_msg = content[:120]
                last_role = msg.get("role", "")
                break

        cost_data = get_session_cost(uid)
        quality_data = get_conversation_quality(uid)
        rows.append({
            "uid": uid,
            "name": mem.get("profile_name") or mem.get("name") or "",
            "phone": get_user_phone(uid) or "",
            "last_message": last_msg,
            "last_role": last_role,
            "last_agent": get_last_agent(uid) or "default",
            "lead_score": mem.get("lead_score", 0),
            "funnel_stage": mem.get("funnel_max", ""),
            "last_seen": mem.get("last_seen", ""),
            "human_mode": human_mode,
            "message_count": len(conv),
            "cost_inr": round(cost_data.get("cost_usd", 0.0) * 95, 2),
            "attention_flags": attention_flags,
            "quality_score": quality_data.get("score"),
        })

    # When filtering, total is the filtered count and we paginate in-memory
    if filter == "needs_attention":
        total = len(rows)
        page = rows[offset: offset + limit]
    else:
        total = total_brand
        page = rows

    return {
        "conversations": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": (offset + limit) < total,
    }


@router.get("/admin/conversations/{uid}")
async def admin_conversation_detail(uid: str, brand_hash: str = Depends(require_admin_brand_key)):
    """Return full conversation thread + user context for a given uid."""
    _require_ownership(uid, brand_hash)
    conv = get_conversation(uid)
    mem = get_user_memory(uid)
    prefs = get_preferences(uid)
    cost = get_session_cost(uid)
    human_mode = get_human_mode(uid, brand_hash=brand_hash)
    last_agent = get_last_agent(uid) or "default"

    # Follow-up state (Sprint 2)
    try:
        from core.followup import get_followup_state
        followup_state = get_followup_state(uid)
    except Exception:
        followup_state = []

    # Attention flags (Sprint 3)
    try:
        from core.attention import get_attention_flags
        attention_flags = get_attention_flags(uid)
    except Exception:
        attention_flags = []

    # Conversation quality (Sprint 4)
    try:
        from db.redis.quality import get_conversation_quality
        quality = get_conversation_quality(uid)
    except Exception:
        quality = {}

    return {
        "uid": uid,
        "messages": conv,
        "memory": mem,
        "preferences": prefs,
        "cost": cost,
        "human_mode": human_mode,
        "last_agent": last_agent,
        "followup_state": followup_state,
        "attention_flags": attention_flags,
        "quality": quality,
    }


class AdminMessageRequest(BaseModel):
    message: str
    platform: str = "whatsapp"  # "whatsapp" | "web"


@router.post("/admin/conversations/{uid}/takeover")
async def admin_takeover(uid: str, brand_hash: str = Depends(require_admin_brand_key)):
    """Activate human takeover — AI stops responding for this user."""
    _require_ownership(uid, brand_hash)
    set_human_mode(uid, brand_hash=brand_hash)
    return {"ok": True}


@router.post("/admin/conversations/{uid}/resume")
async def admin_resume(uid: str, brand_hash: str = Depends(require_admin_brand_key)):
    """Deactivate human takeover — AI resumes handling this user."""
    _require_ownership(uid, brand_hash)
    clear_human_mode(uid, brand_hash=brand_hash)
    return {"ok": True}


@router.post("/admin/conversations/{uid}/message")
async def admin_send_message(uid: str, req: AdminMessageRequest, brand_hash: str = Depends(require_admin_brand_key)):
    """Send a manual message as the admin (human operator).

    The message is delivered via WhatsApp and appended to the conversation
    history with source="human" so the thread view can style it distinctly.

    After sending, human_mode is automatically cleared so the AI resumes
    on the user's next reply. This prevents the silent-bot bug where the
    admin sends one message and forgets to click "Resume AI", leaving every
    subsequent user message unanswered.
    """
    _require_ownership(uid, brand_hash)
    sent_at = datetime.utcnow().isoformat()

    # Deliver via WhatsApp if platform is whatsapp
    if req.platform == "whatsapp":
        await send_text(uid, req.message)

    # Append to conversation history for thread view
    conv = get_conversation(uid)
    conv.append({
        "role": "assistant",
        "content": req.message,
        "source": "human",
        "sent_at": sent_at,
    })
    save_conversation(uid, conv, brand_hash=brand_hash)

    # Auto-resume AI after admin message — prevents silent-bot if admin forgets
    # to click "Resume AI". Admin can re-take-over by calling /takeover again.
    clear_human_mode(uid, brand_hash=brand_hash)

    return {"ok": True, "sent_at": sent_at}


# ---------------------------------------------------------------------------
# Command center
# ---------------------------------------------------------------------------

@router.get("/admin/command-center")
async def admin_command_center(brand_hash: str = Depends(require_admin_brand_key)):
    """Today's at-a-glance stats for the command center home screen."""
    today = date.today().isoformat()
    day_funnel = get_funnel(today, brand_hash=brand_hash)
    day_agents = get_agent_usage(today, brand_hash=brand_hash)

    # Count conversations currently in human mode (brand-scoped)
    brand_uids = get_brand_active_users(brand_hash, offset=0, limit=200)
    human_count = sum(1 for uid in brand_uids if get_human_mode(uid, brand_hash=brand_hash))

    # Message count for today from Postgres (best-effort)
    msg_count = 0
    try:
        vol = await pg.get_message_volume(today, today, brand_hash=brand_hash)
        msg_count = vol.get(today, 0)
    except Exception:
        pass

    return {
        "today": {
            "messages":         msg_count,
            "new_leads":        day_funnel.get("search", 0),
            "visits_scheduled": day_funnel.get("visit", 0),
            "booked":           day_funnel.get("booking", 0),
        },
        "funnel":               day_funnel,
        "agents":               day_agents,
        "active_conversations": get_brand_active_users_count(brand_hash),
        "human_mode_count":     human_count,
        # Cost fields (INR, 1 USD = 95 INR)
        "cost_inr_today":       round(get_daily_cost(today, brand_hash=brand_hash) * 95, 2),
        "agents_cost":          get_agent_costs(today, brand_hash=brand_hash),
        # Cost-per-conversion (today, INR)
        "cost_per_visit_inr":   round(get_daily_cost(today, brand_hash=brand_hash) * 95 / day_funnel.get("visit", 1), 2) if day_funnel.get("visit") else None,
        "cost_per_booking_inr": round(get_daily_cost(today, brand_hash=brand_hash) * 95 / day_funnel.get("booking", 1), 2) if day_funnel.get("booking") else None,
        "generated_at":         datetime.utcnow().isoformat(),
    }


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------

def _lead_row(uid: str) -> dict:
    """Build the full lead dict for a single uid. DRY helper used by both endpoints."""
    mem   = get_user_memory(uid)
    prefs = get_preferences(uid)
    phone = get_user_phone(uid) or ""
    name  = mem.get("profile_name") or mem.get("name") or ""
    cost  = get_session_cost(uid)

    # Budget: prefer structured prefs, fall back to memory strings
    budget_min = prefs.get("min_budget")
    budget_max = prefs.get("max_budget") or mem.get("budget_max") or mem.get("budget")

    # Follow-up state (best-effort)
    followup_step = ""
    try:
        from core.followup import get_followup_state
        fs = get_followup_state(uid)
        # fs is a list of state dicts; surface the step of the most recent active entry
        active = [s for s in (fs or []) if s.get("status") not in ("done", "skipped")]
        if active:
            followup_step = active[-1].get("step", "")
        elif fs:
            followup_step = fs[-1].get("step", "")
    except Exception:
        followup_step = mem.get("followup_step", "")

    shortlisted = mem.get("properties_shortlisted") or []

    return {
        # Identity
        "uid":              uid,
        "name":             name,
        "phone":            phone,
        "phone_collected":  bool(mem.get("phone_collected", False)),
        "persona":          mem.get("persona") or "",
        # Funnel
        "stage":            mem.get("funnel_max") or "",
        "first_seen":       mem.get("first_seen") or "",
        "last_seen":        mem.get("last_seen") or "",
        "session_count":    int(mem.get("session_count") or 0),
        # Engagement
        "viewed_count":      len(mem.get("properties_viewed") or []),
        "shortlisted_count": len(shortlisted),
        "properties_shortlisted": shortlisted,
        "visits_count":      len(mem.get("visits_scheduled") or []),
        # Intent signals
        "deal_breakers":    mem.get("deal_breakers") or [],
        "must_haves":       mem.get("must_haves") or [],
        "lead_score":       int(mem.get("lead_score") or 0),
        # Location & Budget
        "location_pref":    mem.get("location_preference") or mem.get("location_pref") or "",
        "budget_min":       budget_min,
        "budget_max":       budget_max,
        "budget":           mem.get("budget") or "",
        # Preferences
        "property_type":    prefs.get("property_type") or "",
        "amenities":        prefs.get("amenities") or prefs.get("must_have_amenities") or [],
        "sharing_types":    prefs.get("sharing_types_enabled") or [],
        # Cost
        "cost_inr":         round(float(cost.get("cost_usd") or 0.0) * 95, 2),
        # Move-in intent
        "move_in_date":     mem.get("move_in_date") or "",
        # Follow-up state machine
        "followup_step":    followup_step,
        # Outcome (Sprint 3)
        "lead_outcome":     mem.get("lead_outcome") or "",
        "outcome_notes":    mem.get("outcome_notes") or "",
        "outcome_at":       mem.get("outcome_at") or "",
    }


@router.get("/admin/leads")
async def admin_leads(
    stage: str = "",
    area: str = "",
    budget_max: int = 0,
    days_since_active: int = 0,
    outcome: str = "",
    q: str = "",
    offset: int = 0,
    limit: int = 25,
    brand_hash: str = Depends(require_admin_brand_key),
):
    """Return paginated, filterable lead list sorted by recency."""
    cutoff_ts = _time.time() - (days_since_active * 86400) if days_since_active else 0

    # Fetch all brand UIDs so filters don't silently drop users outside an artificial ceiling
    total_brand_count = get_brand_active_users_count(brand_hash)
    uids = get_brand_active_users(brand_hash, offset=0, limit=total_brand_count) if total_brand_count else []

    rows = []
    for uid in uids:
        mem = get_user_memory(uid)

        # Age filter
        if cutoff_ts:
            score = _r_score(uid)
            if score and score < cutoff_ts:
                continue

        # Stage filter
        if stage and mem.get("funnel_max") != stage:
            continue

        # Outcome filter (Sprint 3)
        if outcome and mem.get("lead_outcome", "") != outcome:
            continue

        # Area filter
        loc = (mem.get("location_preference") or mem.get("location_pref") or "").lower()
        if area and area.lower() not in loc:
            continue

        # Budget filter
        budget_val = mem.get("budget_max") or mem.get("budget")
        if budget_max and budget_val:
            try:
                if int(budget_val) > budget_max:
                    continue
            except (ValueError, TypeError):
                pass

        name  = mem.get("profile_name") or mem.get("name") or ""
        phone = get_user_phone(uid) or ""

        # Full-text search across name + phone
        if q and q.lower() not in name.lower() and q.lower() not in phone:
            continue

        rows.append(_lead_row(uid))

    total = len(rows)
    page  = rows[offset: offset + limit]

    # Persist enriched snapshot to PostgreSQL (fire-and-forget, brand-scoped)
    if rows:
        asyncio.create_task(pg.upsert_leads(rows, brand_hash=brand_hash))

    return {"leads": page, "total": total, "offset": offset, "limit": limit}


@router.get("/admin/leads/{uid}")
async def admin_lead_detail(uid: str, brand_hash: str = Depends(require_admin_brand_key)):
    """Return the full 25-field profile for a single lead."""
    _require_ownership(uid, brand_hash)
    row = _lead_row(uid)
    asyncio.create_task(pg.upsert_leads([row], brand_hash=brand_hash))
    return row


class LeadOutcomeRequest(BaseModel):
    outcome: str  # "converted" | "lost" | "no_show" | "in_progress"
    notes: str = ""


@router.post("/admin/leads/{uid}/outcome")
async def admin_set_lead_outcome(uid: str, body: LeadOutcomeRequest, brand_hash: str = Depends(require_admin_brand_key)):
    """Mark final outcome for a lead.

    Outcomes: converted, lost, no_show, in_progress.
    Side effects:
      - converted: fires payment_completed funnel event + booking_initiated property event
      - no_show: logs deal-breaker signal
    """
    _require_ownership(uid, brand_hash)

    valid_outcomes = {"converted", "lost", "no_show", "in_progress"}
    if body.outcome not in valid_outcomes:
        raise HTTPException(status_code=400, detail=f"Invalid outcome. Must be one of: {', '.join(sorted(valid_outcomes))}")

    # Update user memory with outcome
    update_user_memory(
        uid,
        lead_outcome=body.outcome,
        outcome_notes=body.notes,
        outcome_at=datetime.utcnow().isoformat(),
    )

    # Side effects
    if body.outcome == "converted":
        try:
            track_funnel(uid, "payment_completed", brand_hash=brand_hash)
            # Track on the property they most recently interacted with
            mem = get_user_memory(uid)
            visited = mem.get("visits_attended", []) or mem.get("properties_visited", [])
            if visited:
                track_property_event(visited[-1], "booking_initiated", brand_hash=brand_hash)
                from db.redis.analytics import track_property_outcome
                track_property_outcome(visited[-1], "converted")
        except Exception:
            pass

    elif body.outcome == "no_show":
        try:
            from db.redis_store import add_deal_breaker
            add_deal_breaker(uid, f"No-show (admin marked): {body.notes}" if body.notes else "No-show (admin marked)")
            # Track no_show on the property
            mem = get_user_memory(uid)
            visited = mem.get("visits_attended", []) or mem.get("properties_visited", [])
            if visited:
                from db.redis.analytics import track_property_outcome
                track_property_outcome(visited[-1], "no_show")
        except Exception:
            pass

    elif body.outcome == "lost":
        try:
            mem = get_user_memory(uid)
            visited = mem.get("visits_attended", []) or mem.get("properties_visited", [])
            if visited:
                from db.redis.analytics import track_property_outcome
                track_property_outcome(visited[-1], "lost")
        except Exception:
            pass

    return {"ok": True, "uid": uid, "outcome": body.outcome}


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

@router.get("/admin/flags")
async def admin_get_flags(brand_hash: str = Depends(require_admin_brand_key)):
    """Return effective feature flag states (per-brand overrides merged over global defaults)."""
    from db.redis_store import get_effective_flags
    flags = get_effective_flags(brand_hash)
    flags["WEB_SEARCH_ENABLED"] = bool(settings.TAVILY_API_KEY)  # always global, read-only
    return flags


# Mutable flags that can be toggled per-brand (persisted in Redis).
_MUTABLE_FLAGS = {"DYNAMIC_SKILLS_ENABLED", "KYC_ENABLED", "PAYMENT_REQUIRED", "SEMANTIC_KB_ENABLED"}


@router.post("/admin/flags")
async def admin_set_flags(request: Request, brand_hash: str = Depends(require_admin_brand_key)):
    """Update per-brand feature flags (persisted in Redis per brand).

    Accepts both payload formats:
      - { "key": "FLAG_NAME", "value": bool }  (frontend sends this)
      - { "FLAG_NAME": bool }                  (direct API usage)
    """
    from db.redis_store import set_brand_flag, get_effective_flags
    body = await request.json()

    # Support both: { KEY: value } and { key: "KEY", value: bool }
    if "key" in body and "value" in body:
        updates = {body["key"]: body["value"]}
    else:
        updates = body

    changed = {}
    for flag in _MUTABLE_FLAGS:
        if flag in updates and updates[flag] is not None:
            set_brand_flag(brand_hash, flag, bool(updates[flag]))
            changed[flag] = bool(updates[flag])

    # Return all effective flags so frontend reflects the merged state
    return {"ok": True, "changed": changed, "effective": get_effective_flags(brand_hash)}


# ---------------------------------------------------------------------------
# Admin login (ID + password → brand API key). No auth dependency — this IS
# the gate. Credentials validated server-side; the raw key never ships in the
# frontend bundle. See core/admin_login.py.
# ---------------------------------------------------------------------------

def _client_ip(request: Request) -> str:
    """Real client IP for rate-limiting. Thin adapter over core.accounts.trusted_client_ip.

    This service runs on Render (single trusted proxy), which appends the real
    client IP as the LAST X-Forwarded-For hop. Leading hops are client-forgeable,
    so the resolver takes the last hop, never the first. (Azure is RentOk's stack,
    not this prototype's — there is no X-Azure-ClientIP here.)
    """
    from core.accounts import trusted_client_ip
    peer = request.client.host if request.client else None
    return trusted_client_ip(request.headers.get("X-Forwarded-For"), peer)


@router.post("/admin/login")
async def admin_login(request: Request):
    from core.admin_login import (
        verify_admin_login, is_throttled, _record_failure, _clear_failures,
    )
    from core.accounts import (
        verify_login, login_ip_throttled, record_login_ip_failure, clear_login_ip_failures,
    )

    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password are required")

    # Two throttles: per-username (targeted guessing) AND per-IP (credential
    # stuffing that rotates the email so the username counter never trips).
    ip = _client_ip(request)
    if is_throttled(username) or login_ip_throttled(ip):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")

    # Self-serve accounts first (username == email). Only fall back to the legacy
    # env credential when the account is genuinely not found / wrong password
    # ("invalid") — a correct account whose brand is mis-provisioned must surface
    # as "misconfigured", not be masked behind the legacy 401.
    api_key, reason = verify_login(username, password)
    consulted_legacy = False
    if reason == "invalid":
        consulted_legacy = True
        api_key, reason = verify_admin_login(username, password)

    if reason == "ok":
        _clear_failures(username)
        clear_login_ip_failures(ip)
        return {"api_key": api_key}
    if reason == "unconfigured":
        raise HTTPException(status_code=503, detail="Admin login is not configured")
    if reason == "throttled":
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
    if reason == "misconfigured":
        raise HTTPException(status_code=503, detail="Admin login misconfigured")
    # legacy verify_admin_login records its own per-username failure; only count the
    # account path there. The per-IP counter is recorded for every failure.
    if not consulted_legacy:
        _record_failure(username)
    record_login_ip_failure(ip)
    raise HTTPException(status_code=401, detail="Invalid username or password")


@router.post("/admin/signup")
async def admin_signup(request: Request):
    from core.accounts import signup, send_verification_email, check_signup_rate

    client_ip = _client_ip(request)
    if not check_signup_rate(client_ip):
        raise HTTPException(status_code=429, detail="Too many signups from this network. Try again later.")

    body = await request.json()
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    brand_name = (body.get("brand_name") or "").strip()

    result, reason = signup(email, password, brand_name)
    if reason == "ok":
        send_verification_email(result["email"], result["verify_token"])
        # Return the api_key so the panel logs straight into the demo.
        return {
            "status": 200,
            "api_key": result["api_key"],
            "brand_link_token": result["brand_link_token"],
            "email_verified": False,
        }
    if reason == "exists":
        raise HTTPException(status_code=409, detail="An account with this email already exists.")
    if reason.startswith("invalid:"):
        raise HTTPException(status_code=400, detail=reason.split("invalid:", 1)[1])
    raise HTTPException(status_code=400, detail="Signup failed.")


@router.post("/admin/verify-email")
async def admin_verify_email(request: Request):
    from core.accounts import verify_email

    body = await request.json()
    token = (body.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="token is required")
    if verify_email(token):
        return {"status": 200, "verified": True}
    raise HTTPException(status_code=400, detail="Invalid or expired verification link.")


# ---------------------------------------------------------------------------
# Brand configuration (multi-tenant white-label)
# ---------------------------------------------------------------------------

class BrandConfigRequest(BaseModel):
    pg_ids: list[str] | None = None
    brand_name: str | None = None
    cities: str | None = None
    areas: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_access_token: str | None = None  # "••••xxxx" → preserve existing token
    waba_id: str | None = None
    is_meta: bool | None = None
    # brand_link_token is auto-generated server-side, never in request body


@router.get("/admin/brand-config")
async def admin_get_brand_config(api_key: str = Depends(require_brand_api_key)):
    """Return brand config for the given API key. Access token is masked."""
    config = dict(get_brand_config(api_key) or {})
    token = config.get("whatsapp_access_token", "")
    if token:
        config["whatsapp_access_token"] = "••••" + token[-4:]
    link_token = config.get("brand_link_token", "")
    chatbot_url = f"{CHAT_BASE_URL}?brand={link_token}" if link_token else None
    return {"is_configured": bool(config.get("pg_ids")), "chatbot_url": chatbot_url, **config}


@router.post("/admin/brand-config")
async def admin_set_brand_config(body: BrandConfigRequest, api_key: str = Depends(require_brand_api_key)):
    """Upsert brand config. Partial updates are merged with existing config."""
    existing = dict(get_brand_config(api_key) or {})
    merged = {**existing, **{k: v for k, v in body.dict().items() if v is not None}}
    wa_token = merged.get("whatsapp_access_token", "")
    if wa_token.startswith("••••"):
        # Masked value submitted — preserve the real token already in Redis
        merged["whatsapp_access_token"] = existing.get("whatsapp_access_token", "")
    if not merged.get("brand_link_token"):
        # Auto-generate a permanent UUID on first save
        merged["brand_link_token"] = str(uuid_lib.uuid4())
    merged["updated_at"] = datetime.utcnow().isoformat() + "Z"
    if "created_at" not in merged:
        merged["created_at"] = merged["updated_at"]
    set_brand_config(api_key, merged)
    return {"ok": True, "brand_link_token": merged["brand_link_token"]}


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------

class BroadcastRequest(BaseModel):
    message: str


@router.post("/admin/broadcast")
async def admin_broadcast(req: BroadcastRequest, brand_hash: str = Depends(require_admin_brand_key)):
    """Send a text message to all brand users active in the last 7 days."""
    cutoff = _time.time() - 7 * 86400
    uids = get_brand_active_users(brand_hash, offset=0, limit=500)

    sent = 0
    for uid in uids:
        try:
            score = _r().zscore(f"active_users:{brand_hash}", uid)
            if score and float(score) < cutoff:
                continue
            await send_text(uid, req.message)
            sent += 1
        except Exception as e:
            logger.warning("broadcast to %s failed: %s", uid, e)

    return {"ok": True, "sent": sent}


# ---------------------------------------------------------------------------
# Property documents
# ---------------------------------------------------------------------------

@router.get("/admin/properties")
async def admin_list_properties(brand_hash: str = Depends(require_admin_brand_key)):
    """Return properties belonging to this brand.

    Always returns all brand pg_ids as stubs; enriches with names/areas from
    the Redis property_info_map cache when available (populated by user searches).
    """
    brand_cfg = get_brand_config_by_hash(brand_hash) or {}
    brand_pg_ids: list[str] = brand_cfg.get("pg_ids", [])

    # Try to enrich from the Rentok property cache
    prop_map: dict = {}
    try:
        raw = _r().get("property_info_map")
        if raw:
            prop_map = _json_module.loads(raw)
    except Exception as e:
        logger.warning("admin_list_properties cache read: %s", e)

    doc_counts = await pg.get_property_doc_counts(brand_pg_ids)

    # Always return all brand pg_ids; use cache for enrichment
    props = []
    for pid in brand_pg_ids:
        info = prop_map.get(pid, {})
        props.append({
            "prop_id": pid,
            "name": info.get("pg_name") or info.get("name") or f"Property …{pid[-6:]}",
            "area": info.get("area") or info.get("location") or "",
            "doc_count": doc_counts.get(pid, 0),
        })
    return {"properties": props}


def _require_property_ownership(prop_id: str, brand_hash: str) -> None:
    """Raise 403 if prop_id is not in the brand's pg_ids list."""
    brand_cfg = get_brand_config_by_hash(brand_hash) or {}
    brand_pg_ids = brand_cfg.get("pg_ids", [])
    if prop_id not in brand_pg_ids:
        raise HTTPException(status_code=403, detail="Property not in your brand")


@router.get("/admin/properties/{prop_id}/documents")
async def admin_get_documents(prop_id: str, brand_hash: str = Depends(require_admin_brand_key)):
    """Return document metadata for a property."""
    _require_property_ownership(prop_id, brand_hash)
    docs = await pg.get_property_documents(prop_id)
    return {"documents": docs}


async def _embed_document_background(doc_id: int, text: str) -> None:
    """Background task: embed document text and store vector in Postgres.

    Fire-and-forget — failures are logged but never block the upload response.
    """
    try:
        from config import settings
        if not settings.NOMIC_API_KEY:
            return
        from utils.embeddings import embed_documents
        vectors = await embed_documents([text])
        if vectors and len(vectors) == 1:
            await pg.update_document_embedding(doc_id, vectors[0])
            logger.info("Embedded document %d (%d dims)", doc_id, len(vectors[0]))
    except Exception as e:
        logger.warning("Background embed failed for doc %d: %s", doc_id, e)


class UploadDocResponse(BaseModel):
    id: int
    filename: str
    size_bytes: int
    uploaded_at: str


@router.post("/admin/properties/{prop_id}/documents")
async def admin_upload_document(
    prop_id: str,
    file: UploadFile = File(...),
    category: str = Form(""),
    brand_hash: str = Depends(require_admin_brand_key),
):
    """Upload a knowledge document (PDF, XLSX, CSV, TXT) for a property.

    Optional category: pricing_availability | living_experience | location_area | brand_story.
    If provided, used for skill-based document filtering in the semantic KB.
    """
    _require_property_ownership(prop_id, brand_hash)
    ALLOWED = {"pdf", "xlsx", "csv", "txt"}
    VALID_CATEGORIES = {"pricing_availability", "living_experience", "location_area", "brand_story"}
    MAX_SIZE = 10 * 1024 * 1024  # 10 MB

    # Validate category
    cat = category.strip() if category else None
    if cat and cat not in VALID_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Invalid category: {cat}. Must be one of: {', '.join(sorted(VALID_CATEGORIES))}")

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: .{ext}")

    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(status_code=400, detail="File exceeds 10 MB limit")

    # Extract text
    text = ""
    try:
        if ext == "pdf":
            from io import BytesIO
            import pypdf
            reader = pypdf.PdfReader(BytesIO(content))
            text = "\n".join(p.extract_text() or "" for p in reader.pages)
        elif ext == "xlsx":
            from io import BytesIO
            import openpyxl
            wb = openpyxl.load_workbook(BytesIO(content), read_only=True, data_only=True)
            rows = []
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    rows.append("\t".join(str(c) if c is not None else "" for c in row))
            text = "\n".join(rows)
        elif ext == "csv":
            text = content.decode("utf-8", errors="replace")
        else:
            text = content.decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("text extraction failed for %s: %s", file.filename, e)
        text = ""

    doc = await pg.insert_property_document(
        property_id=prop_id,
        filename=file.filename,
        file_type=ext,
        content_text=text,
        size_bytes=len(content),
        category=cat,
    )

    # Background: embed the document text for semantic retrieval
    if text.strip():
        import asyncio
        asyncio.create_task(_embed_document_background(doc["id"], text))

    return doc


@router.delete("/admin/properties/{prop_id}/documents/{doc_id}")
async def admin_delete_document(prop_id: str, doc_id: int, brand_hash: str = Depends(require_admin_brand_key)):
    """Delete a property document."""
    _require_property_ownership(prop_id, brand_hash)
    deleted = await pg.delete_property_document(prop_id, doc_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Backfill utility
# ---------------------------------------------------------------------------

@router.post("/admin/backfill-brands")
async def admin_backfill_brands(brand_hash: str = Depends(require_admin_brand_key)):
    """One-time backfill: tag all existing users with this brand and populate
    the brand-scoped active_users sorted set.

    Safe to call multiple times — idempotent writes.

    How it works:
      1. Scans the global active_users sorted set (all existing users).
      2. For each user, checks if they already have a brand tag.
      3. If untagged (legacy user), assigns them to the calling admin's brand.
      4. Users already tagged with a different brand are skipped.
    """
    from db.redis_store import set_user_brand, add_to_brand_active_users

    try:
        r = _r()
        tagged = 0
        skipped = 0
        already_tagged = 0

        # Iterate all users in the global sorted set
        all_uids = get_active_users(offset=0, limit=5000)
        for uid in all_uids:
            existing_brand = get_user_brand(uid)
            if existing_brand == brand_hash:
                # Already tagged with this brand
                already_tagged += 1
                continue
            if existing_brand:
                # Tagged with a different brand — skip
                skipped += 1
                continue
            # Untagged legacy user — assign to calling brand
            set_user_brand(uid, brand_hash)
            add_to_brand_active_users(uid, brand_hash)
            tagged += 1

        total = get_brand_active_users_count(brand_hash)
        return {
            "ok": True,
            "tagged": tagged,
            "already_tagged": already_tagged,
            "skipped_other_brand": skipped,
            "total_in_brand": total,
        }
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"error": str(exc), "trace": traceback.format_exc()[-800:]},
        )


# ---------------------------------------------------------------------------
# Error events (Sprint 4)
# ---------------------------------------------------------------------------

@router.get("/admin/errors")
async def admin_errors(
    type: str = "",
    days: int = 7,
    limit: int = 50,
    offset: int = 0,
    brand_hash: str = Depends(require_admin_brand_key),
):
    """Return paginated error events with optional type filter.

    Query params:
      type: filter by error_type (tool_failure | api_timeout | empty_response | routing_override)
      days: lookback window (default 7, max 90)
      limit: page size (default 50)
      offset: pagination offset
    """
    days = max(1, min(days, 90))
    events = await pg.get_error_events(
        brand_hash=brand_hash,
        error_type=type or None,
        days=days,
        limit=limit,
        offset=offset,
    )
    summary = await pg.get_error_summary(brand_hash=brand_hash, days=days)
    return {
        "events": events,
        "summary": summary,
        "total_in_window": sum(summary.values()),
        "filters": {"type": type, "days": days},
        "offset": offset,
        "limit": limit,
    }


# ---------------------------------------------------------------------------
# Eval health — CI stress-test results
# ---------------------------------------------------------------------------

class EvalRunRequest(BaseModel):
    run_at: str
    passed: int
    warned: int
    failed: int
    total: int
    scenarios: list[dict] | None = None
    trigger: str | None = None
    commit: str | None = None


@router.get("/admin/eval-health")
async def admin_get_eval_health(brand_hash: str = Depends(require_admin_brand_key)):
    """Return the last stored eval run and history (last 10 runs)."""
    from db.redis.eval import get_eval_last_run, get_eval_history
    return {"last_run": get_eval_last_run(brand_hash), "history": get_eval_history(brand_hash, limit=10)}


@router.post("/admin/eval-health")
async def admin_post_eval_health(
    body: EvalRunRequest, brand_hash: str = Depends(require_admin_brand_key)
):
    """Record a new eval run result. Call this from CI after running stress_test_broker.py."""
    from db.redis.eval import save_eval_run
    save_eval_run(brand_hash, body.model_dump())
    return {"ok": True}


@router.post("/admin/backfill-message-brand-hash")
async def admin_backfill_message_brand_hash(brand_hash: str = Depends(require_admin_brand_key)):
    """One-time migration: stamp booking_messages rows that have NULL brand_hash with the
    calling brand's hash.  Safe to call multiple times — only touches NULL rows.
    """
    if pg._pool is None:
        raise HTTPException(status_code=503, detail="Database pool not available")
    try:
        null_before = await pg._pool.fetchval(
            "SELECT COUNT(*) FROM booking_messages WHERE brand_hash IS NULL"
        )
        await pg._pool.execute(
            "UPDATE booking_messages SET brand_hash = $1 WHERE brand_hash IS NULL",
            brand_hash,
        )
        null_after = await pg._pool.fetchval(
            "SELECT COUNT(*) FROM booking_messages WHERE brand_hash IS NULL"
        )
        return {
            "ok": True,
            "updated": null_before - null_after,
            "null_remaining": null_after,
            "brand_hash": brand_hash,
        }
    except Exception as exc:
        logger.error("backfill-message-brand-hash failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
