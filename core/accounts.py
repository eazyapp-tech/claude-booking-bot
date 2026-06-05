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


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _generate_api_key() -> str:
    """Random, unguessable brand key the panel sends as X-API-Key."""
    return "eapg_" + secrets.token_urlsafe(24)


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

    save_account({
        "email": email_norm,
        "password_sha256": _sha256_hex(password),
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
