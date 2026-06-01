"""
db/redis/__init__.py — Public API for the Redis package.

Re-exports all functions from domain modules so callers can use:
    from db.redis import get_conversation, save_preferences, ...

Domain modules:
  _base        — connection pool, _r(), _json_get, _json_set, TTL constants
  conversation — conversation history, active request, last agent, account values
  user         — preferences, identity, language, KYC, cross-session memory
  property     — property cache, search results, images, templates
  analytics    — feedback, agent/skill usage, funnel, WhatsApp dedup
  payment      — payment info, follow-up scheduling
  brand        — multi-tenant brand config
  admin        — user enumeration, human mode, session cost
"""

# Infrastructure — exposed for the rare callers that need raw access
from db.redis._base import _r, _json_get, _json_set  # noqa: F401
from db.redis._base import (  # noqa: F401
    PROPERTY_INFO_TTL,
    SEARCH_IDS_TTL,
    LANGUAGE_TTL,
    ANALYTICS_TTL,
    LAST_SEARCH_TTL,
)

# Conversation domain
from db.redis.conversation import (  # noqa: F401
    get_conversation,
    save_conversation,
    clear_conversation,
    set_active_request,
    get_active_request,
    delete_active_request,
    set_last_agent,
    get_last_agent,
    set_account_values,
    get_account_values,
    clear_account_values,
    set_whitelabel_pg_ids,
    get_whitelabel_pg_ids,
    # wamid-based dedup (WhatsApp)
    set_wamid_seen,
    is_wamid_seen,
    # Per-user WhatsApp message queue (debounce + accumulation)
    wa_queue_push,
    wa_queue_drain,
    wa_queue_len,
    wa_processing_acquire,
    wa_processing_release,
    # Pipeline cancellation signal (Phase C)
    set_cancel_requested,
    clear_cancel_requested,
    is_cancel_requested,
)

# User domain
from db.redis.user import (  # noqa: F401
    save_preferences,
    get_preferences,
    set_user_name,
    get_user_name,
    set_user_phone,
    get_user_phone,
    set_no_message,
    get_no_message,
    clear_no_message,
    set_user_language,
    get_user_language,
    set_aadhar_user_name,
    get_aadhar_user_name,
    delete_aadhar_user_name,
    set_aadhar_gender,
    get_aadhar_gender,
    delete_aadhar_gender,
    detect_persona,
    update_persona,
    get_user_memory,
    save_user_memory,
    update_user_memory,
    _calculate_lead_score,
    get_lead_temperature,
    record_property_viewed,
    record_property_shortlisted,
    record_visit_scheduled,
    add_deal_breaker,
    build_returning_user_context,
    FUNNEL_ORDER,
)

# Property domain
from db.redis.property import (  # noqa: F401
    set_property_info_map,
    get_property_info_map,
    set_last_search_results,
    get_last_search_results,
    get_shortlisted_properties,
    save_property_template,
    get_property_template,
    clear_property_template,
    set_property_images_id,
    get_property_images_id,
    clear_property_images_id,
    set_image_urls,
    get_image_urls,
    clear_image_urls,
    set_property_id_for_search,
    get_property_id_for_search,
    clear_property_id_for_search,
)

# Analytics domain
from db.redis.analytics import (  # noqa: F401
    save_feedback,
    get_feedback_counts,
    track_agent_usage,
    get_agent_usage,
    track_skill_usage,
    track_skill_miss,
    get_skill_usage,
    get_skill_misses,
    track_funnel,
    get_funnel,
    increment_agent_cost,
    get_agent_costs,
    increment_daily_cost,
    get_daily_cost,
    # Tool reliability (C1)
    track_tool_result,
    get_tool_stats,
    # Routing accuracy (C2)
    track_routing_override,
    get_routing_overrides,
    # Response latency (C3)
    track_response_latency,
    get_response_latency,
    # Property-level events (Sprint 3)
    track_property_event,
    get_property_events,
    get_property_performance,
    PROPERTY_EVENTS,
    # Property outcome signals (Sprint 5)
    track_property_outcome,
    get_property_signals,
    set_response,
    get_response,
    FUNNEL_STAGES,
    # Daily quality aggregate (trend sparkline + avg KPI)
    track_daily_quality,
    get_quality_trend,
)

# Payment domain
from db.redis.payment import (  # noqa: F401
    set_payment_info,
    get_payment_info,
    clear_payment_info,
    schedule_followup,
    get_due_followups,
    complete_followup,
    cancel_followups,
)

# Brand domain
from db.redis.brand import (  # noqa: F401
    _brand_hash,
    get_brand_config,
    get_brand_config_by_hash,
    set_brand_config,
    get_brand_wa_config,
    get_brand_by_token,
    get_default_brand_config,
    # Per-brand feature flags
    get_brand_flags,
    set_brand_flag,
    get_effective_flags,
)

# Admin domain
from db.redis.admin import (  # noqa: F401
    get_active_users,
    get_active_users_count,
    # Brand user tagging + per-brand user enumeration
    set_user_brand,
    get_user_brand,
    add_to_brand_active_users,
    get_brand_active_users,
    get_brand_active_users_count,
    # Human mode
    get_human_mode,
    set_human_mode,
    clear_human_mode,
    increment_session_cost,
    get_session_cost,
)

# Quality domain (Sprint 4)
from db.redis.quality import (  # noqa: F401
    compute_conversation_quality,
    save_conversation_quality,
    get_conversation_quality,
    update_conversation_quality,
)

# Idempotency domain (Wave 3) — burst-dedup for write-path tools
from db.redis.idempotency import (  # noqa: F401
    idem_begin,
    idem_complete,
    idem_release,
    idem_clear,
)

# Eval domain — CI stress-test result storage
from db.redis.eval import (  # noqa: F401
    save_eval_run,
    get_eval_last_run,
    get_eval_history,
)
