"""
ui_parts.py — Backend-controlled UI part generator (Generative UI).

Generates structured UI parts that the frontend renders via its component
registry. Replaces the fragile frontend quick-replies.js which guessed
context from regex on bot text.

Advantages over frontend chip generation:
  - Access to Redis (property names, user prefs, shortlist, memory, images)
  - Deterministic: no regex guessing — knows the agent + response context
  - Single source of truth: backend decides what UI to show
  - i18n-ready: chips are generated in the user's locale

Part types emitted:
  - quick_replies:     { type, chips: [{ label, action, icon? }] }
  - action_buttons:    { type, buttons: [{ label, action, style }] }
  - status_card:       { type, status, icon, title, subtitle, details[], actions[] }
  - confirmation_card: { type, title, subtitle, details[], confirm_action, cancel_action, style }
  - image_gallery:     { type, property_name, images: [{ url, caption? }] }
"""

import re
from core.log import get_logger
from db.redis_store import get_property_info_map, get_preferences, get_property_images_id

logger = get_logger("core.ui_parts")

# ── i18n labels ──────────────────────────────────────────────────────────

_LABELS = {
    "en": {
        "details": "Details",
        "visit": "Schedule Visit",
        "compare": "Compare",
        "shortlist": "Shortlist",
        "more_options": "More Options",
        "see_rooms": "See Rooms",
        "images": "Photos",
        "commute": "Commute Time",
        "my_bookings": "My Bookings",
        "browse_more": "Browse More",
        "ive_paid": "I've Paid",
        "search_pgs": "Search Properties",
        "diff_area": "Different Area",
        "loved_it": "Loved it!",
        "was_okay": "It was okay",
        "not_for_me": "Not for me",
        "confirm": "Confirm",
        "change_time": "Change Time",
        "reschedule": "Reschedule",
        "cancel_visit": "Cancel Visit",
        "search_here": "Search PGs Here",
        "tell_more": "Tell Me More",
    },
    "hi": {
        "details": "Details",
        "visit": "Visit Book Karo",
        "compare": "Compare Karo",
        "shortlist": "Shortlist Karo",
        "more_options": "Aur Options",
        "see_rooms": "Rooms Dekho",
        "images": "Photos Dekho",
        "commute": "Kitna Door Hai?",
        "my_bookings": "Meri Bookings",
        "browse_more": "Aur Dekho",
        "ive_paid": "Payment Ho Gaya",
        "search_pgs": "PG Search Karo",
        "diff_area": "Alag Area",
        "loved_it": "Bahut Pasand Aaya!",
        "was_okay": "Theek Tha",
        "not_for_me": "Pasand Nahi Aaya",
        "confirm": "Confirm Karo",
        "change_time": "Time Badlo",
        "reschedule": "Reschedule Karo",
        "cancel_visit": "Visit Cancel",
        "search_here": "Yahan PG Dhundho",
        "tell_more": "Aur Batao",
    },
}


def _t(key: str, locale: str) -> str:
    """Get localized label. Falls back to English."""
    return _LABELS.get(locale, _LABELS["en"]).get(key, _LABELS["en"].get(key, key))


# ── Property name extraction (mirrors frontend logic) ────────────────────

def _extract_listed_properties(text: str) -> list[dict]:
    """Extract numbered properties from search-result listings.
    Returns list of { num: int, name: str }.
    """
    props = []
    seen = set()

    # Bold format: **1. Name** or **1. Name — …**
    for m in re.finditer(r"\*\*(\d+)\.\s+([^*\n]+?)\*\*", text):
        num = int(m.group(1))
        if num not in seen:
            seen.add(num)
            name = re.sub(r"\s*[—–\-|]\s*$", "", m.group(2)).strip()
            props.append({"num": num, "name": name})

    if props:
        return props

    # H3 format: ### 1. Name or ### 🏠 1. Name
    for m in re.finditer(r"^#{1,3}\s+(?:[^\d\n]*?)(\d+)\.\s+(.+?)$", text, re.MULTILINE):
        num = int(m.group(1))
        if num not in seen:
            seen.add(num)
            name = re.sub(r"\s*[—–\-|]\s*$", "", m.group(2)).strip()
            props.append({"num": num, "name": name})

    return props


def _extract_single_name(text: str) -> str | None:
    """Extract a single property name from detail/commute/shortlist responses."""
    # 'Property Name' in single quotes — require uppercase first char to avoid
    # matching apostrophes in contractions ("isn't loading yet, but here's" → false match)
    m = re.search(r"'([A-Z][^'\n]{2,49})'", text)
    if m:
        return m.group(1).strip()
    # **Property Name** — first bold match (skip numbered ones)
    m = re.search(r"\*\*([^*\d][^*\n]{2,40})\*\*", text)
    if m:
        return re.sub(r"\s*[—–\-|:]\s*$", "", m.group(1)).strip()
    return None


def _enrich_with_redis_names(props: list[dict], user_id: str) -> list[dict]:
    """If regex extraction missed names, try to fill from Redis property_info_map."""
    if not props or all(p.get("name") for p in props):
        return props

    try:
        info_map = get_property_info_map(user_id)
        if not info_map:
            return props
        for i, prop in enumerate(props):
            if not prop.get("name") and i < len(info_map):
                prop["name"] = info_map[i].get("property_name", "")
    except Exception as e:
        logger.debug("property name enrichment failed: %s", e)
    return props


# ── Context detection flags ──────────────────────────────────────────────

def _detect_context(text: str, agent: str) -> dict:
    """Detect what kind of response this is. Returns context flags."""
    lower = text.lower()

    # Multi-property detection
    has_multi_bold = bool(re.search(r"\*\*[23456789]\.\s", text))
    has_multi_h3 = bool(re.search(r"^#{1,3}\s+[^\d\n]*[23456789]\.\s", text, re.MULTILINE))
    has_one_bold = bool(re.search(r"\*\*1\.\s", text))
    has_one_h3 = bool(re.search(r"^#{1,3}\s+[^\d\n]*1\.\s", text, re.MULTILINE))

    has_multi = has_multi_bold or has_multi_h3
    has_one = (has_one_bold or has_one_h3) and not has_multi

    return {
        "has_multi": has_multi,
        "has_one": has_one,
        "is_qualifying": (
            "quick —" in lower or "quick—" in lower
            or "must-haves from" in lower
            or "has some great options" in lower
            or "just share what matters" in lower
            or ("boys" in lower and "girls" in lower and "monthly budget" in lower)
        ),
        "is_comparison": (
            "comparison" in lower or "⚖" in lower
            or ("compare" in lower and not has_multi)
        ),
        "is_commute": (
            "commute" in lower or "🚗" in lower or "🚇" in lower
            or "by car" in lower or "by metro" in lower
        ),
        "is_visit_feedback": (
            "how was your visit" in lower
            or "how did the visit go" in lower
            or "how did it go" in lower
        ),
        "is_shortlisted": "shortlist" in lower or "saved" in lower,
        "is_area_info": (
            "neighborhood" in lower or "from what i know" in lower
            or ("area" in lower and "search" in lower)
        ),
        "is_property_detail": (
            "rent starts from" in lower or "here's what we have" in lower
            or "type: flat" in lower or "type: pg" in lower
            or "type: hostel" in lower or "type: co-living" in lower
            or ("₹" in lower and "/month" in lower and "📍" in lower)
        ),
        "is_confirmed": (
            "confirmed" in lower or "scheduled" in lower or "booked" in lower
        ),
        "is_payment": (
            "payment" in lower or "token" in lower
            or ("link" in lower and ("pay" in lower or "₹" in lower))
        ),
    }


# ── Chip generators per context ──────────────────────────────────────────

def _broker_chips(text: str, ctx: dict, user_id: str, locale: str) -> list[dict]:
    """Generate chips for broker agent responses."""
    chips = []
    props = _extract_listed_properties(text)
    props = _enrich_with_redis_names(props, user_id)

    if ctx["is_qualifying"]:
        # Qualifying question — no chips, let user type freely
        return []

    if ctx["is_visit_feedback"]:
        chips = [
            {"label": f"❤️ {_t('loved_it', locale)}", "action": "I loved it! I want to book this property", "icon": "heart"},
            {"label": f"🤔 {_t('was_okay', locale)}", "action": "It was okay, but I'm not sure yet", "icon": "think"},
            {"label": f"👎 {_t('not_for_me', locale)}", "action": "Not for me. The property didn't match my expectations", "icon": "thumbs_down"},
        ]
        return chips

    if ctx["is_comparison"] and len(props) >= 2:
        # After comparison — offer visit/shortlist for specific properties
        chips = [
            {"label": f"📅 Visit {props[0]['name'][:20]}", "action": f"Schedule a visit for {props[0]['name']}", "icon": "calendar"},
            {"label": f"📅 Visit {props[1]['name'][:20]}", "action": f"Schedule a visit for {props[1]['name']}", "icon": "calendar"},
            {"label": f"🔍 {_t('more_options', locale)}", "action": "Show me more options", "icon": "search"},
        ]
        return chips

    if ctx["has_multi"] and len(props) >= 2:
        # Multiple search results — smart chips with property names
        p1, p2 = props[0], props[1]
        chips = [
            {"label": f"📋 #{p1['num']} {_t('details', locale)}", "action": f"Tell me more about {p1['name']}", "icon": "info"},
            {"label": f"📅 #{p1['num']} {_t('visit', locale)}", "action": f"Schedule a visit for {p1['name']}", "icon": "calendar"},
            {"label": f"⚖️ #{p1['num']} vs #{p2['num']}", "action": f"Compare {p1['name']} and {p2['name']}", "icon": "compare"},
        ]
        if len(props) >= 3:
            chips.append({"label": f"📋 #{props[2]['num']} {_t('details', locale)}", "action": f"Tell me more about {props[2]['name']}", "icon": "info"})
        else:
            chips.append({"label": f"⭐ {_t('shortlist', locale)}", "action": f"Shortlist {p1['name']}", "icon": "star"})
        return chips

    if ctx["has_multi"]:
        # Fallback generic multi-property (name extraction failed)
        chips = [
            {"label": f"📋 {_t('details', locale)}", "action": "Tell me more about the first property", "icon": "info"},
            {"label": f"📅 {_t('visit', locale)}", "action": "Schedule a visit", "icon": "calendar"},
            {"label": f"⭐ {_t('shortlist', locale)}", "action": "Shortlist the first property", "icon": "star"},
            {"label": f"⚖️ {_t('compare', locale)}", "action": "Compare the top 2 properties", "icon": "compare"},
        ]
        return chips

    if ctx["is_commute"]:
        name = _extract_single_name(text)
        chips = [
            {"label": f"📅 {_t('visit', locale)}", "action": f"Schedule a visit for {name}" if name else "Schedule a visit", "icon": "calendar"},
            {"label": f"⭐ {_t('shortlist', locale)}", "action": f"Shortlist {name}" if name else "Shortlist this property", "icon": "star"},
            {"label": f"🔍 {_t('more_options', locale)}", "action": "Show me more options", "icon": "search"},
        ]
        return chips

    if ctx["has_one"]:
        name = props[0]["name"] if props else None
        chips = [
            {"label": f"📅 {_t('visit', locale)}", "action": f"Schedule a visit for {name}" if name else "Schedule a visit", "icon": "calendar"},
            {"label": f"⭐ {_t('shortlist', locale)}", "action": f"Shortlist {name}" if name else "Shortlist this property", "icon": "star"},
            {"label": f"🛏️ {_t('see_rooms', locale)}", "action": f"Show me room options for {name}" if name else "Show me room options and pricing", "icon": "bed"},
            {"label": f"📷 {_t('images', locale)}", "action": f"Show me photos of {name}" if name else "Show me photos", "icon": "camera"},
        ]
        return chips

    if ctx["is_shortlisted"]:
        name = _extract_single_name(text)
        chips = [
            {"label": f"📅 {_t('visit', locale)}", "action": f"Schedule a visit for {name}" if name else "Schedule a visit", "icon": "calendar"},
            {"label": f"🔍 {_t('more_options', locale)}", "action": "Show me more properties", "icon": "search"},
        ]
        return chips

    if ctx["is_area_info"]:
        chips = [
            {"label": f"🔍 {_t('search_here', locale)}", "action": "Search for PGs here", "icon": "search"},
            {"label": f"ℹ️ {_t('tell_more', locale)}", "action": "Tell me more about the area", "icon": "info"},
        ]
        return chips

    if ctx["is_property_detail"]:
        name = _extract_single_name(text)
        chips = [
            {"label": f"📅 {_t('visit', locale)}", "action": f"Schedule a visit for {name}" if name else "Schedule a visit", "icon": "calendar"},
            {"label": f"⭐ {_t('shortlist', locale)}", "action": f"Shortlist {name}" if name else "Shortlist this property", "icon": "star"},
            {"label": f"🛏️ {_t('see_rooms', locale)}", "action": f"Show me room options for {name}" if name else "Show me room options and pricing", "icon": "bed"},
            {"label": f"🚗 {_t('commute', locale)}", "action": f"How far is {name} from my office?" if name else "How far is this from my office?", "icon": "car"},
        ]
        return chips

    # Default broker chips
    chips = [
        {"label": f"🔍 {_t('search_pgs', locale)}", "action": "Show me properties in Mumbai", "icon": "search"},
        {"label": f"📍 {_t('diff_area', locale)}", "action": "Search in a different area", "icon": "location"},
    ]
    return chips


def _booking_chips(text: str, ctx: dict, locale: str) -> list[dict]:
    """Generate chips for booking agent responses."""
    chips = []

    if ctx["is_confirmed"]:
        chips.append({"label": f"📋 {_t('my_bookings', locale)}", "action": "Show my upcoming visits", "icon": "list"})
        chips.append({"label": f"🔍 {_t('browse_more', locale)}", "action": "Show me more properties", "icon": "search"})

    if ctx["is_payment"]:
        chips.append({"label": f"✅ {_t('ive_paid', locale)}", "action": "I have completed the payment", "icon": "check"})

    return chips


def _profile_chips(locale: str) -> list[dict]:
    """Generate chips for profile agent responses."""
    return [
        {"label": f"🔍 {_t('search_pgs', locale)}", "action": "Show me properties in Mumbai", "icon": "search"},
    ]


def _default_chips(text: str, ctx: dict, locale: str) -> list[dict]:
    """Generate chips for default agent responses."""
    # Default/greeting — offer search
    return [
        {"label": f"🔍 {_t('search_pgs', locale)}", "action": "Show me properties in Mumbai", "icon": "search"},
    ]


# ── Rich card generators ─────────────────────────────────────────────────

def _extract_date(text: str) -> str:
    """Extract a human-readable date from text."""
    # DD/MM/YYYY or DD-MM-YYYY
    m = re.search(r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text)
    if m:
        return m.group(1)
    # "1st March 2026" / "March 1, 2026" / "15th Jan" etc.
    m = re.search(
        r"(\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*(?:\s+\d{4})?)",
        text, re.IGNORECASE,
    )
    if m:
        return m.group(1)
    m = re.search(
        r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?)",
        text, re.IGNORECASE,
    )
    if m:
        return m.group(1)
    return ""


def _extract_time(text: str) -> str:
    """Extract a time string like '10:30 AM' from text."""
    m = re.search(r"(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))", text)
    if m:
        return m.group(1)
    m = re.search(r"(\d{1,2}\s*(?:AM|PM|am|pm))", text)
    if m:
        return m.group(1)
    return ""


def _extract_maps_link(text: str) -> str:
    """Extract a Google Maps link from text."""
    m = re.search(r"(https?://(?:www\.)?google\.com/maps\S+)", text)
    if m:
        return m.group(1)
    # Markdown link format: [text](url)
    m = re.search(r"\[.*?\]\((https?://(?:www\.)?google\.com/maps\S+?)\)", text)
    if m:
        return m.group(1)
    return ""


def _generate_status_card(text: str, ctx: dict, user_id: str, locale: str) -> dict | None:
    """Generate a status_card for confirmed milestones.

    These are the moments that matter — a visit confirmed, a property
    saved, a payment completed. They deserve more than a text line.
    """
    lower = text.lower()

    # ── Visit scheduled ──
    if ctx["is_confirmed"] and ("visit" in lower or "booking" in lower):
        prop_name = _extract_single_name(text) or ""
        date_str = _extract_date(text)
        time_str = _extract_time(text)
        maps_link = _extract_maps_link(text)

        details = []
        if date_str:
            details.append({"icon": "calendar", "text": date_str})
        if time_str:
            details.append({"icon": "clock", "text": time_str})
        if maps_link:
            details.append({"icon": "location", "text": "View on Maps", "url": maps_link})

        return {
            "type": "status_card",
            "status": "success",
            "icon": "calendar-check",
            "title": _t("visit", locale) + " " + (_t("confirm", locale) if locale != "en" else "Confirmed!"),
            "subtitle": prop_name,
            "details": details,
            "celebration": "confetti",
            "actions": [
                {"label": _t("my_bookings", locale), "action": "Show my upcoming visits", "style": "secondary"},
                {"label": _t("browse_more", locale), "action": "Show me more properties", "style": "secondary"},
            ],
        }

    # ── Property shortlisted ──
    if ctx["is_shortlisted"] and "success" in lower:
        prop_name = _extract_single_name(text) or ""
        return {
            "type": "status_card",
            "status": "success",
            "icon": "star",
            "title": _t("shortlist", locale) + ("ed!" if locale == "en" else "!"),
            "subtitle": prop_name,
            "details": [],
            "celebration": "heart",
            "actions": [
                {
                    "label": _t("visit", locale),
                    "action": f"Schedule a visit for {prop_name}" if prop_name else "Schedule a visit",
                    "style": "primary",
                },
                {"label": _t("more_options", locale), "action": "Show me more properties", "style": "secondary"},
            ],
        }

    # ── Payment link generated ──
    if ctx["is_payment"] and ("link" in lower or "generated" in lower or "pay now" in lower):
        prop_name = _extract_single_name(text) or ""
        # Extract amount
        amount_m = re.search(r"₹\s*([\d,]+)", text)
        amount = amount_m.group(0) if amount_m else ""
        # Extract payment URL
        pay_url_m = re.search(r"(https?://\S*(?:pay|razorpay|checkout)\S*)", text, re.IGNORECASE)
        pay_url = pay_url_m.group(1) if pay_url_m else ""

        details = []
        if amount:
            details.append({"icon": "wallet", "text": f"Amount: {amount}"})
        if prop_name:
            details.append({"icon": "home", "text": prop_name})

        if pay_url or amount:
            return {
                "type": "status_card",
                "status": "info",
                "icon": "wallet",
                "title": "Payment Ready",
                "subtitle": f"Token amount for {prop_name}" if prop_name else "Complete your payment",
                "details": details,
                "celebration": "checkmark",
                "actions": [
                    {"label": "Pay Now", "action": pay_url or "I want to proceed with payment", "style": "primary", "url": pay_url},
                    {"label": _t("ive_paid", locale), "action": "I have completed the payment", "style": "secondary"},
                ],
            }

    return None


def _generate_image_gallery(text: str, user_id: str) -> dict | None:
    """Generate an image_gallery part when property images are available.

    Images help users _feel_ a space — the room they'll sleep in, the
    kitchen they'll cook in. This is the closest to visiting in person.
    """
    lower = text.lower()
    if "image" not in lower and "photo" not in lower and "pic" not in lower:
        return None

    images = get_property_images_id(user_id)
    if not images:
        return None

    prop_name = _extract_single_name(text) or "Property"

    image_urls = []
    for img in images:
        if isinstance(img, dict):
            url = img.get("url", img.get("media_id", ""))
        else:
            url = str(img)
        if url and url.startswith("http"):
            image_urls.append({"url": url})

    if not image_urls:
        return None

    return {
        "type": "image_gallery",
        "property_name": prop_name,
        "images": image_urls[:10],
    }


def _generate_expandable_sections(text: str, user_id: str) -> dict | None:
    """Generate expandable sections for property detail responses.

    Property details have 25+ fields crammed into flat text. Collapsible
    sections let users scan the overview and expand only what they care
    about — amenities, house rules, FAQs, or property description.

    Data source: Redis property_info_map (already populated by
    fetch_property_details before this runs).
    """
    # Extract property name from the tool output header
    name_m = re.search(r"PROPERTY DETAILS:\s*(.+)", text)
    if not name_m:
        return None

    target_name = name_m.group(1).strip()

    # Look up structured data from info_map
    info_map = get_property_info_map(user_id)
    prop = None
    for p in info_map:
        pname = p.get("property_name", "").strip()
        if pname.lower() == target_name.lower():
            prop = p
            break
    if not prop:
        # Partial match fallback
        for p in info_map:
            pname = p.get("property_name", "").strip()
            if target_name.lower() in pname.lower() or pname.lower() in target_name.lower():
                prop = p
                break
    if not prop:
        return None

    sections = []

    # ── Amenities ──
    amenity_fields = ["amenities", "common_amenities", "food_amenities", "services_amenities"]
    all_amenities = []
    for field in amenity_fields:
        raw = prop.get(field, "")
        if raw and isinstance(raw, str):
            items = [a.strip() for a in raw.split(",") if a.strip()]
            all_amenities.extend(items)
        elif raw and isinstance(raw, list):
            all_amenities.extend([str(a).strip() for a in raw if a])

    if all_amenities:
        # Deduplicate while preserving order
        seen = set()
        unique = []
        for a in all_amenities:
            key = a.lower()
            if key not in seen:
                seen.add(key)
                unique.append(a)
        sections.append({
            "id": "amenities",
            "title": "Amenities & Facilities",
            "icon": "\U0001f3e0",
            "content_type": "pills",
            "items": unique,
        })

    # ── House Rules & Policies ──
    rules_kv = []
    kv_fields = [
        ("property_rules", "House Rules"),
        ("notice_period", "Notice Period"),
        ("agreement_period", "Agreement Period"),
        ("locking_period", "Locking Period"),
        ("checkin_time", "Check-in Time"),
        ("checkout_time", "Check-out Time"),
        ("gst_on_rent", "GST on Rent"),
    ]
    for field, label in kv_fields:
        val = prop.get(field, "")
        if val and isinstance(val, str) and val.strip():
            rules_kv.append({"label": label, "value": val.strip()})

    if rules_kv:
        sections.append({
            "id": "house_rules",
            "title": "House Rules & Policies",
            "icon": "\U0001f4cb",
            "content_type": "key_value",
            "items": rules_kv,
        })

    # ── FAQs ──
    faqs_raw = prop.get("faqs", "")
    faq_items = []
    if faqs_raw and isinstance(faqs_raw, str) and faqs_raw.strip():
        faq_items = _parse_faqs(faqs_raw)
    elif faqs_raw and isinstance(faqs_raw, list):
        for faq in faqs_raw:
            if isinstance(faq, dict):
                q = faq.get("question", faq.get("q", ""))
                a = faq.get("answer", faq.get("a", ""))
                if q and a:
                    faq_items.append({"question": q.strip(), "answer": a.strip()})

    if faq_items:
        sections.append({
            "id": "faqs",
            "title": "FAQs",
            "icon": "\u2753",
            "content_type": "qa",
            "items": faq_items,
        })

    # ── About ──
    about = prop.get("about", "")
    if about and isinstance(about, str) and about.strip() and len(about.strip()) > 20:
        sections.append({
            "id": "about",
            "title": "About This Property",
            "icon": "\u2139\ufe0f",
            "content_type": "text",
            "items": [about.strip()],
        })

    if not sections:
        return None

    return {
        "type": "expandable_sections",
        "property_name": target_name,
        "sections": sections,
    }


def _parse_faqs(raw: str) -> list[dict]:
    """Parse FAQ text into question/answer pairs.

    Handles common formats:
      - Q: question\\nA: answer
      - **Q:** question\\n**A:** answer
      - question?\\nanswer
    """
    pairs = []

    # Try Q:/A: pattern first
    qa_matches = re.findall(
        r"(?:\*\*)?Q[:\.]?\s*(?:\*\*)?\s*(.+?)(?:\*\*)?(?:\n|\r\n?)(?:\*\*)?A[:\.]?\s*(?:\*\*)?\s*(.+?)(?=(?:\n(?:\*\*)?Q[:\.])|$)",
        raw,
        re.IGNORECASE | re.DOTALL,
    )
    if qa_matches:
        for q, a in qa_matches:
            q, a = q.strip(), a.strip()
            if q and a:
                pairs.append({"question": q, "answer": a})
        return pairs

    # Try question? / answer pattern (lines ending with ?)
    lines = [l.strip() for l in raw.split("\n") if l.strip()]
    i = 0
    while i < len(lines) - 1:
        if lines[i].endswith("?"):
            pairs.append({"question": lines[i], "answer": lines[i + 1]})
            i += 2
        else:
            i += 1

    # Fallback: treat whole text as a single item
    if not pairs and raw.strip():
        pairs.append({"question": "Frequently Asked", "answer": raw.strip()})

    return pairs


def _generate_confirmation_card(text: str, ctx: dict, user_id: str, locale: str) -> dict | None:
    """Generate a confirmation_card when the bot asks the user to confirm an action.

    These are high-stakes moments: reserving a bed, confirming a visit time,
    proceeding with payment. The user needs to clearly see what they're
    agreeing to — property name, date/time, amount — before they commit.
    Reducing ambiguity reduces anxiety.
    """
    lower = text.lower()

    # Only trigger when the bot is ASKING for confirmation (not announcing one)
    is_asking = (
        "confirm" in lower
        or "shall i" in lower
        or "should i" in lower
        or "would you like to" in lower
        or "do you want" in lower
        or "proceed" in lower
        or "go ahead" in lower
    )
    # Don't trigger if it's already confirmed (status card handles that)
    already_confirmed = "confirmed" in lower or "successfully" in lower or "done" in lower
    if not is_asking or already_confirmed:
        return None

    prop_name = _extract_single_name(text) or ""
    date_str = _extract_date(text)
    time_str = _extract_time(text)

    # ── Visit confirmation ──
    # Require a date — CTAs like "Should I book a visit?" don't have a date,
    # so they won't trigger this card. Only actual scheduled-visit proposals do.
    if ("visit" in lower or "schedule" in lower) and date_str:
        details = []
        if prop_name:
            details.append({"icon": "home", "text": prop_name})
        if date_str:
            details.append({"icon": "calendar", "text": date_str})
        if time_str:
            details.append({"icon": "clock", "text": time_str})

        if details:
            return {
                "type": "confirmation_card",
                "title": "Confirm Your Visit",
                "subtitle": "We'll let the property manager know you're coming",
                "details": details,
                "confirm_action": "Yes, confirm the visit",
                "cancel_action": _t("change_time", locale),
                "style": "visit",
            }

    # ── Reservation / payment confirmation ──
    if "reserv" in lower or "payment" in lower or "token" in lower or "book" in lower:
        amount_m = re.search(r"₹\s*([\d,]+)", text)
        amount = amount_m.group(0) if amount_m else ""

        details = []
        if prop_name:
            details.append({"icon": "home", "text": prop_name})
        if amount:
            details.append({"icon": "wallet", "text": f"Amount: {amount}"})

        if details:
            return {
                "type": "confirmation_card",
                "title": "Confirm Reservation",
                "subtitle": "Token amount to reserve your bed" if amount else "Reserve your spot",
                "details": details,
                "confirm_action": "Yes, proceed with payment" if amount else "Yes, reserve the bed",
                "cancel_action": "No, cancel",
                "style": "payment",
            }

    return None


# ── Error card helper (used by main.py for pipeline/stream errors) ────────

def make_error_part(
    title: str = "Something went wrong",
    message: str = "We're having trouble right now. This usually resolves in a moment.",
    retry_label: str = "Try Again",
    retry_message: str = "",
) -> dict:
    """Create an error_card UI part for pipeline/streaming errors.

    Errors should feel warm and helpful, not alarming. The amber card
    communicates "we know, we're on it" — not "everything is broken".
    A retry button gives users agency instead of helplessness.
    """
    part = {
        "type": "error_card",
        "icon": "warning",
        "title": title,
        "message": message,
    }
    if retry_label and retry_message:
        part["retry_action"] = retry_label
        part["retry_message"] = retry_message
    return part


# ── Main entry point ─────────────────────────────────────────────────────

def generate_ui_parts(
    response_text: str,
    agent_name: str,
    user_id: str,
    locale: str = "en",
) -> list[dict]:
    """Generate UI parts to append to the parts[] array.

    Called after the agent produces its text response. Analyzes the response
    + context to produce structured UI parts the frontend renders.

    Returns list of part dicts (may be empty).
    """
    if not response_text or not response_text.strip():
        return []

    # Normalize locale
    locale = locale if locale in _LABELS else "en"

    # Detect response context
    ctx = _detect_context(response_text, agent_name)

    parts = []

    # ── Rich cards (status card, image gallery) — before chips ──
    try:
        status = _generate_status_card(response_text, ctx, user_id, locale)
        if status:
            parts.append(status)
    except Exception as e:
        logger.warning("status_card generation failed: %s", e)

    try:
        gallery = _generate_image_gallery(response_text, user_id)
        if gallery:
            parts.append(gallery)
    except Exception as e:
        logger.warning("image_gallery generation failed: %s", e)

    try:
        confirm = _generate_confirmation_card(response_text, ctx, user_id, locale)
        if confirm:
            parts.append(confirm)
    except Exception as e:
        logger.warning("confirmation_card generation failed: %s", e)

    # ── Expandable sections — collapsible amenities/rules/FAQ for property details ──
    if ctx.get("is_property_detail"):
        try:
            exp = _generate_expandable_sections(response_text, user_id)
            if exp:
                parts.append(exp)
        except Exception as e:
            logger.warning("expandable_sections generation failed: %s", e)

    # ── Quick reply chips — always last so they appear below cards ──
    chips = []
    if agent_name == "broker":
        chips = _broker_chips(response_text, ctx, user_id, locale)
    elif agent_name == "booking":
        chips = _booking_chips(response_text, ctx, locale)
    elif agent_name == "profile":
        chips = _profile_chips(locale)
    elif agent_name == "default":
        chips = _default_chips(response_text, ctx, locale)

    # If we generated a status card or confirmation card, suppress default chips
    has_card = any(p["type"] in ("status_card", "confirmation_card") for p in parts)
    if parts and has_card:
        chips = []  # card has its own actions

    if chips:
        parts.append({"type": "quick_replies", "chips": chips})

    return parts
