from config import settings
from core.log import get_logger
from db.redis_store import get_user_phone
from utils.api import user_error
from utils.date import transcribe_date
from utils.properties import find_property as _find_property
from utils.retry import http_post

logger = get_logger("tools.schedule_call")


TOOL_SCHEMA = {
    "name": "save_call_time",
    "description": "Schedule a phone call or video tour with a property. Available 10 AM - 9 PM, next 7 days.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "property_name": {"type": "string", "description": "Exact property name"},
            "visit_date": {"type": "string", "description": "Date as stated by user"},
            "visit_time": {"type": "string", "description": "Time as stated by user"},
            "visit_type": {"type": "string", "description": "'Phone Call' or 'Video Tour'"},
        },
        "required": ["property_name", "visit_date", "visit_time"],
    },
}


async def save_call_time(
    user_id: str,
    property_name: str,
    visit_date: str,
    visit_time: str,
    visit_type: str = "Phone Call",
    **kwargs,
) -> str:
    # Phone gate — must have phone before scheduling a call
    if not get_user_phone(user_id):
        return "I need your phone number before I can schedule a call. Please share your mobile number (e.g., 9876543210)."

    prop = _find_property(user_id, property_name)
    if not prop:
        return f"Property '{property_name}' not found. Please provide the correct property name."

    property_id = prop.get("property_id", "")
    if not property_id:
        return "Property ID not available."

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
            return "There is already a scheduled booking for this property or on the same date. Would you like to see your scheduled events?"
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return user_error(f"schedule your {visit_type.lower()}", e, logger=logger)

    # Bug fix: 'and' was wrong — 200 + success:false would fall through silently.
    # Now: any non-success body is treated as a failure regardless of HTTP status.
    if not data.get("success"):
        msg = data.get("message", "unknown error")
        return f"Booking failed: {msg}. Please try again."

    prop_display = prop.get("property_name", property_name)
    phone = get_user_phone(user_id) or ""

    # Create external CRM lead — required for property owner visibility.
    # If this fails, the booking record exists internally but the owner won't see it,
    # so we surface a partial-failure message instead of a false "success".
    eazypg_id = prop.get("eazypg_id", "")
    pg_id = prop.get("pg_id", "")
    pg_number = prop.get("pg_number", "")
    if eazypg_id:
        from tools.booking.schedule_visit import _create_external_lead

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
                f"We received your {visit_type.lower()} request for '{prop_display}' on {visit_date} at {visit_time}, "
                f"but ran into a technical issue confirming it with the property team. "
                f"Our team will reach out to you{' on ' + phone if phone else ''} to confirm. "
                f"We apologize for the inconvenience!"
            )

    return (
        f"{visit_type} scheduled successfully for '{prop_display}' "
        f"on {visit_date} at {visit_time}."
    )
