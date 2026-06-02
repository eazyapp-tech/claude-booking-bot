import httpx
import uuid

from config import settings
from core.log import get_logger
from db.redis_store import get_property_info_map, get_account_values, track_funnel, get_user_phone, get_aadhar_user_name, get_user_memory, record_visit_scheduled, schedule_followup, get_user_brand, track_property_event
from core.followup import create_followup_state
from utils.api import user_error
from utils.date import transcribe_date
from utils.properties import find_property as _find_property
from utils.retry import http_post

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

    # Bug fix: 'and' was wrong — 200 + success:false would fall through silently.
    # Now: any non-success body is treated as a failure regardless of HTTP status.
    if not data.get("success"):
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
            return (
                f"We received your visit request for '{prop_display}' on {visit_date} at {visit_time}, "
                f"but ran into a technical issue confirming it with the property team. "
                f"Our team will reach out to you{' on ' + phone if phone else ''} to confirm. "
                f"We apologize for the inconvenience!"
            )

    return (
        f"Visit scheduled successfully for '{prop_display}' "
        f"on {visit_date} at {visit_time} ({visit_type}).{location_info}"
    )


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

    phone = get_user_phone(user_id) or ""
    name = get_aadhar_user_name(user_id)
    if not name:
        mem = get_user_memory(user_id) or {}
        name = mem.get("profile_name") or mem.get("name") or phone or "Guest"

    payload = {
        "eazypg_id": eazypg_id,
        "phone": phone,
        "name": name,
        "gender": gender,
        "rent_range": budget,
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
