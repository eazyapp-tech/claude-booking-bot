import asyncio

from config import settings
from core.log import get_logger
from db.redis_store import (
    get_property_info_map,
    set_payment_info,
    get_payment_info,
    clear_payment_info,
    track_funnel,
    get_user_phone,
    get_aadhar_user_name,
    schedule_followup,
    cancel_followups,
    get_user_brand,
)
from utils.api import check_rentok_response, RentokAPIError, user_error
from utils.properties import find_property as _find_property
from utils.retry import http_get, http_post

logger = get_logger("tools.payment")

# A brand-new tenant's CRM lead is not immediately resolvable to a tenant_uuid:
# get-tenant_uuid can return empty for a second or two right after the lead is
# created. Without a retry, a FIRST-time reservation fails the payment link (the
# tenant exists on the next attempt). Retry a few times with a short backoff,
# kept well under the 30s per-tool ceiling (core.tool_boundary.TOOL_TIMEOUT_SECONDS).
_UUID_RETRIES = 3
_UUID_RETRY_DELAY = 1.5  # seconds between retries → ≤4.5s added latency

CREATE_PAYMENT_LINK_SCHEMA = {
    "name": "create_payment_link",
    "description": "Generate a payment link for the token amount to reserve a bed/room.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "property_name": {"type": "string", "description": "Exact property name"},
        },
        "required": ["property_name"],
    },
}

VERIFY_PAYMENT_SCHEMA = {
    "name": "verify_payment",
    "description": "Verify and record a completed payment for a property reservation.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
        "required": [],
    },
}


async def create_payment_link(user_id: str, property_name: str, **kwargs) -> str:
    # Phone validation first — required before any property lookup or API call
    phone = get_user_phone(user_id)
    if not phone:
        return (
            "I need your mobile number to generate a payment link. "
            "Please share your 10-digit Indian mobile number and I'll proceed right away!"
        )

    prop = _find_property(user_id, property_name)
    if not prop:
        return f"Property '{property_name}' not found."

    eazypg_id = prop.get("eazypg_id", "")
    pg_id = prop.get("pg_id", "")
    pg_number = prop.get("pg_number", "")
    amount = prop.get("property_min_token_amount", 0) or 1000

    # Fetch tenant UUID — create lead if tenant doesn't exist yet
    tenant_uuid = ""
    try:
        uuid_data = await http_get(
            f"{settings.RENTOK_API_BASE_URL}/tenant/get-tenant_uuid",
            params={"phone": phone, "eazypg_id": eazypg_id},
        )
        check_rentok_response(uuid_data, "get-tenant_uuid")
        tenant_uuid = uuid_data.get("data", {}).get("tenant_uuid", "")
    except (RentokAPIError, Exception) as e:
        logger.warning("tenant UUID fetch failed for user=%s eazypg_id=%s: %s", user_id, eazypg_id, e)

    # If no UUID yet, create a lead first, then poll for the tenant_uuid. The
    # CRM is not read-after-write consistent here, so one immediate re-fetch
    # often still returns empty for a brand-new tenant — retry with a short
    # backoff instead of giving up on the user's first reservation.
    if not tenant_uuid:
        try:
            from tools.booking.schedule_visit import _create_external_lead

            await _create_external_lead(
                user_id, eazypg_id, pg_id, pg_number, "", "", "",
            )
        except Exception as e2:
            return user_error("generate the payment link", e2, logger=logger)

        for _attempt in range(_UUID_RETRIES):
            await asyncio.sleep(_UUID_RETRY_DELAY)
            try:
                uuid_data = await http_get(
                    f"{settings.RENTOK_API_BASE_URL}/tenant/get-tenant_uuid",
                    params={"phone": phone, "eazypg_id": eazypg_id},
                )
                tenant_uuid = uuid_data.get("data", {}).get("tenant_uuid", "")
            except Exception as e3:
                logger.warning(
                    "tenant UUID retry %d/%d failed for user=%s: %s",
                    _attempt + 1, _UUID_RETRIES, user_id, e3,
                )
                continue
            if tenant_uuid:
                break

    if not tenant_uuid:
        return "Could not generate payment link. Please try again in a moment."

    # Generate payment link
    try:
        data = await http_get(
            f"{settings.RENTOK_API_BASE_URL}/tenant/{tenant_uuid}/lead-payment-link",
            params={"pg_id": pg_id, "pg_number": pg_number, "amount": amount},
        )
        check_rentok_response(data, "lead-payment-link")
    except (RentokAPIError, Exception) as e:
        return user_error("generate the payment link", e, logger=logger)

    link_subs = data.get("data", {}).get("link", "")
    pg_name = data.get("data", {}).get("pg_name", prop.get("property_name", property_name))

    if not link_subs:
        return "Could not generate payment link. Please try again."

    set_payment_info(user_id, pg_name, pg_id, pg_number, str(amount), link_subs)
    link = f"https://pay.rentok.com/p/{link_subs}"

    # Schedule follow-up: 24h after payment link creation
    try:
        schedule_followup(user_id, "payment_pending", {
            "property_name": pg_name,
            "pg_id": pg_id,
            "amount": str(amount),
            "link": link,
        }, 86400)  # 24 hours
    except Exception as e:
        logger.warning("payment follow-up scheduling failed: %s", e)

    return f"Payment link generated for {pg_name}: {link}\nToken amount: Rs. {amount}. Please complete the payment and let me know once done."


async def verify_payment(user_id: str, **kwargs) -> str:
    payment_info = get_payment_info(user_id)
    if not payment_info:
        return "No pending payment found. Please generate a payment link first."

    pg_name = payment_info.get("pg_name", "")
    pg_id = payment_info.get("pg_id", "")
    pg_number = payment_info.get("pg_number", "")
    amount = payment_info.get("amount", "")
    link_subs = payment_info.get("short_link", "")

    # Record payment in backend. /bookingBot/addPayment returns HTTP 200 with a
    # `status` field in the body even when the insert fails (status:500), so a
    # clean HTTP status is not proof — inspect the body and never report success
    # on a backend rejection. user_id column is `text` (no length limit) and the
    # table is write-only, so send the full id, not a truncated one.
    try:
        add_resp = await http_post(
            f"{settings.RENTOK_API_BASE_URL}/bookingBot/addPayment",
            json={
                "user_id": user_id,
                "pg_id": pg_id,
                "pg_number": pg_number,
                "amount": amount,
                "short_link": link_subs,
            },
        )
    except Exception as e:
        logger.error("addPayment API failed for user=%s pg_id=%s: %s", user_id, pg_id, e)
        return "Payment recording failed — please contact support to confirm your payment was received."

    if isinstance(add_resp, dict):
        status = add_resp.get("status")
        rejected = (
            add_resp.get("success") is False
            or (isinstance(status, int) and status >= 400)
            or (isinstance(status, str) and status.isdigit() and int(status) >= 400)
            or (isinstance(status, str) and status.lower() == "error")
        )
        if rejected:
            logger.error(
                "addPayment rejected by backend for user=%s pg_id=%s: %s",
                user_id, pg_id, add_resp.get("message", add_resp),
            )
            return "Payment recording failed — please contact support to confirm your payment was received."

    # Update lead status to Token
    info_map = get_property_info_map(user_id)
    eazypg_id = ""
    for p in info_map:
        if p.get("pg_id") == pg_id and str(p.get("pg_number", "")) == str(pg_number):
            eazypg_id = p.get("eazypg_id", "")
            break

    # Update lead status to Token in CRM — required for property owner visibility.
    # Payment is already recorded above; this is a secondary CRM update.
    # If it fails, still clear state and return a partial-failure message so the user
    # is not told "success" when the owner's dashboard won't reflect the token status.
    lead_token_ok = True
    if eazypg_id:
        try:
            from datetime import datetime
            from db.redis_store import get_aadhar_gender, get_preferences

            gender = get_aadhar_gender(user_id) or "Any"
            prefs = get_preferences(user_id)
            budget = prefs.get("min_budget") or prefs.get("max_budget", "")
            phone = get_user_phone(user_id) or ""
            name = get_aadhar_user_name(user_id) or phone or "Guest"

            await http_post(
                f"{settings.RENTOK_API_BASE_URL}/tenant/addLeadFromEazyPGID",
                json={
                    "eazypg_id": eazypg_id,
                    "phone": phone,
                    "name": name,
                    "gender": gender,
                    "rent_range": budget,
                    # Canonical bot source — C1 get-tenant_uuid resolves only this.
                    "lead_source": "bookingBot00",
                    "visit_date": "",
                    "visit_time": "",
                    "visit_type": "",
                    "lead_status": "Token",
                    "firebase_id": f"cust_{datetime.now().strftime('%Y_%m_%d_%H_%M_%S')}",
                },
            )
        except Exception as e:
            logger.error(
                "lead Token update failed after payment success — user=%s eazypg_id=%s: %s",
                user_id, eazypg_id, e,
            )
            lead_token_ok = False

    # Always clear state — payment is recorded regardless of CRM status
    clear_payment_info(user_id)
    brand_hash = get_user_brand(user_id)
    track_funnel(user_id, "booking", brand_hash=brand_hash)
    track_funnel(user_id, "payment_completed", brand_hash=brand_hash)
    cancel_followups(user_id, "payment_pending")

    if eazypg_id and not lead_token_ok:
        phone = get_user_phone(user_id) or ""
        return (
            f"Your payment for {pg_name} has been received, "
            f"but we ran into a technical issue updating your booking status with the property team. "
            f"Our team will reach out to you{' on ' + phone if phone else ''} to confirm. "
            f"We apologize for the inconvenience! You can still proceed with bed reservation."
        )

    return f"Payment verified successfully for {pg_name}. You can now proceed with bed reservation."
