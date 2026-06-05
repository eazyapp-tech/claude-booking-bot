"""
Central tool registry: maps tool names to Anthropic schemas + handler functions.
Each agent picks the tools it needs by name.

Schemas live in their tool files (TOOL_SCHEMA / named schema constants).
This file is thin wiring only — it imports and pairs them at startup.
"""

from typing import Callable
from config import settings

# Handler imports (lazy — registered at startup)
_TOOL_HANDLERS: dict[str, Callable] = {}
_TOOL_SCHEMAS: dict[str, dict] = {}

_KYC_TOOLS: list[str] = ["fetch_kyc_status", "initiate_kyc", "verify_kyc"]
_PAYMENT_TOOLS: list[str] = ["create_payment_link", "verify_payment"]
_BOOKING_BASE_TOOLS: list[str] = [
    "save_phone_number",
    "get_support_contact",
    "save_visit_time",
    "save_call_time",
    # create_payment_link, verify_payment → moved to _PAYMENT_TOOLS (conditional on PAYMENT_REQUIRED)
    "check_reserve_bed",
    "reserve_bed",
    "cancel_booking",
    "reschedule_booking",
]

_AGENT_TOOLS: dict[str, list[str]] = {
    "default": ["brand_info", "web_search", "get_support_contact"],
    "broker": [
        "save_preferences",
        "save_name",
        "get_support_contact",
        "search_properties",
        "fetch_property_details",
        "shortlist_property",
        "fetch_property_images",
        "fetch_landmarks",
        "estimate_commute",
        "fetch_nearby_places",
        "fetch_room_details",
        "fetch_properties_by_query",
        "show_more_properties",
        "compare_properties",
        "web_search",
    ],
    "booking": _BOOKING_BASE_TOOLS + (_PAYMENT_TOOLS if settings.PAYMENT_REQUIRED else []) + (_KYC_TOOLS if settings.KYC_ENABLED else []) + ["save_preferences", "web_search"],
    "profile": [
        "fetch_profile_details",
        "get_scheduled_events",
        "get_shortlisted_properties",
        "web_search",
    ],
}


def register_tool(name: str, schema: dict, handler: Callable) -> None:
    _TOOL_SCHEMAS[name] = schema
    _TOOL_HANDLERS[name] = handler


def get_schemas_for_agent(agent_name: str) -> list[dict]:
    tool_names = _AGENT_TOOLS.get(agent_name, [])
    return [_TOOL_SCHEMAS[n] for n in tool_names if n in _TOOL_SCHEMAS]


def get_handlers_for_agent(agent_name: str) -> dict[str, Callable]:
    tool_names = _AGENT_TOOLS.get(agent_name, [])
    return {n: _TOOL_HANDLERS[n] for n in tool_names if n in _TOOL_HANDLERS}


def get_all_handlers() -> dict[str, Callable]:
    return dict(_TOOL_HANDLERS)


def get_input_schema(name: str) -> dict | None:
    """Return a tool's JSON input_schema for boundary validation (None if unknown)."""
    schema = _TOOL_SCHEMAS.get(name)
    return schema.get("input_schema") if schema else None


def get_schemas_by_names(tool_names: list[str]) -> list[dict]:
    """Return schemas for specific tool names (for skill-based tool filtering)."""
    return [_TOOL_SCHEMAS[n] for n in tool_names if n in _TOOL_SCHEMAS]


def get_handlers_by_names(tool_names: list[str]) -> dict[str, Callable]:
    """Return handlers for specific tool names (for skill-based tool filtering)."""
    return {n: _TOOL_HANDLERS[n] for n in tool_names if n in _TOOL_HANDLERS}


def init_registry() -> None:
    """Register all tool schemas and handler functions. Call at startup.

    Each tool file owns its schema constant(s):
    - Single-tool files export TOOL_SCHEMA
    - Multi-tool files export individually named constants
    """
    # -- broker --
    from tools.broker.preferences import save_preferences, TOOL_SCHEMA as _save_prefs_schema
    from tools.broker.save_name import save_name, TOOL_SCHEMA as _save_name_schema
    from tools.broker.support_contact import get_support_contact, TOOL_SCHEMA as _support_contact_schema
    from tools.broker.search import search_properties, TOOL_SCHEMA as _search_schema
    from tools.broker.property_details import fetch_property_details, TOOL_SCHEMA as _details_schema
    from tools.broker.shortlist import shortlist_property, TOOL_SCHEMA as _shortlist_schema
    from tools.broker.images import fetch_property_images, TOOL_SCHEMA as _images_schema
    from tools.broker.landmarks import (
        fetch_landmarks, FETCH_LANDMARKS_SCHEMA,
        estimate_commute, ESTIMATE_COMMUTE_SCHEMA,
    )
    from tools.broker.nearby_places import fetch_nearby_places, TOOL_SCHEMA as _nearby_schema
    from tools.broker.room_details import fetch_room_details, TOOL_SCHEMA as _rooms_schema
    from tools.broker.query_properties import fetch_properties_by_query, TOOL_SCHEMA as _query_schema
    from tools.broker.show_more import show_more_properties, TOOL_SCHEMA as _show_more_schema
    from tools.broker.compare import compare_properties, TOOL_SCHEMA as _compare_schema

    # -- common --
    from tools.common.web_search import web_search, TOOL_SCHEMA as _websearch_schema

    # -- default --
    from tools.default.brand_info import brand_info, TOOL_SCHEMA as _brand_schema

    # -- booking --
    from tools.booking.save_phone import save_phone_number, TOOL_SCHEMA as _phone_schema
    from tools.booking.schedule_visit import save_visit_time, TOOL_SCHEMA as _visit_schema
    from tools.booking.schedule_call import save_call_time, TOOL_SCHEMA as _call_schema
    from tools.booking.payment import (
        create_payment_link, CREATE_PAYMENT_LINK_SCHEMA,
        verify_payment, VERIFY_PAYMENT_SCHEMA,
    )
    from tools.booking.reserve import (
        check_reserve_bed, CHECK_RESERVE_BED_SCHEMA,
        reserve_bed, RESERVE_BED_SCHEMA,
    )
    from tools.booking.cancel import cancel_booking, TOOL_SCHEMA as _cancel_schema
    from tools.booking.reschedule import reschedule_booking, TOOL_SCHEMA as _reschedule_schema
    from tools.booking.kyc import (
        fetch_kyc_status, FETCH_KYC_STATUS_SCHEMA,
        initiate_kyc, INITIATE_KYC_SCHEMA,
        verify_kyc, VERIFY_KYC_SCHEMA,
    )

    # -- profile --
    from tools.profile.details import fetch_profile_details, TOOL_SCHEMA as _profile_schema
    from tools.profile.events import get_scheduled_events, TOOL_SCHEMA as _events_schema
    from tools.profile.shortlisted import get_shortlisted_properties, TOOL_SCHEMA as _shortlisted_schema

    # Register all 30 tools: (name, schema, handler)
    register_tool("brand_info",                _brand_schema,            brand_info)
    register_tool("save_preferences",          _save_prefs_schema,       save_preferences)
    register_tool("save_name",                 _save_name_schema,        save_name)
    register_tool("get_support_contact",        _support_contact_schema,  get_support_contact)
    register_tool("search_properties",         _search_schema,           search_properties)
    register_tool("fetch_property_details",    _details_schema,          fetch_property_details)
    register_tool("shortlist_property",        _shortlist_schema,        shortlist_property)
    register_tool("fetch_property_images",     _images_schema,           fetch_property_images)
    register_tool("fetch_landmarks",           FETCH_LANDMARKS_SCHEMA,   fetch_landmarks)
    register_tool("estimate_commute",          ESTIMATE_COMMUTE_SCHEMA,  estimate_commute)
    register_tool("fetch_nearby_places",       _nearby_schema,           fetch_nearby_places)
    register_tool("fetch_room_details",        _rooms_schema,            fetch_room_details)
    register_tool("fetch_properties_by_query", _query_schema,            fetch_properties_by_query)
    register_tool("show_more_properties",      _show_more_schema,        show_more_properties)
    register_tool("compare_properties",        _compare_schema,          compare_properties)
    register_tool("web_search",                _websearch_schema,        web_search)
    register_tool("save_phone_number",         _phone_schema,            save_phone_number)
    register_tool("save_visit_time",           _visit_schema,            save_visit_time)
    register_tool("save_call_time",            _call_schema,             save_call_time)
    register_tool("create_payment_link",       CREATE_PAYMENT_LINK_SCHEMA, create_payment_link)
    register_tool("verify_payment",            VERIFY_PAYMENT_SCHEMA,    verify_payment)
    register_tool("check_reserve_bed",         CHECK_RESERVE_BED_SCHEMA, check_reserve_bed)
    register_tool("reserve_bed",               RESERVE_BED_SCHEMA,       reserve_bed)
    register_tool("cancel_booking",            _cancel_schema,           cancel_booking)
    register_tool("reschedule_booking",        _reschedule_schema,       reschedule_booking)
    register_tool("fetch_kyc_status",          FETCH_KYC_STATUS_SCHEMA,  fetch_kyc_status)
    register_tool("initiate_kyc",              INITIATE_KYC_SCHEMA,      initiate_kyc)
    register_tool("verify_kyc",               VERIFY_KYC_SCHEMA,        verify_kyc)
    register_tool("fetch_profile_details",     _profile_schema,          fetch_profile_details)
    register_tool("get_scheduled_events",      _events_schema,           get_scheduled_events)
    register_tool("get_shortlisted_properties", _shortlisted_schema,     get_shortlisted_properties)
