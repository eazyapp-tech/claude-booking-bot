"""
core/webhook_security.py — HMAC payload-authenticity verification for inbound webhooks.

Meta signs every WhatsApp webhook delivery with an `X-Hub-Signature-256` header:
the HMAC-SHA256 of the *raw* request body keyed by the app secret, hex-encoded and
prefixed `sha256=`. The payment callback uses the same scheme via `X-Webhook-Signature`.

Verifying authenticity requires the RAW body bytes (not re-serialized JSON), so these
dependencies read `await request.body()` before any parsing. Starlette caches the body,
so the downstream `await request.json()` in the handler still works.

Rollout-safe: when the corresponding secret is unset, verification is skipped and the
endpoint falls back to the legacy `verify_api_key` (X-API-Key) check — so configuring
the secret in the environment is what activates enforcement, with no code change.
"""

import hashlib
import hmac

from fastapi import HTTPException, Request

from config import settings
from core.auth import verify_api_key
from core.log import get_logger

logger = get_logger("webhook_security")


def signature_is_valid(secret: str, raw_body: bytes, signature_header: str) -> bool:
    """Constant-time check of an HMAC-SHA256 hex signature over raw_body.

    Accepts both the Meta `sha256=<hex>` form and a bare `<hex>` form. Returns
    False for any empty/secretless/malformed input — never raises.
    """
    if not secret or not signature_header:
        return False
    provided = signature_header.strip()
    if provided.startswith("sha256="):
        provided = provided[len("sha256="):]
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)


async def verify_whatsapp_signature(request: Request):
    """Dependency for POST /webhook/whatsapp.

    Enforces X-Hub-Signature-256 when WHATSAPP_APP_SECRET is set; otherwise falls
    back to the legacy X-API-Key check (Interakt / pre-secret deployments).
    """
    secret = settings.WHATSAPP_APP_SECRET
    if not secret:
        await verify_api_key(request.headers.get("X-API-Key"))
        return
    raw = await request.body()
    if not signature_is_valid(secret, raw, request.headers.get("X-Hub-Signature-256", "")):
        logger.warning("Rejected WhatsApp webhook — invalid or missing X-Hub-Signature-256")
        raise HTTPException(status_code=403, detail="Invalid webhook signature")


async def verify_payment_signature(request: Request):
    """Dependency for POST /webhook/payment.

    Enforces X-Webhook-Signature when PAYMENT_WEBHOOK_SECRET is set; otherwise falls
    back to the legacy X-API-Key check.
    """
    secret = settings.PAYMENT_WEBHOOK_SECRET
    if not secret:
        await verify_api_key(request.headers.get("X-API-Key"))
        return
    raw = await request.body()
    if not signature_is_valid(secret, raw, request.headers.get("X-Webhook-Signature", "")):
        logger.warning("Rejected payment webhook — invalid or missing X-Webhook-Signature")
        raise HTTPException(status_code=403, detail="Invalid webhook signature")
