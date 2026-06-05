"""
core/demo_brand.py — Provision a starter DEMO brand for a fresh signup.

A new account gets an immediately-working web bot on sandbox inventory: a
brand_config pointing at settings.DEMO_PG_IDS, marked is_demo=True / status="demo".
Going live (Plan 2 Activate) swaps the demo pg_ids for the brand's real ones.
"""

import time
import uuid

from config import settings
from db.redis.brand import set_brand_config


def build_demo_config(brand_name: str) -> dict:
    """Pure builder for a demo brand_config dict (no Redis writes)."""
    now = int(time.time())
    return {
        "brand_name": (brand_name or "").strip() or "My Demo PG",
        "pg_ids": list(settings.DEMO_PG_IDS),
        "cities": list(settings.DEMO_CITIES),
        "areas": list(settings.DEMO_AREAS),
        "brand_link_token": str(uuid.uuid4()),
        "is_demo": True,
        "status": "demo",
        "created_at": now,
        "updated_at": now,
    }


def provision_demo_brand(api_key: str, brand_name: str) -> dict:
    """Create + persist the demo brand_config for a new account's api_key. Returns the config."""
    config = build_demo_config(brand_name)
    set_brand_config(api_key, config)  # also writes brand_token + injects brand_hash
    return config
