"""
core/admin_login.py — ID + password gate for the admin panel.

The admin SPA used to embed the raw brand API key (it was even printed as the
login input's placeholder). This module replaces that with a real gate: the
browser POSTs {username, password} to /admin/login; we validate against
credentials held ONLY in env (never in the frontend bundle or git) and, on
success, hand back the brand API key the browser then sends as X-API-Key.

Credentials:
  - ADMIN_LOGIN_USERNAME          (default "oxotel")
  - ADMIN_LOGIN_PASSWORD          plaintext in env — sha256'd here, never compared raw
  - ADMIN_LOGIN_PASSWORD_SHA256   preferred — store only the hex digest
  - ADMIN_LOGIN_API_KEY           key returned on success (falls back to DEFAULT_BRAND_API_KEY)

If no password is configured, verify_admin_login returns (None, "unconfigured")
so the endpoint can answer 503 instead of silently letting anyone in.

Brute-force protection: per-username failure counter in Redis with a fixed
lockout window. A Redis hiccup never blocks a legitimate login (fail-open on
the throttle only — credential checking itself always runs).
"""

import hashlib
import hmac

from config import settings
from core.log import get_logger

logger = get_logger("admin_login")


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _expected_password_hash() -> str:
    """Resolve the configured password hash (digest preferred, else hash the plaintext)."""
    digest = (settings.ADMIN_LOGIN_PASSWORD_SHA256 or "").strip().lower()
    if digest:
        return digest
    if settings.ADMIN_LOGIN_PASSWORD:
        return _sha256_hex(settings.ADMIN_LOGIN_PASSWORD)
    return ""


def _throttle_key(username: str) -> str:
    return f"admin_login_fail:{username.lower()}"


def is_throttled(username: str) -> bool:
    try:
        from db.redis._base import _r

        raw = _r().get(_throttle_key(username))
        return bool(raw) and int(raw) >= settings.ADMIN_LOGIN_MAX_FAILS
    except Exception:
        return False  # never lock out a real admin because Redis blipped


def _record_failure(username: str) -> None:
    try:
        from db.redis._base import _r

        key = _throttle_key(username)
        r = _r()
        n = r.incr(key)
        if n == 1:
            r.expire(key, settings.ADMIN_LOGIN_THROTTLE_SECONDS)
    except Exception:
        pass


def _clear_failures(username: str) -> None:
    try:
        from db.redis._base import _r

        _r().delete(_throttle_key(username))
    except Exception:
        pass


def verify_admin_login(username: str, password: str) -> tuple[str | None, str]:
    """Validate credentials. Returns (api_key, reason).

    reason is one of: "ok" | "unconfigured" | "throttled" | "invalid" | "misconfigured".
    api_key is non-None only when reason == "ok".
    """
    username = (username or "").strip()
    password = password or ""

    expected_hash = _expected_password_hash()
    if not expected_hash:
        return None, "unconfigured"

    if is_throttled(username):
        logger.warning("Admin login throttled for user=%s", username)
        return None, "throttled"

    # Constant-time comparison on both fields (avoid early-out leaking which was wrong).
    user_ok = hmac.compare_digest(username, settings.ADMIN_LOGIN_USERNAME)
    pass_ok = hmac.compare_digest(_sha256_hex(password), expected_hash)
    if not (user_ok and pass_ok):
        _record_failure(username)
        return None, "invalid"

    api_key = settings.ADMIN_LOGIN_API_KEY or settings.DEFAULT_BRAND_API_KEY
    # The returned key must resolve to a real brand, or the panel would 403 on
    # every request after "login". Catch that here as a server misconfiguration.
    try:
        from db.redis.brand import get_brand_config

        if not get_brand_config(api_key):
            logger.error("Admin login: returned key has no brand config — check ADMIN_LOGIN_API_KEY")
            return None, "misconfigured"
    except Exception:
        logger.exception("Admin login: brand-config lookup failed")
        return None, "misconfigured"

    _clear_failures(username)
    return api_key, "ok"
