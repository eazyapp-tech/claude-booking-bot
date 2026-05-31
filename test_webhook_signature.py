"""
test_webhook_signature.py — Webhook HMAC payload-authenticity regression test.

Proves Wave 1 #2: inbound webhooks (WhatsApp + payment) verify an HMAC-SHA256
signature over the RAW request body, reject tampered/forged/absent signatures,
and fall back to legacy X-API-Key auth only when no secret is configured.

Deterministic: no Redis, no network, no LLM. A minimal fake Request stands in for
Starlette's. Run: `python test_webhook_signature.py` (exit 0 = all pass).
"""

import asyncio
import hashlib
import hmac
import os
import sys
from unittest.mock import patch

# webhook_security → core.auth → config.settings (needs an API key at import).
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

from fastapi import HTTPException  # noqa: E402

from core import webhook_security as ws  # noqa: E402

SECRET = "meta-app-secret-xyz"
BODY = b'{"entry":[{"changes":[{"value":{"messages":[{"type":"text"}]}}]}]}'


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class FakeRequest:
    def __init__(self, body: bytes, headers: dict):
        self._body = body
        self.headers = headers

    async def body(self) -> bytes:
        return self._body


# ── Test harness ─────────────────────────────────────────────────────────────
_passed = 0
_failed = 0


def check(name, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


def raises_403(coro):
    try:
        asyncio.run(coro)
        return False
    except HTTPException as e:
        return e.status_code == 403


def runs_ok(coro):
    try:
        asyncio.run(coro)
        return True
    except HTTPException:
        return False


# ── 1. Pure signature check ──────────────────────────────────────────────────
print("Webhook signature verification\n")

good = sign(SECRET, BODY)
check("valid sha256=<hex> accepted", ws.signature_is_valid(SECRET, BODY, f"sha256={good}"))
check("valid bare <hex> accepted", ws.signature_is_valid(SECRET, BODY, good))
check("wrong signature rejected", not ws.signature_is_valid(SECRET, BODY, "sha256=" + "0" * 64))
check("empty signature rejected", not ws.signature_is_valid(SECRET, BODY, ""))
check("empty secret rejected", not ws.signature_is_valid("", BODY, f"sha256={good}"))
check("tampered body rejected", not ws.signature_is_valid(SECRET, BODY + b" ", f"sha256={good}"))
check("signature for different secret rejected",
      not ws.signature_is_valid(SECRET, BODY, f"sha256={sign('other-secret', BODY)}"))


# ── 2. WhatsApp dependency — secret CONFIGURED ───────────────────────────────
with patch.object(ws.settings, "WHATSAPP_APP_SECRET", SECRET):
    req_valid = FakeRequest(BODY, {"X-Hub-Signature-256": f"sha256={sign(SECRET, BODY)}"})
    check("WA dep: valid signature passes", runs_ok(ws.verify_whatsapp_signature(req_valid)))

    req_tampered = FakeRequest(BODY + b"x", {"X-Hub-Signature-256": f"sha256={sign(SECRET, BODY)}"})
    check("WA dep: tampered body → 403", raises_403(ws.verify_whatsapp_signature(req_tampered)))

    req_missing = FakeRequest(BODY, {})
    check("WA dep: missing signature → 403", raises_403(ws.verify_whatsapp_signature(req_missing)))

    req_apikey_only = FakeRequest(BODY, {"X-API-Key": "anything"})
    check("WA dep: X-API-Key alone (no signature) → 403",
          raises_403(ws.verify_whatsapp_signature(req_apikey_only)))


# ── 3. WhatsApp dependency — secret UNSET → legacy X-API-Key fallback ─────────
with patch.object(ws.settings, "WHATSAPP_APP_SECRET", ""):
    # API_KEY also unset → verify_api_key is a no-op → request allowed.
    with patch.object(ws.settings, "API_KEY", None):
        req = FakeRequest(BODY, {})
        check("WA dep: no secret + no API_KEY → allowed (legacy open)",
              runs_ok(ws.verify_whatsapp_signature(req)))
    # API_KEY set → legacy check enforces it.
    with patch.object(ws.settings, "API_KEY", "legacy-key"):
        req_bad = FakeRequest(BODY, {"X-API-Key": "wrong"})
        check("WA dep: no secret + wrong API_KEY → rejected",
              not runs_ok(ws.verify_whatsapp_signature(req_bad)))
        req_ok = FakeRequest(BODY, {"X-API-Key": "legacy-key"})
        check("WA dep: no secret + correct API_KEY → allowed",
              runs_ok(ws.verify_whatsapp_signature(req_ok)))


# ── 4. Payment dependency — secret CONFIGURED ────────────────────────────────
PAY_SECRET = "pay-secret-abc"
PAY_BODY = b'{"user_id":"u1","status":"success"}'
with patch.object(ws.settings, "PAYMENT_WEBHOOK_SECRET", PAY_SECRET):
    req_valid = FakeRequest(PAY_BODY, {"X-Webhook-Signature": sign(PAY_SECRET, PAY_BODY)})
    check("Payment dep: valid signature passes", runs_ok(ws.verify_payment_signature(req_valid)))

    req_forged = FakeRequest(PAY_BODY, {"X-Webhook-Signature": sign("guess", PAY_BODY)})
    check("Payment dep: forged signature → 403", raises_403(ws.verify_payment_signature(req_forged)))

    req_missing = FakeRequest(PAY_BODY, {})
    check("Payment dep: missing signature → 403", raises_403(ws.verify_payment_signature(req_missing)))


# ── Summary ──────────────────────────────────────────────────────────────────
print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
