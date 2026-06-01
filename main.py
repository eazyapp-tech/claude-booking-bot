"""
Claude Booking Bot — FastAPI Application (thin factory)

All routes live in routers/. This file only wires up startup/shutdown
and registers the four routers with the app.

Router modules:
  routers/public.py    — GET /health, GET /brand-config
  routers/chat.py      — POST /chat, POST /chat/stream, feedback, funnel, language
  routers/webhooks.py  — GET/POST /webhook/whatsapp, /webhook/payment, /cron/follow-ups
  routers/admin.py     — All /admin/* endpoints, /rate-limit/status
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import core.state as state
from core.claude import AnthropicEngine
from core.conversation import ConversationManager
from core.log import get_logger
from core.rate_limiter import RateLimitExceeded
from core.tool_executor import ToolExecutor
from db import postgres as pg
from routers import admin, chat, public, webhooks
from tools.registry import get_all_handlers, init_registry

logger = get_logger("main")


# ---------------------------------------------------------------------------
# Brand auto-seeding — ensures known brands have configs on first startup
# ---------------------------------------------------------------------------

_SEED_BRANDS = {
    "OxOtel1234": {
        "brand_name": "OxOtel",
        "pg_ids": [
            "l5zf3ckOnRQV9OHdv5YTTXkvLHp1",
            "egu5HmrYFMP8MRJyMsefnpaL7ka2",
            "Z2wyLOXXp5QA596DQ6aZAQpakmQ2",
            "UaDCGP3dzzZRgVIzBDgXb5ry5ng2",
            "EqhTMiUNksgXh5QhGQRsY5DQiO42",
            "fzDBxYtHgVV21ertfkUdSHeomiv2",
            "CUxtdeaGxYS8IMXmGZ1yUnqyfOn2",
            "wtlUSKV9H8bkNqvlGmnogwoqwyk2",
            "1Dy0t6YeIHh3kQhqvQR8tssHWKt1",
            "U2uYCaeiCebrE95iUDsS4PwEd1J2",
        ],
        "cities": "Mumbai",
        "areas": "Andheri, Kurla, Powai",
    },
    # Add more brands here when their pg_ids are available:
    # "Stanza1234": { "brand_name": "Stanza", "pg_ids": [...], "cities": "Bangalore", ... },
}


def _seed_brand_configs():
    """Idempotent: create brand configs for known brands if they don't exist yet."""
    from db.redis_store import get_brand_config, set_brand_config
    import uuid as _uuid

    for api_key, brand_data in _SEED_BRANDS.items():
        if not get_brand_config(api_key):
            # Auto-generate a brand_link_token for the chatbot URL
            brand_data = {**brand_data}  # shallow copy so we don't mutate the constant
            if "brand_link_token" not in brand_data:
                brand_data["brand_link_token"] = str(_uuid.uuid4())
            set_brand_config(api_key, brand_data)
            logger.info("Auto-seeded brand config for %s", brand_data["brand_name"])
        else:
            logger.debug("Brand config already exists for %s", brand_data.get("brand_name", api_key))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await pg.init_pool()
    await pg.create_booking_messages_table()
    await pg.create_property_documents_table()
    await pg.enable_pgvector()  # Semantic KB — pgvector extension + embedding columns
    await pg.create_leads_table()
    await pg.add_brand_hash_columns()  # Phase 2B migration — idempotent
    await pg.create_error_events_table()  # Sprint 4 — structured error log
    init_registry()

    executor = ToolExecutor()
    executor.register_many(get_all_handlers())

    state.engine = AnthropicEngine(tool_executor=executor)
    state.conversation = ConversationManager()

    # Auto-seed brand configs for known brands (idempotent)
    _seed_brand_configs()

    logger.info("Claude Booking Bot ready")
    yield

    # Shutdown
    await pg.close_pool()
    logger.info("Pools closed")


app = FastAPI(title="Claude Booking Bot", lifespan=lifespan)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={
            "detail": f"Rate limit exceeded ({exc.tier}). "
                      f"Try again in {exc.retry_after}s.",
            "retry_after": exc.retry_after,
            "tier": exc.tier,
            "limit": exc.limit,
        },
        headers={"Retry-After": str(exc.retry_after)},
    )


app.include_router(public.router)
app.include_router(chat.router)
app.include_router(webhooks.router)
app.include_router(admin.router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
