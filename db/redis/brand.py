"""
db/redis/brand.py — Multi-tenant brand configuration.

Redis keys:
  brand_config:{sha256(api_key)[:16]}  — full brand config JSON (no TTL)
  brand_wa:{phone_number_id}           — reverse-lookup for WhatsApp webhook (no TTL)
  brand_token:{uuid}                   — public chatbot link token → brand hash (no TTL)

Isolation: raw API key NEVER stored — all reads/writes use _brand_hash(api_key)
as the Redis key prefix.
"""

import hashlib
import json

from db.redis._base import _r, _json_get, _json_set


def _brand_hash(api_key: str) -> str:
    """16-char prefix of SHA-256(api_key) — never stores the raw key."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def get_brand_config(api_key: str):
    """Return brand config dict for the given API key, or None if not set."""
    return _json_get(f"brand_config:{_brand_hash(api_key)}")


def get_brand_config_by_hash(brand_hash: str):
    """Return brand config dict by hash (admin endpoints only have hash, not raw key)."""
    return _json_get(f"brand_config:{brand_hash}")


def set_brand_config(api_key: str, config: dict) -> None:
    """Atomically write brand_config, brand_wa reverse-lookup, and brand_token."""
    brand_hash = _brand_hash(api_key)
    config["brand_hash"] = brand_hash  # store hash inside config for reverse-lookups
    config_str = json.dumps(config, default=str)
    pipe = _r().pipeline()
    pipe.set(f"brand_config:{brand_hash}", config_str)
    if config.get("whatsapp_phone_number_id"):
        pipe.set(f"brand_wa:{config['whatsapp_phone_number_id']}", config_str)
    if config.get("brand_link_token"):
        pipe.set(f"brand_token:{config['brand_link_token']}", brand_hash)
    pipe.execute()


def get_brand_wa_config(phone_number_id: str):
    """Return brand config for a given WhatsApp phone_number_id, or None."""
    return _json_get(f"brand_wa:{phone_number_id}")


def get_brand_by_token(token: str):
    """Return brand config for a public link token, or None if not found."""
    brand_hash = _r().get(f"brand_token:{token}")
    if not brand_hash:
        return None
    return _json_get(f"brand_config:{brand_hash.decode()}")


def get_default_brand_config():
    """Return the configured default brand config for tokenless web traffic, or None.

    Used by the web channel when no valid link token is supplied (demo / direct visits).
    Resolved server-side from settings.DEFAULT_BRAND_API_KEY — never from the request.
    """
    from config import settings
    return get_brand_config(settings.DEFAULT_BRAND_API_KEY)


# ---------------------------------------------------------------------------
# Per-brand feature flags (Phase 2 — brand-scoped flag overrides)
# ---------------------------------------------------------------------------

def get_brand_flags(brand_hash: str) -> dict:
    """Return per-brand flag overrides, or empty dict if none set."""
    return _json_get(f"brand_flags:{brand_hash}", default={})


def set_brand_flag(brand_hash: str, flag: str, value: bool) -> None:
    """Set a single per-brand flag override."""
    flags = get_brand_flags(brand_hash)
    flags[flag] = value
    _json_set(f"brand_flags:{brand_hash}", flags)


def get_effective_flags(brand_hash: str | None = None) -> dict:
    """Merge brand overrides over global defaults. Returns all flag values.

    Priority: per-brand override > global setting (from config.py / env).
    """
    from config import settings
    defaults = {
        "DYNAMIC_SKILLS_ENABLED": settings.DYNAMIC_SKILLS_ENABLED,
        "KYC_ENABLED": settings.KYC_ENABLED,
        "PAYMENT_REQUIRED": settings.PAYMENT_REQUIRED,
        "SEMANTIC_KB_ENABLED": settings.SEMANTIC_KB_ENABLED,
    }
    if brand_hash:
        overrides = get_brand_flags(brand_hash)
        defaults.update(overrides)
    return defaults
