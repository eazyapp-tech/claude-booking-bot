"""
db/redis/accounts.py — Self-serve brand accounts + email-verification tokens.

Redis keys:
  brand_account:{email_lower}   — account JSON {email, password_sha256, api_key, brand_hash, email_verified, created_at} (no TTL)
  email_verify:{token}          — token → email_lower (24h TTL, single-use)

The account record deliberately holds the brand's own api_key so /admin/login can
return it (the panel sends it as X-API-Key). This generalizes the single
ADMIN_LOGIN_API_KEY model to one record per brand. Raw keys of OTHER brands are
never stored here — each record holds only its own.
"""

from db.redis._base import _r, _json_get, _json_set

EMAIL_VERIFY_TTL = 86400  # 24h


def _account_key(email: str) -> str:
    return f"brand_account:{email.strip().lower()}"


def get_account(email: str):
    """Return the account dict for an email (case-insensitive), or None."""
    return _json_get(_account_key(email))


def save_account(account: dict) -> None:
    """Persist an account record (keyed by lowercased email)."""
    _json_set(_account_key(account["email"]), account)


def account_exists(email: str) -> bool:
    return get_account(email) is not None


def set_email_verify_token(token: str, email: str) -> None:
    """Store token → email with a 24h TTL."""
    _json_set(f"email_verify:{token}", email.strip().lower(), ex=EMAIL_VERIFY_TTL)


def consume_email_verify_token(token: str):
    """Return the email for a verification token and delete it (single-use). None if missing/expired."""
    email = _json_get(f"email_verify:{token}")
    if email is not None:
        _r().delete(f"email_verify:{token}")
    return email
