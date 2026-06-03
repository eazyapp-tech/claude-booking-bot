"""
core/pipeline.py — Shared AI pipeline: run_pipeline + _route_agent.

Both functions are used by the streaming and non-streaming chat endpoints.
They reference core.state.engine and core.state.conversation (set at lifespan startup).
"""

import time

import core.state as state
from core.log import get_logger
from core.language import detect_language
from core.router import apply_keyword_safety_net
from db.redis_store import (
    get_human_mode,
    get_user_memory,
    get_user_phone,
    update_user_memory,
    get_user_language,
    set_user_language,
    get_conversation,
    save_conversation,
    set_last_agent,
    track_agent_usage,
    track_skill_usage,
    track_routing_override,
    track_response_latency,
    update_persona,
    get_user_brand,
)
from agents import supervisor, broker_agent, booking_agent, profile_agent, default_agent

logger = get_logger("pipeline")


async def run_pipeline(user_id: str, message: str) -> tuple[str, str, str]:
    """Run the full supervisor → agent pipeline. Returns (response, agent_name, language)."""
    # Resolve brand_hash once for all downstream calls
    brand_hash = get_user_brand(user_id)

    # Follow-up reply interception — if user has an active followup awaiting reply,
    # handle it directly without routing through supervisor/agent
    try:
        from core.followup import has_active_followup, handle_followup_reply
        if has_active_followup(user_id):
            followup_response = handle_followup_reply(user_id, message)
            if followup_response:
                # Save to conversation history so it shows in admin + chat
                lang = get_user_language(user_id) or "en"
                conv = get_conversation(user_id)
                conv.append({"role": "user", "content": message})
                conv.append({"role": "assistant", "content": followup_response})
                save_conversation(user_id, conv, brand_hash=brand_hash)
                return followup_response, "followup", lang
    except Exception as e:
        logger.warning("followup reply check failed: %s", e)

    # Human takeover bypass — admin is handling this user manually
    if get_human_mode(user_id, brand_hash=brand_hash):
        conv = get_conversation(user_id)
        conv.append({"role": "user", "content": message})
        save_conversation(user_id, conv, brand_hash=brand_hash)
        return "", "human", get_user_language(user_id) or "en"

    # Update cross-session memory (only bump session_count on first message of a session)
    mem = get_user_memory(user_id)
    updates = {"phone_collected": bool(get_user_phone(user_id))}
    # Conversation history is empty on first message → new session
    if not get_conversation(user_id):
        updates["session_count"] = mem.get("session_count", 0) + 1
    update_user_memory(user_id, **updates)

    # Detect and persist persona from user message (non-blocking)
    update_persona(user_id, message)

    # Detect language from message
    detected_lang = detect_language(message)
    stored_lang = get_user_language(user_id)
    language = detected_lang if detected_lang != "en" else stored_lang
    if detected_lang != "en":
        set_user_language(user_id, detected_lang)

    # Load conversation history + summarize if needed
    messages = await state.conversation.add_user_message_with_summary(user_id, message, brand_hash=brand_hash)

    # SKILL RESOLUTION ORDER (broker agent only):
    # 1. Supervisor LLM classifies → {"agent": str, "skills": list[str]}
    # 2. Keyword safety net overrides agent if LLM misclassifies (e.g. booking
    #    intent mis-routed to broker). If it fires, skills are cleared — they
    #    were computed for the wrong agent.
    # 3. If broker has no skills after step 2, keyword heuristic fills them in
    #    (detect_skills_heuristic). This is the last-resort fallback.
    route_result = await supervisor.route(state.engine, messages)
    agent_name = route_result["agent"]
    skills = route_result.get("skills", [])

    original_agent = agent_name
    agent_name = apply_keyword_safety_net(agent_name, message, user_id)
    if agent_name != original_agent:
        skills = []  # Safety net fired — skills from wrong agent are invalid
        track_routing_override(original_agent, agent_name, brand_hash=brand_hash)
        # Fire-and-forget: log routing override as structured error event
        try:
            import asyncio
            from db.postgres import insert_error_event
            asyncio.create_task(insert_error_event(
                user_id=user_id,
                brand_hash=brand_hash,
                error_type="routing_override",
                error_source=f"{original_agent}>{agent_name}",
                error_message=f"Safety net overrode {original_agent} to {agent_name}",
                context={"message": message[:200]},
            ))
        except Exception:
            pass

    if agent_name == "broker" and not skills:
        from skills.skill_map import detect_skills_heuristic
        skills = detect_skills_heuristic(message)

    logger.info("user=%s agent=%s lang=%s msg=%s", user_id, agent_name, language, message[:60])

    # Track agent usage + skill usage for analytics (brand-scoped)
    track_agent_usage(user_id, agent_name, brand_hash=brand_hash)
    if skills:
        track_skill_usage(skills, brand_hash=brand_hash)

    # Step 2: Run selected agent (with language + skills for broker)
    t0 = time.monotonic()
    if agent_name == "broker":
        response = await broker_agent.run(state.engine, messages, user_id, language=language, skills=skills)
    else:
        agent_map = {
            "default": default_agent.run,
            "booking": booking_agent.run,
            "profile": profile_agent.run,
        }
        agent_fn = agent_map.get(agent_name, default_agent.run)
        response = await agent_fn(state.engine, messages, user_id, language=language)
    latency_ms = int((time.monotonic() - t0) * 1000)
    track_response_latency(agent_name, latency_ms, brand_hash=brand_hash)

    # Log empty responses as structured error events
    if not response or not response.strip():
        try:
            import asyncio
            from db.postgres import insert_error_event
            asyncio.create_task(insert_error_event(
                user_id=user_id,
                brand_hash=brand_hash,
                error_type="empty_response",
                error_source=agent_name,
                error_message="Agent returned empty response",
                context={"latency_ms": latency_ms, "message": message[:200]},
            ))
        except Exception:
            pass

    # Track last active agent for multi-turn continuations
    set_last_agent(user_id, agent_name)

    # Save assistant response to history
    state.conversation.add_assistant_message(user_id, response, brand_hash=brand_hash)

    # Compute + cache attention flags + conversation quality (fire-and-forget)
    try:
        conv = get_conversation(user_id)
        mem = get_user_memory(user_id)
        from core.attention import update_attention_flags
        update_attention_flags(user_id, conv, mem, brand_hash=brand_hash)
        from db.redis.quality import update_conversation_quality
        quality_data = update_conversation_quality(user_id, conv, mem)
        if quality_data and brand_hash:
            from db.redis.analytics import track_daily_quality
            track_daily_quality(brand_hash=brand_hash, score=quality_data.get("score", 0))
    except Exception:
        pass

    return response, agent_name, language


async def _route_agent(user_id: str, message: str) -> tuple[str, list[dict], str, list[str]]:
    """Shared routing logic: returns (agent_name, messages, language, skills).

    Applies supervisor + keyword safety net + last-agent fallback + skill detection —
    identical to run_pipeline but without running the agent itself.
    """
    # Detect language
    detected_lang = detect_language(message)
    stored_lang = get_user_language(user_id)
    language = detected_lang if detected_lang != "en" else stored_lang
    if detected_lang != "en":
        set_user_language(user_id, detected_lang)

    # Resolve brand_hash for conversation save
    brand_hash = get_user_brand(user_id)
    messages = await state.conversation.add_user_message_with_summary(user_id, message, brand_hash=brand_hash)

    route_result = await supervisor.route(state.engine, messages)
    agent_name = route_result["agent"]
    skills = route_result.get("skills", [])

    # Safety net: keyword-based override if supervisor misclassifies
    original_agent = agent_name
    agent_name = apply_keyword_safety_net(agent_name, message, user_id)
    # If safety net changed the agent, skills are no longer valid
    if agent_name != original_agent:
        skills = []
        track_routing_override(original_agent, agent_name, brand_hash=brand_hash)
    # Keyword fallback for broker skill detection
    if agent_name == "broker" and not skills:
        from skills.skill_map import detect_skills_heuristic
        skills = detect_skills_heuristic(message)

    # Track agent usage + skill usage for analytics (brand-scoped)
    track_agent_usage(user_id, agent_name, brand_hash=brand_hash)
    if skills:
        track_skill_usage(skills, brand_hash=brand_hash)

    logger.info("user=%s agent=%s lang=%s skills=%s msg=%s", user_id, agent_name, language, skills, message[:60])
    return agent_name, messages, language, skills
