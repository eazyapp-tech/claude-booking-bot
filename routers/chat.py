"""
routers/chat.py — Chat, streaming, feedback, funnel, and language endpoints.

Routes:
  POST /chat           — JSON API (non-streaming)
  POST /chat/stream    — SSE streaming
  POST /feedback       — Thumbs up/down
  GET  /feedback/stats — Aggregate feedback counters
  GET  /funnel         — Funnel stage counts
  POST /language       — Language preference override
"""

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import core.state as state
from core.auth import verify_api_key
from core.log import get_logger
from core.message_parser import parse_message_parts
from core.pipeline import run_pipeline, _route_agent
from core.rate_limiter import check_rate_limit
from core.signals import reset_signals
from core.tenancy import resolve_web_brand
from core.ui_parts import generate_ui_parts, make_error_part
from db import postgres as pg
from db.redis_store import (
    set_account_values,
    set_whitelabel_pg_ids,
    set_user_brand,
    get_user_brand,
    add_to_brand_active_users,
    get_human_mode,
    get_conversation,
    save_conversation,
    get_user_language,
    set_user_language,
    save_feedback,
    get_feedback_counts,
    get_funnel,
    set_last_agent,
    set_cancel_requested,
    clear_cancel_requested,
)
from agents import broker_agent, booking_agent, profile_agent, default_agent

logger = get_logger("routers.chat")

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    user_id: str
    message: str
    account_values: dict = {}
    brand_token: str = ""  # public link token — the ONLY trusted source of brand identity


class ChatResponse(BaseModel):
    response: str
    agent: str = ""
    parts: list[dict] = []
    locale: str = "en"


class FeedbackRequest(BaseModel):
    user_id: str
    message_snippet: str = ""
    rating: str  # "up" or "down"
    agent: str = ""


class LanguageRequest(BaseModel):
    user_id: str
    language: str  # "en", "hi", or "mr"


class StopRequest(BaseModel):
    user_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _apply_web_brand(user_id: str, account_values: dict, brand_token: str) -> tuple[str, list[str]]:
    """Resolve + persist server-authoritative brand identity for a web request.

    Trusts ONLY the verified link token (or the configured default brand) — never
    the client-supplied brand_hash/pg_ids in account_values. Returns
    (brand_hash, pg_ids); both empty when no brand resolves this turn.
    """
    brand_hash, pg_ids, safe_account = resolve_web_brand(brand_token, account_values)
    if brand_hash:
        set_account_values(user_id, safe_account)
        if pg_ids:
            set_whitelabel_pg_ids(user_id, pg_ids)
        set_user_brand(user_id, brand_hash)
        add_to_brand_active_users(user_id, brand_hash)
    return brand_hash, pg_ids


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/chat", response_model=ChatResponse, dependencies=[Depends(verify_api_key)])
async def chat(req: ChatRequest):
    """JSON API for Streamlit and other clients."""
    if not req.user_id or not req.message:
        raise HTTPException(status_code=400, detail="user_id and message are required")

    check_rate_limit(req.user_id)

    # Resolve brand identity server-side from the verified link token — never trust
    # the client-supplied brand_hash/pg_ids in account_values.
    brand_hash, pg_ids_list = _apply_web_brand(req.user_id, req.account_values, req.brand_token)

    response, agent_name, language = await run_pipeline(req.user_id, req.message)

    # Human mode — AI bypassed; admin is responding manually
    if agent_name == "human":
        return ChatResponse(response="", agent="human", parts=[], locale=language)

    # Persist to Postgres (brand-scoped). Fall back to any prior brand tag if this
    # turn carried no token (e.g. resumed conversation).
    _chat_bh = brand_hash or get_user_brand(req.user_id)
    await pg.insert_message(
        thread_id=req.user_id,
        user_phone=req.user_id,
        message_text=req.message,
        message_sent_by=1,
        platform_type="api",
        is_template=False,
        pg_ids=pg_ids_list,
        brand_hash=_chat_bh,
    )
    await pg.insert_message(
        thread_id=req.user_id,
        user_phone=req.user_id,
        message_text=response,
        message_sent_by=2,
        platform_type="api",
        is_template=False,
        pg_ids=pg_ids_list,
        brand_hash=_chat_bh,
    )

    # Parse structured parts for frontend rendering
    try:
        parts = parse_message_parts(response, req.user_id)
    except Exception as e:
        logger.warning("parse_message_parts failed: %s", e)
        parts = [{"type": "text", "markdown": response}]

    # Generate backend-controlled UI parts (chips, buttons)
    try:
        ui_parts = generate_ui_parts(response, agent_name, req.user_id, language)
        parts.extend(ui_parts)
    except Exception as e:
        logger.warning("generate_ui_parts failed: %s", e)

    return ChatResponse(response=response, agent=agent_name, parts=parts, locale=language)


@router.post("/chat/stream", dependencies=[Depends(verify_api_key)])
async def chat_stream(req: ChatRequest):
    """SSE streaming endpoint — streams agent events as they happen."""
    if not req.user_id or not req.message:
        raise HTTPException(status_code=400, detail="user_id and message are required")

    check_rate_limit(req.user_id)

    # Resolve brand identity server-side from the verified link token — never trust
    # the client-supplied brand_hash/pg_ids in account_values.
    brand_hash, pg_ids_list = _apply_web_brand(req.user_id, req.account_values, req.brand_token)

    # Brand used in save_conversation + PG inserts below; fall back to any prior tag.
    stream_brand_hash = brand_hash or get_user_brand(req.user_id)

    # Human takeover bypass — save user message and emit empty stream so admin handles it
    if get_human_mode(req.user_id, brand_hash=stream_brand_hash):
        conv = get_conversation(req.user_id)
        conv.append({"role": "user", "content": req.message})
        save_conversation(req.user_id, conv, brand_hash=stream_brand_hash)
        language = get_user_language(req.user_id) or "en"

        async def _human_stream():
            yield f"event: done\ndata: {json.dumps({'agent': 'human', 'full_response': '', 'parts': [], 'locale': language})}\n\n"

        return StreamingResponse(
            _human_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    # Route to the correct agent (fast — Haiku + keyword fallback + skill detection)
    agent_name, messages, language, skills = await _route_agent(req.user_id, req.message)

    # Get agent config (with language + skills for broker)
    if agent_name == "broker":
        cfg = broker_agent.get_config(req.user_id, language=language, skills=skills)
        await broker_agent._inject_doc_context(cfg, user_message=req.message)
    else:
        config_map = {
            "default": default_agent.get_config,
            "booking": booking_agent.get_config,
            "profile": profile_agent.get_config,
        }
        get_cfg = config_map.get(agent_name, default_agent.get_config)
        cfg = get_cfg(req.user_id, language=language)

    # Drop any stale Stop flag from a prior turn so it can't cancel this fresh run
    # before the user has interacted (the flag has a 30s TTL on the WhatsApp path).
    clear_cancel_requested(req.user_id)

    async def event_generator():
        # Clean slate for this turn's truth signals (read at egress to shape honest UI).
        reset_signals()
        # Emit agent_start so frontend knows which agent is handling + locale
        yield f"event: agent_start\ndata: {json.dumps({'agent': agent_name, 'locale': language})}\n\n"

        full_text = ""
        try:
            async for ev in state.engine.run_agent_stream(
                system_prompt=cfg["system_prompt"],
                tools=cfg["tools"],
                messages=messages,
                model=cfg["model"],
                user_id=req.user_id,
                tool_executor=cfg["executor"],
                agent_name=agent_name,
            ):
                yield f"event: {ev['event']}\ndata: {json.dumps(ev['data'])}\n\n"
                if ev["event"] == "content_delta":
                    full_text += ev["data"]["text"]

        except Exception as e:
            logger.error("stream error: %s", e)
            error_msg = "I'm experiencing a temporary issue. Please try again."
            yield f"event: error\ndata: {json.dumps({'text': error_msg})}\n\n"
            full_text = full_text or error_msg
            # Emit error card instead of plain text
            error_part = make_error_part(
                title="Couldn't process your request",
                message="We hit a temporary issue. This usually resolves in a moment.",
                retry_label="Try Again",
                retry_message=req.message,
            )
            error_parts = [error_part]
            yield f"event: done\ndata: {json.dumps({'agent': agent_name or 'system', 'full_response': full_text, 'parts': error_parts, 'locale': language})}\n\n"
            # Persist and return
            set_last_agent(req.user_id, agent_name or "system")
            state.conversation.add_assistant_message(req.user_id, full_text, brand_hash=stream_brand_hash)
            await pg.insert_message(thread_id=req.user_id, user_phone=req.user_id, message_text=req.message, message_sent_by=1, platform_type="api", is_template=False, pg_ids=pg_ids_list, brand_hash=stream_brand_hash)
            await pg.insert_message(thread_id=req.user_id, user_phone=req.user_id, message_text=full_text, message_sent_by=2, platform_type="api", is_template=False, pg_ids=pg_ids_list, brand_hash=stream_brand_hash)
            return

        # Parse structured parts for frontend rendering
        try:
            parts = parse_message_parts(full_text, req.user_id)
        except Exception as e:
            logger.warning("parse_message_parts failed: %s", e)
            parts = [{"type": "text", "markdown": full_text}]

        # Generate backend-controlled UI parts (chips, buttons)
        try:
            ui_parts = generate_ui_parts(full_text, agent_name, req.user_id, language)
            parts.extend(ui_parts)
        except Exception as e:
            logger.warning("generate_ui_parts failed: %s", e)

        # Emit final done event with the full assembled response + parts + locale
        yield f"event: done\ndata: {json.dumps({'agent': agent_name, 'full_response': full_text, 'parts': parts, 'locale': language})}\n\n"

        # Persist state (same as non-streaming path)
        set_last_agent(req.user_id, agent_name)
        state.conversation.add_assistant_message(req.user_id, full_text, brand_hash=stream_brand_hash)

        await pg.insert_message(
            thread_id=req.user_id, user_phone=req.user_id,
            message_text=req.message, message_sent_by=1,
            platform_type="api", is_template=False, pg_ids=pg_ids_list,
            brand_hash=stream_brand_hash,
        )
        await pg.insert_message(
            thread_id=req.user_id, user_phone=req.user_id,
            message_text=full_text, message_sent_by=2,
            platform_type="api", is_template=False, pg_ids=pg_ids_list,
            brand_hash=stream_brand_hash,
        )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable Nginx buffering on Render
        },
    )


@router.post("/chat/stop", dependencies=[Depends(verify_api_key)])
async def chat_stop(req: StopRequest):
    """Server-authoritative interrupt.

    The web client also aborts its SSE fetch, but proxy buffering (Render/Nginx)
    can delay the disconnect from reaching the ASGI server — meanwhile a
    multi-round tool loop keeps spending. This sets the Phase-C cancel flag so
    core.claude.run_agent_stream stops at the next tool-round checkpoint,
    independent of connection teardown.
    """
    if not req.user_id:
        raise HTTPException(status_code=400, detail="user_id is required")
    set_cancel_requested(req.user_id)
    return {"status": "ok"}


@router.post("/feedback", dependencies=[Depends(verify_api_key)])
async def submit_feedback(req: FeedbackRequest):
    """Record thumbs-up / thumbs-down feedback on a bot response."""
    if req.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating must be 'up' or 'down'")
    save_feedback(req.user_id, req.message_snippet, req.rating, req.agent, brand_hash=get_user_brand(req.user_id))
    return {"status": "ok"}


@router.get("/feedback/stats", dependencies=[Depends(verify_api_key)])
async def feedback_stats():
    """Return aggregate feedback counters."""
    return get_feedback_counts()


@router.get("/funnel", dependencies=[Depends(verify_api_key)])
async def funnel_stats(day: str = None):
    """Return funnel stage counts for a given day (default: today)."""
    return get_funnel(day)


@router.post("/language", dependencies=[Depends(verify_api_key)])
async def set_language(req: LanguageRequest):
    """Allow the frontend to explicitly set the user's preferred language."""
    if req.language not in ("en", "hi", "mr"):
        raise HTTPException(status_code=400, detail="language must be 'en', 'hi', or 'mr'")
    set_user_language(req.user_id, req.language)
    return {"status": "ok", "language": req.language}
