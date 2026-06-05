import re

import httpx

from config import settings
from core.log import get_logger
from db.redis_store import set_aadhar_user_name, set_aadhar_gender, get_user_phone

logger = get_logger("tools.kyc")


def _kyc_status_ok(body) -> bool:
    """True only when the backend signals genuine success (inner status 200).

    The /checkIn/* KYC endpoints return HTTP 200 even on failure; success is
    carried by an inner ``status`` of 200 (int or str). A string status like
    "error", or a numeric 400/500, is a real failure.
    """
    if not isinstance(body, dict):
        return False
    return body.get("status") in (200, "200")


def _safe_kyc_reason(body) -> str:
    """A short, user-safe reason string drawn from the backend body.

    Surfaces a clean vendor message (e.g. "Invalid Aadhaar Number.") but never
    leaks internal-error noise; falls back to a generic line when unclear.
    """
    msg = ""
    if isinstance(body, dict):
        msg = str(body.get("message") or "").strip()
    if not msg or "internal server error" in msg.lower():
        return "we couldn't verify that with the Aadhaar service."
    return msg if msg.endswith((".", "!", "?")) else msg + "."

FETCH_KYC_STATUS_SCHEMA = {
    "name": "fetch_kyc_status",
    "description": "Check if the user has completed KYC (Aadhaar verification).",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
        "required": [],
    },
}

INITIATE_KYC_SCHEMA = {
    "name": "initiate_kyc",
    "description": "Start KYC process by submitting user's 12-digit Aadhaar number. An OTP will be sent to their registered phone.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "aadhar_number": {"type": "string", "description": "12-digit Aadhaar number"},
        },
        "required": ["aadhar_number"],
    },
}

VERIFY_KYC_SCHEMA = {
    "name": "verify_kyc",
    "description": "Complete KYC by verifying the OTP sent to user's phone.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "otp": {"type": "string", "description": "OTP received by the user"},
        },
        "required": ["otp"],
    },
}


async def fetch_kyc_status(user_id: str, **kwargs) -> str:
    """Check if the user has completed KYC verification."""
    # Initiate KYC status entry
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.get(
                f"{settings.RENTOK_API_BASE_URL}/bookingBotKyc/user-kyc/{user_id}"
            )
    except Exception as e:
        logger.warning("KYC init failed for user=%s: %s", user_id, e)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{settings.RENTOK_API_BASE_URL}/bookingBotKyc/booking/{user_id}/kyc-status"
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return f"Error checking KYC status: {str(e)}"

    kyc_status = data.get("data", {}).get("kyc_status", 0)

    if kyc_status == 1:
        return "KYC verification successful — user is verified."
    return "KYC verification required. Please provide your 12-digit Aadhaar number to begin."


async def initiate_kyc(user_id: str, aadhar_number: str, **kwargs) -> str:
    """Start KYC by generating OTP for the Aadhaar number."""
    aadhar_clean = re.sub(r"\s+", "", aadhar_number)
    if not re.match(r"^\d{12}$", aadhar_clean):
        return "Invalid Aadhaar number. It must be exactly 12 digits."

    phone = get_user_phone(user_id)
    if not phone:
        return (
            "I need your mobile number to send the Aadhaar OTP. "
            "Please share your 10-digit Indian mobile number first."
        )

    try:
        # 25s (not 15s): a REAL Aadhaar triggers an actual UIDAI OTP dispatch via
        # QuickEkyc that can exceed 15s — the old cap made the bot falsely report
        # "couldn't reach" even when the OTP was sent server-side. Kept under the
        # 30s per-tool ceiling (core.tool_boundary.TOOL_TIMEOUT_SECONDS).
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.post(
                f"{settings.RENTOK_API_BASE_URL}/checkIn/generateAadharOTP",
                json={
                    "aadhar_number": aadhar_clean,
                    "user_phone_number": phone,
                },
            )
            resp.raise_for_status()
            body = resp.json()
    except Exception as e:
        logger.warning("KYC generate failed for user=%s: %s", user_id, e)
        return "I couldn't reach the verification service just now. Please try again in a moment."

    # The backend returns HTTP 200 even when QuickEkyc REJECTS the Aadhaar
    # (e.g. {"status":"error","status_code":500,"message":"Invalid Aadhaar Number."}).
    # Success is signalled only by an inner status of 200; treating any 200 HTTP
    # response as "OTP sent" silently lies to the user, who then waits for an OTP
    # that was never dispatched. Inspect the body and surface failures honestly.
    if not _kyc_status_ok(body):
        return (
            f"That didn't work — {_safe_kyc_reason(body)} "
            "Please double-check your Aadhaar number and send it again."
        )

    return "An OTP has been sent to the mobile number linked with your Aadhaar. Please share the OTP to complete verification."


async def verify_kyc(user_id: str, otp: str, **kwargs) -> str:
    """Verify the OTP to complete KYC."""
    otp_clean = otp.strip()
    if not otp_clean:
        return "Please provide the OTP."

    phone = get_user_phone(user_id)
    if not phone:
        return "Phone number required for KYC verification. Please save your mobile number first using the save_phone_number tool."

    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.post(
                f"{settings.RENTOK_API_BASE_URL}/checkIn/verifyAadharOTP",
                json={"otp": otp_clean, "user_phone_number": phone},
            )
            resp_data = resp.json()
    except Exception as e:
        logger.warning("KYC verify failed for user=%s: %s", user_id, e)
        return "I couldn't reach the verification service just now. Please try again in a moment."

    # Success is ONLY an inner status of 200. The backend returns {status:400}
    # for a wrong/expired OTP and {status:500} on its own error — both arrive as
    # HTTP 200, so anything other than an explicit 200 must be treated as a
    # failure, never silently passed through as "verified".
    if not _kyc_status_ok(resp_data):
        return (
            f"OTP verification failed — {_safe_kyc_reason(resp_data)} "
            "Please re-enter the OTP, or ask me to resend it."
        )

    # Update KYC status in backend
    kyc_data = resp_data.get("data", {})
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"{settings.RENTOK_API_BASE_URL}/bookingBotKyc/update-kyc",
                json={"user_id": user_id, "kyc_data": kyc_data},
            )
    except Exception as e:
        logger.warning("KYC update failed for user=%s: %s", user_id, e)
        return f"OTP verified but KYC status update failed — please contact support. Error: {str(e)}"

    # Store Aadhaar name and gender in Redis
    name = kyc_data.get("name", "")
    gender = kyc_data.get("gender", "")
    if name:
        set_aadhar_user_name(user_id, name)
    if gender:
        set_aadhar_gender(user_id, gender)

    return f"KYC verification successful! Welcome, {name}." if name else "KYC verification successful!"
