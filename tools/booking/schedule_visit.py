import httpx
import uuid

from config import settings
from core.log import get_logger
from core.signals import record_signal
from db.redis_store import get_property_info_map, get_account_values, track_funnel, get_user_phone, get_aadhar_user_name, get_user_memory, record_visit_scheduled, schedule_followup, get_user_brand, track_property_event
from core.followup import create_followup_state
from utils.api import user_error
from utils.date import transcribe_date
from utils.properties import find_property as _find_property
from utils.retry import http_post
from tools.booking.notify_manager import fire_booking_notification

logger = get_logger("tools.schedule_visit")


def _normalize_visit_time(s: str) -> str:
    """Try multiple time formats, return normalized '%I:%M %p' or original string."""
    from datetime import datetime
    for fmt in ('%I:%M %p', '%I:%M%p', '%I %p', '%H:%M', '%I:%M'):
        try:
            return datetime.strptime(s.strip(), fmt).strftime('%I:%M %p')
        except ValueError:
            continue
    return s  # passthrough if no format matches


TOOL_SCHEMA = {
    "name": "save_visit_time",
    "description": "Schedule a physical visit to a property. Visits available 9 AM - 5 PM, next 7 days, 30-minute slots.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "property_name": {"type": "string", "description": "Exact property name"},
            "visit_date": {"type": "string", "description": "Visit date as stated by user"},
            "visit_time": {"type": "string", "description": "Visit time as stated by user"},
            "visit_type": {"type": "string", "description": "Always 'Physical visit'"},
        },
        "required": ["property_name", "visit_date", "visit_time"],
    },
}


async def save_visit_time(
    user_id: str,
    property_name: str,
    visit_date: str,
    visit_time: str,
    visit_type: str = "Physical visit",
    **kwargs,
) -> str:
    # Phone gate — must have phone before scheduling a visit
    if not get_user_phone(user_id):
        return "I need your phone number before I can schedule a visit. Please share your mobile number (e.g., 9876543210)."

    prop = _find_property(user_id, property_name)
    if not prop:
        return f"Property '{property_name}' not found. Please provide the correct property name."

    property_id = prop.get("property_id", "")
    if not property_id:
        return "Property ID not available."

    # Normalise date (Claude should pass DD/MM/YYYY but handle natural language too)
    visit_date = transcribe_date(visit_date)
    if not visit_date:
        return "I couldn't understand that date. Please say something like 'tomorrow', '15 March', or '25/03/2026'."

    try:
        resp = await http_post(
            f"{settings.RENTOK_API_BASE_URL}/bookingBot/add-booking",
            json={
                "user_id": user_id,
                "property_id": property_id,
                "visit_date": visit_date,
                "visit_time": visit_time,
                "visit_type": visit_type,
                "property_name": prop.get("property_name", property_name),
            },
            raw=True,
        )
        if resp.status_code == 400:
            return "There is already a scheduled visit for this property or a visit on the same date. Would you like to see your scheduled visits?"
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 400:
            return "There is already a scheduled visit for this property or a visit on the same date. Would you like to see your scheduled visits?"
        return user_error("schedule your visit", e, logger=logger)
    except Exception as e:
        return user_error("schedule your visit", e, logger=logger)

    # /bookingBot/add-booking signals success via INNER status==200 (HTTP is 200)
    # and carries NO top-level `success` key — verified contract. The old
    # `not data.get("success")` reported failure on every genuine save (the UAT
    # P0: bot said "booking failed" while Rentok persisted the booking → "already
    # scheduled" on retry, and track_funnel("visit") below never fired). Inner
    # status 400 = dedup (a booking already exists); inner 500 = real error.
    inner_status = data.get("status") if isinstance(data, dict) else None
    if inner_status in (400, "400"):
        return "There is already a scheduled visit for this property or a visit on the same date. Would you like to see your scheduled visits?"
    ok = data.get("success") is True or inner_status in (200, "200")
    if not ok:
        msg = data.get("message", "unknown error")
        return f"Booking failed: {msg}. Please try again."

    prop_lat = prop.get("property_lat", "")
    prop_long = prop.get("property_long", "")
    maps_link = f"https://www.google.com/maps?q={prop_lat},{prop_long}" if prop_lat and prop_long else ""
    location_info = f"\nLocation: {maps_link}" if maps_link else ""

    brand_hash_val = get_user_brand(user_id)
    track_funnel(user_id, "visit", brand_hash=brand_hash_val)
    record_visit_scheduled(user_id, property_id)
    try:
        track_property_event(property_id, "visit_scheduled", brand_hash=brand_hash_val)
    except Exception:
        pass

    # Schedule follow-up: 2 hours after the visit time
    try:
        from datetime import datetime as _dt
        normalized_time = _normalize_visit_time(visit_time)
        visit_dt = _dt.strptime(f"{visit_date} {normalized_time}", "%d/%m/%Y %I:%M %p")
        seconds_until_visit = max(0, (visit_dt - _dt.now()).total_seconds())
        followup_delay = int(seconds_until_visit) + 7200  # visit time + 2h
        schedule_followup(user_id, "visit_complete", {
            "property_name": prop.get("property_name", property_name),
            "property_id": property_id,
            "visit_date": visit_date,
            "visit_time": visit_time,
        }, followup_delay)
        # Create multi-step follow-up state for the state machine (Sprint 2)
        create_followup_state(
            user_id, property_id,
            prop.get("property_name", property_name),
            visit_dt.isoformat(),
            brand_hash=get_user_brand(user_id),
        )
    except Exception as e:
        logger.error("follow-up scheduling failed: %s", e)

    # Create external CRM lead — required for property owner visibility.
    # If this fails, the booking record exists internally but the owner won't see it,
    # so we surface a partial-failure message instead of a false "success".
    eazypg_id = prop.get("eazypg_id", "")
    pg_id = prop.get("pg_id", "")
    pg_number = prop.get("pg_number", "")
    prop_display = prop.get("property_name", property_name)
    phone = get_user_phone(user_id) or ""

    if not eazypg_id:
        logger.warning("skip CRM lead: no eazypg_id for user=%s property=%s", user_id, property_id)
    if eazypg_id:
        lead_ok = await _create_external_lead(
            user_id, eazypg_id, pg_id, pg_number,
            visit_date, visit_time, visit_type,
        )
        if not lead_ok:
            logger.error(
                "lead creation failed after booking success — user=%s eazypg_id=%s property=%s",
                user_id, eazypg_id, prop_display,
            )
            # Booking landed internally but CRM sync failed → honest partial receipt.
            record_signal(booking_held=True, crm_synced=False)
            return (
                f"We received your visit request for '{prop_display}' on {visit_date} at {visit_time}, "
                f"but ran into a technical issue confirming it with the property team. "
                f"Our team will reach out to you{' on ' + phone if phone else ''} to confirm. "
                f"We apologize for the inconvenience!"
            )

    record_signal(booking_held=True, crm_synced=True)

    # SILO — tell the manager a visit just landed (owner + team FCM). Background
    # fire-and-forget (reached only here, after a confirmed booking + CRM lead); it
    # never delays or breaks the user's confirmation.
    fire_booking_notification("visit", user_id, pg_id, pg_number, prop_display, visit_date, visit_time)

    return (
        f"Visit scheduled successfully for '{prop_display}' "
        f"on {visit_date} at {visit_time} ({visit_type}).{location_info}"
    )


def _build_lead_remarks(prefs: dict, memory: dict) -> str:
    """Distill the user's captured intent into ONE compact, manager-readable line.

    Persisted backend-side to Tenant.lead_remarks and surfaced to the property
    manager as the lead's `comments` field (and `Notes` in reports) — verified
    against rentok-backend. Without this, the manager receives a bare name+date
    and has no idea *why* the lead matters. Only fields that are actually present
    are emitted, so a sparse profile never produces dangling fragments.
    """
    prefs = prefs or {}
    memory = memory or {}
    parts = []

    min_b = prefs.get("min_budget")
    max_b = prefs.get("max_budget")
    if min_b and max_b:
        parts.append(f"Budget ₹{min_b}–{max_b}")
    elif max_b:
        parts.append(f"Budget up to ₹{max_b}")
    elif min_b:
        parts.append(f"Budget from ₹{min_b}")

    if prefs.get("location"):
        parts.append(f"Area: {prefs['location']}")

    move_in = prefs.get("move_in_date") or memory.get("move_in_date")
    if move_in:
        parts.append(f"Move-in: {move_in}")

    if prefs.get("property_type"):
        parts.append(f"Type: {prefs['property_type']}")

    sharing = prefs.get("unit_types_available") or prefs.get("sharing")
    if sharing:
        parts.append(f"Sharing: {sharing}")

    must_have = prefs.get("must_have_amenities") or prefs.get("amenities")
    if must_have:
        parts.append(f"Must-have: {must_have}")

    deal_breakers = memory.get("deal_breakers")
    if deal_breakers:
        db_str = ", ".join(deal_breakers) if isinstance(deal_breakers, (list, tuple)) else str(deal_breakers)
        if db_str.strip():
            parts.append(f"Avoid: {db_str}")

    parts.append("via EazyPG AI assistant")
    return " | ".join(parts)


async def _create_external_lead(
    user_id: str,
    eazypg_id: str,
    pg_id: str,
    pg_number: str,
    visit_date: str,
    visit_time: str,
    visit_type: str,
) -> bool:
    """Create an external CRM lead entry. Returns True on success, False on any failure."""
    from db.redis_store import get_preferences, get_aadhar_gender

    gender = get_aadhar_gender(user_id) or "Any"
    prefs = get_preferences(user_id)
    budget = prefs.get("min_budget") or prefs.get("max_budget", "")
    mem_ctx = get_user_memory(user_id) or {}
    room_type = prefs.get("unit_types_available") or prefs.get("sharing") or ""
    remarks = _build_lead_remarks(prefs, mem_ctx)

    phone = get_user_phone(user_id) or ""
    name = get_aadhar_user_name(user_id)
    if not name:
        name = mem_ctx.get("profile_name") or mem_ctx.get("name") or phone or "Guest"

    payload = {
        "eazypg_id": eazypg_id,
        "phone": phone,
        "name": name,
        "gender": gender,
        "rent_range": budget,
        "room_type": room_type,
        # Manager-facing context — persisted to Tenant.lead_remarks, shown as the
        # lead's `comments`. The bot's one lever on the silo: the manager sees the
        # real intent (budget, area, must-haves, deal-breakers) instead of a bare lead.
        "remarks": remarks,
        # Ground truth (RentOk backend): C1 GET /tenant/get-tenant_uuid resolves
        # leads ONLY where lead_source="bookingBot00" (AND status=3). Any other
        # source string is invisible to the payment-link flow, so the bot MUST
        # stamp its leads with this exact canonical value — not a display name.
        "lead_source": "bookingBot00",
        "visit_date": visit_date,
        "visit_time": visit_time,
        "visit_type": visit_type,
        "lead_status": "Visit Scheduled",
        "firebase_id": str(uuid.uuid4()),
    }
    try:
        data = await http_post(
            f"{settings.RENTOK_API_BASE_URL}/tenant/addLeadFromEazyPGID",
            json=payload,
        )
        # The CRM returns 200 even when it rejects the lead, so a clean HTTP
        # status is not proof of success — inspect the body. Only an *explicit*
        # failure marker counts as a failure; success shapes that omit a flag
        # (the common case) still pass, so we never raise a false negative.
        if isinstance(data, dict):
            status = data.get("status")
            failed = (
                data.get("success") is False
                or (isinstance(status, int) and status >= 400)
                or (isinstance(status, str) and status.lower() == "error")
            )
            if failed:
                logger.error(
                    "lead creation rejected by CRM for user=%s eazypg_id=%s: %s",
                    user_id, eazypg_id, data.get("message", data),
                )
                return False
        return True
    except Exception as e:
        logger.error("lead creation failed for user=%s eazypg_id=%s: %s", user_id, eazypg_id, e)
        return False
