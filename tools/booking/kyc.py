import re

import httpx

from config import settings
from core.log import get_logger
from db.redis_store import set_aadhar_user_name, set_aadhar_gender, get_user_phone

logger = get_logger("tools.kyc")

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
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{settings.RENTOK_API_BASE_URL}/checkIn/generateAadharOTP",
                json={
                    "aadhar_number": aadhar_clean,
                    "user_phone_number": phone,
                },
            )
            resp_data = resp.json()
    except Exception as e:
        return f"Error initiating KYC: {str(e)}"

    # The generate endpoint returns HTTP 200 even on failure, with the real
    # outcome in the body status (mirrors verify_kyc below). Only a body
    # status of 200 means the OTP was actually sent.
    if resp_data.get("status") != 200:
        reason = resp_data.get("message", "please try again in a few minutes")
        return f"Couldn't send the OTP: {reason}. Please re-check your Aadhaar number or try again shortly."

    return "OTP has been sent to the mobile number linked with your Aadhaar. Please share the OTP to complete verification."


async def verify_kyc(user_id: str, otp: str, **kwargs) -> str:
    """Verify the OTP to complete KYC."""
    otp_clean = otp.strip()
    if not otp_clean:
        return "Please provide the OTP."

    phone = get_user_phone(user_id)
    if not phone:
        return "Phone number required for KYC verification. Please save your mobile number first using the save_phone_number tool."

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{settings.RENTOK_API_BASE_URL}/checkIn/verifyAadharOTP",
                json={"otp": otp_clean, "user_phone_number": phone},
            )
            resp_data = resp.json()
    except Exception as e:
        return f"Error verifying OTP: {str(e)}"

    if resp_data.get("status") == 400:
        return f"OTP verification failed: {resp_data.get('message', 'Invalid OTP')}. Please try again."

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
