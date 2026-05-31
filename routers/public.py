"""
routers/public.py — Public endpoints (no auth required).

Routes:
  GET /health         — liveness check
  GET /brand-config   — public brand config for chatbot link tokens
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException

from db.redis_store import get_brand_by_token

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@router.get("/brand-config")
async def get_public_brand_config(token: str):
    """Return public brand fields for a chatbot link token. No credentials exposed."""
    config = get_brand_by_token(token)
    if not config:
        raise HTTPException(status_code=404, detail="Brand not found")
    # brand_hash is intentionally NOT exposed: the server derives tenant identity
    # from the link token itself, so clients never need (or get) the raw hash.
    return {
        "pg_ids": config.get("pg_ids", []),
        "brand_name": config.get("brand_name", ""),
        "cities": config.get("cities", ""),
        "areas": config.get("areas", ""),
        "is_configured": bool(config.get("pg_ids")),
    }
