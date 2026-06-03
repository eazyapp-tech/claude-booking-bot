"""
WhatsApp Business API channel: send text, images, and carousel templates.
Supports both Meta Graph API and Interakt API based on account config.
"""

import json
from datetime import datetime

import httpx

from core.log import get_logger

logger = get_logger("channels.whatsapp")

from db.redis_store import (
    get_account_values,
    get_whitelabel_pg_ids,
    clear_property_images_id,
    clear_property_template,
    clear_image_urls,
    set_image_urls,
    get_image_urls,
    set_response,
)
from db.postgres import insert_message
from db.redis_store import get_user_brand
from utils.image import upload_media_from_url
from core.channel_adapter import adapt, to_plain_text


def _get_whatsapp_config(user_id: str) -> dict:
    """Extract WhatsApp API config from account values."""
    account = get_account_values(user_id)
    phone_number_id = account.get("whatsapp_phone_number_id", "")
    access_token = account.get("whatsapp_access_token", "")
    waba_id = account.get("waba_id", "")
    is_meta = account.get("is_meta", True)

    if is_meta:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        base_url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
    else:
        headers = {
            "x-waba-id": waba_id,
            "x-access-token": access_token,
            "Content-Type": "application/json",
        }
        base_url = f"https://amped-express.interakt.ai/api/v17.0/{phone_number_id}/messages"

    return {
        "url": base_url,
        "headers": headers,
        "is_meta": is_meta,
        "phone_number_id": phone_number_id,
        "waba_id": waba_id,
    }


async def send_text(user_id: str, message: str) -> dict:
    """Send a text message via WhatsApp."""
    config = _get_whatsapp_config(user_id)
    recipient = user_id[:12]

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {"body": message},
    }

    # Persist to DB (brand-scoped)
    pg_ids = get_whitelabel_pg_ids(user_id)
    await insert_message(
        thread_id=recipient,
        user_phone=recipient,
        message_text=message,
        message_sent_by=2,
        platform_type="whatsapp",
        is_template=False,
        pg_ids=str(pg_ids),
        brand_hash=get_user_brand(user_id),
    )

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(config["url"], json=payload, headers=config["headers"])
            resp.raise_for_status()
            resp_data = resp.json()

            # Track message ID for response mapping
            messages = resp_data.get("messages", [])
            if messages:
                msg_id = messages[0].get("id", "")
                if msg_id:
                    set_response(msg_id, message)

            return resp_data
    except Exception as e:
        logger.error("Error sending text: %s", e)
        return {"error": True, "message": str(e)}


async def send_image(user_id: str, media_id: str, caption: str = "") -> dict:
    """Send a single image via WhatsApp."""
    config = _get_whatsapp_config(user_id)
    recipient = user_id[:12]

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "image",
        "image": {"id": media_id},
    }
    if caption:
        payload["image"]["caption"] = caption

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(config["url"], json=payload, headers=config["headers"])
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("Error sending image: %s", e)
        return {"error": True, "message": str(e)}


async def send_images(user_id: str, image_list: list) -> None:
    """Send multiple images via WhatsApp."""
    for item in image_list:
        media_id = item.get("media_id", "") if isinstance(item, dict) else str(item)
        if media_id:
            await send_image(user_id, media_id)
    clear_property_images_id(user_id)


async def send_carousel(user_id: str, property_template: list) -> dict:
    """Send a carousel template message with property cards."""
    config = _get_whatsapp_config(user_id)
    recipient = user_id[:12]
    is_meta = config["is_meta"]

    card_count = len(property_template)

    # Select template based on card count and platform
    if is_meta:
        templates = {1: "rentok_test_100_card1", 2: "rentok_test_100_card2", 3: "rentok_test_100_3", 4: "rentok_test_100_2"}
        template_name = templates.get(card_count, "rentok_test_100")
    else:
        templates = {1: "rentok_interakt_100", 2: "rentok_interakt_2", 3: "rentok_interakt_3", 4: "rentok_interakt_4"}
        template_name = templates.get(card_count, "rentok_interakt_5")

    # Upload images for each card
    image_ids = []
    image_urls = []
    fallback_image = "https://rentok-marketplace.s3.ap-south-1.amazonaws.com/marketplace-dump/microsite/sample/sample-image-house-3.webp"

    for i, card in enumerate(property_template[:5]):
        try:
            img_url = card.get("property_image", fallback_image)
            media_id = await upload_media_from_url(img_url, config)
            if media_id is None:
                media_id = await upload_media_from_url(fallback_image, config)
            image_ids.append(media_id)
            image_urls.append(img_url)
        except Exception as e:
            logger.warning("carousel image upload failed: %s", e)
            image_ids.append(None)
            image_urls.append(fallback_image)

    set_image_urls(user_id, image_urls)

    # Build carousel cards
    cards = []
    for index, card in enumerate(property_template[:5]):
        card_component = {
            "card_index": index,
            "components": [
                {
                    "type": "header",
                    "parameters": [
                        {"type": "image", "image": {"id": image_ids[index] if index < len(image_ids) else ""}}
                    ],
                },
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": card.get("property_name", "")},
                        {"type": "text", "text": card.get("property_location", "")},
                        {"type": "text", "text": card.get("property_rent", "")},
                        {"type": "text", "text": card.get("pg_available_for", "")},
                    ],
                },
                {
                    "type": "button",
                    "sub_type": "url",
                    "index": "0",
                    "parameters": [
                        {"type": "text", "text": str(card.get("prop_id", ""))}
                    ],
                },
            ],
        }
        cards.append(card_component)

    lang_code = "en" if template_name in ("rentok_test_100", "rentok_interakt_100", "rentok_interakt_2", "rentok_interakt_3", "rentok_interakt_4", "rentok_interakt_5") else "en_GB"

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": lang_code},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {
                            "type": "text",
                            "text": "your preferred location. See more options, Ask for the details, Images, Shortlist, Schedule a visit, Book the property.",
                        }
                    ],
                },
                {"type": "carousel", "cards": cards},
            ],
        },
    }

    # Persist to DB (brand-scoped)
    pg_ids = get_whitelabel_pg_ids(user_id)
    await insert_message(
        thread_id=recipient,
        user_phone=recipient,
        message_text=json.dumps(payload),
        message_sent_by=2,
        platform_type="whatsapp",
        is_template=True,
        pg_ids=str(pg_ids),
        brand_hash=get_user_brand(user_id),
    )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(config["url"], json=payload, headers=config["headers"])
            resp.raise_for_status()
            result = resp.json()
    except Exception as e:
        logger.error("Error sending carousel: %s", e)
        result = {"error": True, "message": str(e)}

    clear_image_urls(user_id)
    clear_property_template(user_id)
    return result


def units_to_wa_messages(units: list[dict]) -> list[dict]:
    """Degrade native units for WhatsApp, then map to send-layer message dicts.

    Message dicts: {"type": "text"|"list"|"buttons"|"media", ...}. The existing
    send_text/send_carousel layer consumes these. text is always the floor.
    """
    degraded = adapt(units, "whatsapp")
    messages: list[dict] = []
    for u in degraded:
        kind = u.get("kind")
        d = u.get("data", {})
        if kind == "text":
            messages.append({"type": "text", "text": d.get("text", "")})
        elif kind == "choice_list":
            rows = [{"id": o.get("id", str(i)), "title": o.get("label", "")[:24],
                     "description": (o.get("hint") or "")[:72]}
                    for i, o in enumerate(d.get("options", [])[:10])]
            messages.append({"type": "list", "rows": rows})
        elif kind == "quick_replies":
            buttons = [{"id": str(i), "title": str(r)[:20]}
                       for i, r in enumerate(d.get("replies", [])[:3])]
            messages.append({"type": "buttons", "buttons": buttons})
        elif kind == "action_buttons":
            raw = d.get("buttons", d.get("actions", []))[:3]
            buttons = [{"id": str(i), "title": (b.get("label", "") if isinstance(b, dict) else str(b))[:20]}
                       for i, b in enumerate(raw)]
            messages.append({"type": "buttons", "buttons": buttons})
        elif kind == "carousel" and d.get("payload") == "media":
            for it in d.get("items", []):
                messages.append({"type": "media", "url": it.get("url", ""),
                                 "media_type": it.get("type", "image")})
        else:
            # any other kind already degraded to text by the adapter, but guard anyway
            messages.append({"type": "text", "text": to_plain_text(u)})
    return messages


async def send_interactive_list(user_id: str, body: str, rows: list[dict],
                                header: str = "", button: str = "Choose") -> dict:
    """Send a WhatsApp interactive list (Meta type:interactive/list).

    rows: [{"id", "title", "description"}] (title<=24, description<=72, <=10 rows).
    Same body for Meta and Interakt — _get_whatsapp_config resolves the host.
    """
    config = _get_whatsapp_config(user_id)
    recipient = user_id[:12]

    interactive = {
        "type": "list",
        "body": {"text": body or " "},
        "action": {
            "button": button[:20],
            "sections": [{"title": (header or "Options")[:24], "rows": rows[:10]}],
        },
    }
    if header:
        interactive["header"] = {"type": "text", "text": header[:60]}

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "interactive",
        "interactive": interactive,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(config["url"], json=payload, headers=config["headers"])
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("Error sending interactive list: %s", e)
        return {"error": True, "message": str(e)}


async def send_interactive_buttons(user_id: str, body: str, buttons: list[dict]) -> dict:
    """Send WhatsApp reply buttons (Meta type:interactive/button).

    buttons: [{"id", "title"}] (title<=20, <=3 buttons).
    """
    config = _get_whatsapp_config(user_id)
    recipient = user_id[:12]

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body or " "},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b.get("id", str(i)), "title": b.get("title", "")[:20]}}
                    for i, b in enumerate(buttons[:3])
                ]
            },
        },
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(config["url"], json=payload, headers=config["headers"])
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("Error sending interactive buttons: %s", e)
        return {"error": True, "message": str(e)}


async def send_units(user_id: str, units: list[dict]) -> None:
    """Egress entry point: degrade native units → dispatch each on WhatsApp.

    text → send_text · list → send_interactive_list · buttons → send_interactive_buttons
    · media → upload_media_from_url + send_image. text is always the floor.
    """
    for m in units_to_wa_messages(units):
        t = m.get("type")
        if t == "text":
            await send_text(user_id, m.get("text", ""))
        elif t == "list":
            await send_interactive_list(user_id, m.get("body", "Pick one"), m.get("rows", []))
        elif t == "buttons":
            await send_interactive_buttons(user_id, m.get("body", " "), m.get("buttons", []))
        elif t == "media":
            config = _get_whatsapp_config(user_id)
            media_id = await upload_media_from_url(m.get("url", ""), config)
            if media_id:
                await send_image(user_id, media_id)
            else:
                await send_text(user_id, m.get("url", ""))
