"""
routers/webhooks.py — Webhook and cron endpoints.

Routes:
  GET  /webhook/whatsapp  — Meta webhook verification challenge
  POST /webhook/whatsapp  — Incoming WhatsApp messages
  POST /webhook/payment   — Payment confirmation callback
  POST /cron/follow-ups   — Proactive follow-up processing
"""

import asyncio

import core.state as state
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from config import settings
from core.auth import verify_api_key
from core.webhook_security import verify_whatsapp_signature, verify_payment_signature
from core.log import get_logger
from core.pipeline import run_pipeline
from core.rate_limiter import check_rate_limit, RateLimitExceeded
from channels.whatsapp import send_text, send_carousel, send_images, send_units, filter_interactive
from core.ui_parts import generate_ui_parts
from core.signals import current_signals
from db import postgres as pg
from db.redis_store import (
    get_active_request,
    set_active_request,
    delete_active_request,
    get_account_values,
    set_whitelabel_pg_ids,
    set_user_name,
    get_no_message,
    clear_no_message,
    get_brand_wa_config,
    _json_set,
    get_property_template,
    get_property_images_id,
    get_due_followups,
    complete_followup,
    get_user_memory,
    get_conversation,
    save_conversation,
    # wamid dedup + queue
    set_wamid_seen,
    is_wamid_seen,
    wa_queue_push,
    wa_queue_drain,
    wa_queue_len,
    wa_processing_acquire,
    wa_processing_release,
    # pipeline cancellation (Phase C)
    set_cancel_requested,
)

logger = get_logger("routers.webhooks")

_tag_phone = lambda p: f"***{str(p)[-4:]}" if p else "***"

router = APIRouter()


# ---------------------------------------------------------------------------
# WhatsApp async drain-and-process (debounce + queue accumulation)
# ---------------------------------------------------------------------------

async def _drain_and_process(user_phone: str) -> None:
    """Drain the per-user WhatsApp message queue after a debounce window.

    This coroutine is launched via asyncio.create_task() once per user when
    their first queued message arrives.  It:

      1. Waits WA_DEBOUNCE_SECONDS for any rapid follow-ups to land.
      2. Drains all pending messages from {user_phone}:wa_queue.
      3. Merges them into a single user turn (newline-joined).
      4. Runs the AI pipeline.
      5. Sends the response (+ carousel/images if any).
      6. If new messages arrived during processing (Phase C): sets cancel signal
         so any still-running pipeline iteration exits at its next checkpoint,
         then loops to drain the new arrivals.
      7. Releases the per-user processing lock.

    The caller must already hold the wa_processing lock before creating this task.
    """
    try:
        while True:
            # Debounce: let the user finish typing before we process
            await asyncio.sleep(settings.WA_DEBOUNCE_SECONDS)

            # Drain everything currently in the queue
            messages = wa_queue_drain(user_phone)
            if not messages:
                # Queue was empty (race condition or already drained) — done
                break

            # Merge rapid-fire messages into one coherent user intent
            combined = "\n".join(messages) if len(messages) > 1 else messages[0]
            logger.info(
                "WhatsApp drain: user=%s count=%d text=%r",
                _tag_phone(user_phone), len(messages), combined[:100],
            )

            # Run the AI pipeline with the merged intent
            try:
                response, agent_name, _lang = await run_pipeline(user_phone, combined)
            except Exception as e:
                logger.error("Pipeline error in drain for %s: %s", _tag_phone(user_phone), e)
                response = "I'm sorry, I'm having trouble right now. Please try again."
                agent_name = "error"

            # Human mode — admin is responding manually; skip all outbound sends
            if agent_name == "human":
                pass
            # No-message flag — pipeline chose not to respond
            elif get_no_message(user_phone) == "1":
                clear_no_message(user_phone)
            else:
                # Send primary response
                await send_text(user_phone, response)

                # Send property carousel if pipeline generated one
                template = get_property_template(user_phone)
                if template:
                    await send_carousel(user_phone, template)

                # Send property images if pipeline generated any
                images = get_property_images_id(user_phone)
                if images:
                    await send_images(user_phone, images)

                # Forward the interactive supplements WhatsApp otherwise lacks (tappable
                # quick replies / lists). Filtered to interactive kinds so the body text,
                # carousel and images already sent above are never duplicated (no double-send).
                try:
                    units = generate_ui_parts(response, agent_name, user_phone,
                                              _lang or "en", signals=current_signals())
                    interactive = filter_interactive(units)
                    if interactive:
                        await send_units(user_phone, interactive)
                except Exception as e:
                    logger.warning("WA interactive supplements failed for %s: %s", _tag_phone(user_phone), e)

            # If new messages arrived while we were processing, loop and drain again.
            # Set the cancellation signal BEFORE the next pipeline run so that the
            # previous run (if still mid-tool-call via Phase C checkpoint) exits cleanly.
            if wa_queue_len(user_phone) == 0:
                break
            logger.info(
                "WhatsApp drain: new messages arrived for %s during processing, looping",
                _tag_phone(user_phone),
            )
            # Phase C: signal any in-flight pipeline iteration to abort at its next checkpoint
            set_cancel_requested(user_phone)

    except Exception as e:
        logger.error("Unexpected error in _drain_and_process for %s: %s", _tag_phone(user_phone), e)
    finally:
        # Always release the processing lock so the next message can start a new drain
        wa_processing_release(user_phone)


# ---------------------------------------------------------------------------
# WhatsApp webhook verification (GET — Meta webhook setup)
# ---------------------------------------------------------------------------

@router.get("/webhook/whatsapp")
async def verify_whatsapp_webhook(request: Request):
    """Meta webhook verification challenge."""
    mode = request.query_params.get("hub.mode", "")
    token = request.query_params.get("hub.verify_token", "")
    challenge = request.query_params.get("hub.challenge", "")

    verify_token = settings.WHATSAPP_VERIFY_TOKEN if hasattr(settings, "WHATSAPP_VERIFY_TOKEN") else "booking-bot-verify"

    if mode == "subscribe" and token == verify_token:
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(challenge)

    raise HTTPException(status_code=403, detail="Verification failed")


# ---------------------------------------------------------------------------
# WhatsApp incoming messages (POST)
# ---------------------------------------------------------------------------

@router.post("/webhook/whatsapp", dependencies=[Depends(verify_whatsapp_signature)])
async def whatsapp_webhook(request: Request):
    """Handle incoming WhatsApp messages (Meta + Interakt webhook)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "invalid json"}, status_code=400)

    # Extract message data from webhook payload
    entry = body.get("entry", [{}])
    if not entry:
        return JSONResponse({"status": "no entry"})

    changes = entry[0].get("changes", [{}])
    if not changes:
        return JSONResponse({"status": "no changes"})

    value = changes[0].get("value", {})
    messages = value.get("messages", [])
    contacts = value.get("contacts", [])

    if not messages:
        return JSONResponse({"status": "no messages"})

    msg = messages[0]
    msg_type = msg.get("type", "")

    # Only handle text messages
    if msg_type != "text":
        return JSONResponse({"status": "ignored", "type": msg_type})

    user_phone = msg.get("from", "")
    text = msg.get("text", {}).get("body", "").strip()
    wamid = msg.get("id", "")

    if not user_phone or not text:
        return JSONResponse({"status": "empty message"})

    # Rate limiting — protect against WhatsApp message floods
    try:
        check_rate_limit(user_phone)
    except RateLimitExceeded as e:
        logger.warning("WhatsApp rate limited: user=%s tier=%s", _tag_phone(user_phone), e.tier)
        return JSONResponse({"status": "rate_limited", "retry_after": e.retry_after})

    # ── Dedup by wamid (Meta's unique per-message ID) ──────────────────────────
    # This correctly deduplicates Meta's legitimate duplicate-delivery retries
    # (same wamid = same physical message) without blocking genuine follow-ups
    # that happen to contain the same text as a previous message.
    if not wamid or is_wamid_seen(wamid):
        return JSONResponse({"status": "duplicate"})
    set_wamid_seen(wamid)

    # Store contact name
    if contacts:
        name = contacts[0].get("profile", {}).get("name", "")
        if name:
            set_user_name(user_phone, name)

    # Extract account context from query params or headers
    pg_ids = request.query_params.get("pg_ids", "")
    if pg_ids:
        set_whitelabel_pg_ids(user_phone, pg_ids.split(","))

    # Persist incoming message immediately (before any queuing)
    pg_ids_val = request.query_params.get("pg_ids", "")
    from db.redis_store import get_user_brand as _gub_wa
    _wa_bh = _gub_wa(user_phone)
    await pg.insert_message(
        thread_id=user_phone,
        user_phone=user_phone,
        message_text=text,
        message_sent_by=1,
        platform_type="whatsapp",
        is_template=False,
        pg_ids=pg_ids_val,
        brand_hash=_wa_bh,
    )

    # Hydrate brand config from WhatsApp phone_number_id
    # value is already extracted above; metadata.phone_number_id identifies the brand
    phone_number_id = value.get("metadata", {}).get("phone_number_id")
    if phone_number_id:
        brand_cfg = get_brand_wa_config(phone_number_id)
        if brand_cfg:
            hydrated = {
                "pg_ids": brand_cfg.get("pg_ids", []),
                "whatsapp_phone_number_id": brand_cfg.get("whatsapp_phone_number_id", ""),
                "whatsapp_access_token": brand_cfg.get("whatsapp_access_token", ""),
                "waba_id": brand_cfg.get("waba_id", ""),
                "is_meta": brand_cfg.get("is_meta", True),
                "brand_name": brand_cfg.get("brand_name", ""),
            }
            # Persist account_values WITHOUT short TTL (Gap 1 fix)
            # WA creds rarely change; next incoming message refreshes them.
            # Previous ex=3600 caused silent failures for admin messages,
            # broadcasts, and follow-ups after 1 hour of user inactivity.
            _json_set(f"{user_phone}:account_values", hydrated)

            # Tag WhatsApp user with brand for multi-brand isolation
            brand_hash_val = brand_cfg.get("brand_hash")
            if brand_hash_val:
                from db.redis_store import set_user_brand, add_to_brand_active_users
                set_user_brand(user_phone, brand_hash_val)
                add_to_brand_active_users(user_phone, brand_hash_val)

    # ── Queue + debounce ────────────────────────────────────────────────────
    # Push message text onto the per-user Redis queue.
    # If no drain task is already running for this user, start one.
    # The drain task waits WA_DEBOUNCE_SECONDS, then merges all queued
    # messages into one AI turn — so rapid-fire "Boys PG" + "under 10k"
    # become a single coherent intent.
    wa_queue_push(user_phone, text)
    if wa_processing_acquire(user_phone):
        # Lock acquired — we are the first; fire-and-forget the drain coroutine
        asyncio.create_task(_drain_and_process(user_phone))

    # Return 200 immediately — pipeline runs asynchronously in the drain task
    return JSONResponse({"status": "ok", "queued": True})


# ---------------------------------------------------------------------------
# Payment confirmation webhook
# ---------------------------------------------------------------------------

@router.post("/webhook/payment", dependencies=[Depends(verify_payment_signature)])
async def payment_webhook(request: Request):
    """Handle payment confirmation callback from Rentok."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "invalid json"}, status_code=400)

    user_id = body.get("user_id", "")
    pg_id = body.get("pg_id", "")
    pg_number = body.get("pg_number", "")
    status = body.get("status", "")

    if not user_id:
        return JSONResponse({"status": "missing user_id"}, status_code=400)

    if status == "success":
        # Notify user that payment was confirmed
        notification = "Payment confirmed for your property reservation. Your booking is being processed."
        from db.redis_store import get_user_brand as _get_ub
        state.conversation.add_assistant_message(user_id, notification, brand_hash=_get_ub(user_id))

        # Send notification via WhatsApp if config exists
        account = get_account_values(user_id)
        if account.get("whatsapp_phone_number_id"):
            await send_text(user_id, notification)

    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Proactive follow-up cron
# ---------------------------------------------------------------------------

@router.post("/cron/follow-ups", dependencies=[Depends(verify_api_key)])
async def process_followups():
    """Process due follow-ups. Call this endpoint via an external cron (every 15 min).

    Two systems run in parallel:
    1. Sorted-set followups (payment.py) — fires Step 1 (visit_complete) at visit_time + 2h
    2. State-machine followups (core/followup.py) — fires Steps 2 & 3 based on elapsed time
    """
    from core.followup import (
        get_followup_state, advance_followup, get_due_state_followups,
        _save_followup_state,
    )
    from db.redis_store import get_user_brand as _get_ub, get_user_phone as _get_phone

    followups = get_due_followups(limit=50)
    processed = 0
    errors = 0

    for entry in followups:
        user_id = entry.get("user_id", "")
        ftype = entry.get("type", "")
        data = entry.get("data", {})
        raw = entry.get("_raw")

        if not user_id:
            complete_followup(raw)
            continue

        prop_name = data.get("property_name", "your shortlisted property")

        try:
            if ftype == "visit_complete":
                # Step 1: Use state machine to generate message + advance state
                states = get_followup_state(user_id)
                prop_id = data.get("property_id", "")
                # Find matching state entry
                target = None
                for s in states:
                    if s.get("property_id") == prop_id and s.get("step", 0) == 0:
                        target = s
                        break
                if target:
                    message = advance_followup(user_id, target)
                    _save_followup_state(user_id, states)
                else:
                    # Fallback: no state entry (legacy followup without state machine)
                    message = (
                        f"Hey! How was your visit to {prop_name}? 🏠\n\n"
                        "Quick feedback:\n"
                        "1️⃣ Loved it — I want to book!\n"
                        "2️⃣ It was okay\n"
                        "3️⃣ Not for me\n\n"
                        "Just reply with 1, 2, or 3 and I'll take it from there!"
                    )
            elif ftype == "payment_pending":
                link = data.get("link", "")
                amount = data.get("amount", "")
                message = (
                    f"Just a friendly reminder — your payment link for {prop_name} "
                    f"is still active (₹{amount}).\n\n"
                    f"{link}\n\n"
                    "Complete it to lock in your reservation. "
                    "Let me know if you have any questions!"
                )
            elif ftype == "shortlist_idle":
                mem = get_user_memory(user_id)
                n_shortlisted = len(mem.get("properties_shortlisted", []))
                message = (
                    f"Hey! You shortlisted {prop_name} a couple of days ago. "
                    f"Still interested? 🤔\n\n"
                )
                if n_shortlisted > 1:
                    message += (
                        f"You have {n_shortlisted} properties shortlisted. "
                        "Want me to compare them or schedule a visit to your top pick?"
                    )
                else:
                    message += (
                        "Want me to show you more details, schedule a visit, "
                        "or look for other options nearby?"
                    )
            else:
                complete_followup(raw)
                continue

            ok = await _deliver_followup(user_id, message)
            if ok:
                processed += 1

            complete_followup(raw)

        except Exception as e:
            logger.error("follow-up processing failed: user=%s type=%s error=%s", user_id, ftype, e)
            errors += 1

    # ── State-machine followups: Steps 2 & 3 (no-reply escalation) ──
    state_due = get_due_state_followups(limit=50)
    for uid, followup in state_due:
        try:
            # get_due_state_followups returns references into the full state list,
            # so advance_followup mutates the dict in place. We just re-read + save.
            old_step = followup.get("step", 0)
            message = advance_followup(uid, followup)
            if not message:
                continue
            # Re-read the full state list and update the matching entry
            states = get_followup_state(uid)
            prop_id = followup.get("property_id")
            for i, s in enumerate(states):
                if s.get("property_id") == prop_id and s.get("step", 0) == old_step:
                    states[i] = followup
                    break
            _save_followup_state(uid, states)

            ok = await _deliver_followup(uid, message)
            if ok:
                processed += 1
        except Exception as e:
            logger.error("state followup failed: user=%s prop=%s error=%s", uid, followup.get("property_name"), e)
            errors += 1

    return {
        "status": "ok",
        "processed": processed,
        "errors": errors,
    }


async def _deliver_followup(user_id: str, message: str) -> bool:
    """Deliver a follow-up message via the best available channel.

    Channel priority:
    1. WhatsApp user (phone-based ID) → WhatsApp
    2. Web user with phone number → WhatsApp
    3. Web user without phone → save as proper assistant message in conversation
    """
    from db.redis_store import get_user_brand as _get_ub, get_user_phone as _get_phone

    # WhatsApp user (phone-based ID)
    if user_id.isdigit() and 10 <= len(user_id) <= 13:
        account = get_account_values(user_id)
        if account.get("whatsapp_phone_number_id"):
            await send_text(user_id, message)
            return True
        logger.info("follow-up skipped (no WA config): user=%s", user_id)
        return False

    # Web user — check if they have a phone number (can reach via WhatsApp)
    phone = _get_phone(user_id)
    if phone and phone.isdigit() and 10 <= len(phone) <= 13:
        # Try to send via WhatsApp using the phone number
        try:
            await send_text(phone, message)
            return True
        except Exception as e:
            logger.warning("WA delivery to web user failed (phone=%s): %s", _tag_phone(phone), e)
            # Fall through to in-chat delivery

    # Web user without phone or WA delivery failed — save as proper assistant message
    brand_hash = _get_ub(user_id)
    conv = get_conversation(user_id)
    conv.append({"role": "assistant", "content": message})
    save_conversation(user_id, conv, brand_hash=brand_hash)
    return True
