"""
core/accounts.py — Self-serve brand signup + native email/password login.

Signup: validate → ensure the email is free → generate a unique brand api_key →
provision a demo brand → store the account (password sha256'd) → issue an email
verification token. Login: look up the account, constant-time password check,
return the brand api_key the panel sends as X-API-Key.
"""

import hashlib
import hmac
import re
import secrets
import time

from config import settings
from core.log import get_logger
from core.demo_brand import provision_demo_brand
from db.redis.accounts import (
    account_exists, get_account, save_account,
    set_email_verify_token, consume_email_verify_token,
)
from db.redis.brand import _brand_hash, get_brand_config

logger = get_logger("accounts")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 8

# scrypt work factors (RFC 7914 interactive-login profile). Salted + memory-hard
# so stored hashes are not offline-crackable like a bare SHA-256 digest would be.
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def _hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """Return (hash_hex, salt_hex) for a password using a per-user random salt."""
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(
        (password or "").encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
    )
    return dk.hex(), salt.hex()


def _verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    """Constant-time check of a password against a stored scrypt hash + salt."""
    if not salt_hex or not hash_hex:
        return False
    candidate, _ = _hash_password(password or "", bytes.fromhex(salt_hex))
    return hmac.compare_digest(candidate, hash_hex)


def _generate_api_key() -> str:
    """Random, unguessable brand key the panel sends as X-API-Key."""
    return "eapg_" + secrets.token_urlsafe(24)


def check_signup_rate(client_ip: str) -> bool:
    """Increment the per-IP signup counter; return True if still within the limit.

    Fail-open on a Redis hiccup (consistent with the admin-login throttle): never
    block a legitimate signup because Redis blipped.
    """
    try:
        from db.redis._base import _r

        key = f"signup_rate:{client_ip}"
        r = _r()
        n = r.incr(key)
        if n == 1:
            r.expire(key, settings.SIGNUP_RATE_WINDOW_SECONDS)
        return n <= settings.SIGNUP_MAX_PER_WINDOW
    except Exception:
        return True


def _login_ip_key(client_ip: str) -> str:
    return f"login_ip_fail:{client_ip}"


def login_ip_throttled(client_ip: str) -> bool:
    """True if this IP has exceeded the failed-login limit within the lockout window.

    Fail-open on a Redis hiccup — never lock out a real user because Redis blipped.
    """
    try:
        from db.redis._base import _r

        raw = _r().get(_login_ip_key(client_ip))
        return bool(raw) and int(raw) >= settings.LOGIN_IP_MAX_FAILS
    except Exception:
        return False


def record_login_ip_failure(client_ip: str) -> None:
    """Count one failed login against this IP; arm the lockout window on first miss."""
    try:
        from db.redis._base import _r

        key = _login_ip_key(client_ip)
        r = _r()
        n = r.incr(key)
        if n == 1:
            r.expire(key, settings.ADMIN_LOGIN_THROTTLE_SECONDS)
    except Exception:
        pass


def clear_login_ip_failures(client_ip: str) -> None:
    """Reset this IP's failed-login counter after a successful login."""
    try:
        from db.redis._base import _r

        _r().delete(_login_ip_key(client_ip))
    except Exception:
        pass


def validate_signup(email: str, password: str) -> str | None:
    """Return an error string on invalid input, or None if OK."""
    if not email or not _EMAIL_RE.match(email.strip()):
        return "Enter a valid email address."
    if not password or len(password) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    return None


def signup(email: str, password: str, brand_name: str = "") -> tuple[dict | None, str]:
    """Create an account + demo brand. Returns (result, reason).

    result on success: {api_key, brand_hash, brand_link_token, verify_token, email}.
    reason: "ok" | "invalid:<msg>" | "exists".
    """
    err = validate_signup(email, password)
    if err:
        return None, f"invalid:{err}"
    email_norm = email.strip().lower()
    if account_exists(email_norm):
        return None, "exists"

    api_key = _generate_api_key()
    demo = provision_demo_brand(api_key, brand_name)  # persists brand_config first
    brand_hash = _brand_hash(api_key)

    password_hash, password_salt = _hash_password(password)
    save_account({
        "email": email_norm,
        "password_hash": password_hash,
        "password_salt": password_salt,
        "api_key": api_key,
        "brand_hash": brand_hash,
        "email_verified": False,
        "created_at": int(time.time()),
    })

    verify_token = secrets.token_urlsafe(24)
    set_email_verify_token(verify_token, email_norm)
    logger.info("Self-serve signup: %s (brand_hash=%s)", email_norm, brand_hash)

    return {
        "api_key": api_key,
        "brand_hash": brand_hash,
        "brand_link_token": demo["brand_link_token"],
        "verify_token": verify_token,
        "email": email_norm,
    }, "ok"


def verify_login(email: str, password: str) -> tuple[str | None, str]:
    """Native email/password login. Returns (api_key, reason).

    reason: "ok" | "invalid" | "misconfigured".
    """
    account = get_account(email)
    if not account:
        return None, "invalid"
    if not _verify_password(password or "", account.get("password_salt", ""), account.get("password_hash", "")):
        return None, "invalid"
    api_key = account.get("api_key", "")
    if not get_brand_config(api_key):
        logger.error("Account %s has no brand config — provisioning drift", email)
        return None, "misconfigured"
    return api_key, "ok"


def verify_email(token: str) -> bool:
    """Consume a verification token and flip the account's email_verified. False if bad/expired."""
    email = consume_email_verify_token(token)
    if not email:
        return False
    account = get_account(email)
    if not account:
        return False
    account["email_verified"] = True
    save_account(account)
    return True


def send_verification_email(email: str, token: str) -> None:
    """Pluggable delivery. Defaults to LOGGING the link — no email provider wired in v1.

    Swap this body for a real provider (Resend/SES/etc.) when delivery is needed.
    """
    from config import settings
    link = f"{settings.ADMIN_BASE_URL}/verify-email?token={token}"
    logger.info("[email-verify] %s -> %s", email, link)
