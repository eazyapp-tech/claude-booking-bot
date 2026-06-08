"""Keyword safety net for agent routing.

The supervisor (Haiku) sometimes misclassifies messages as "default" when
they clearly belong to broker / booking / profile.  This module provides
a deterministic fallback that catches those misroutes.

Three phases, each more permissive:
  Phase 1 — Multi-word phrases (resolve ambiguous words by context)
  Phase 2 — Single words (word-boundary matching, not substring)
  Phase 3 — Last-agent fallback for continuations
"""

import re

from core.log import get_logger
from db.redis_store import get_last_agent

logger = get_logger("core.router")

# ---------------------------------------------------------------------------
# Phase 1: Multi-word phrases (highest confidence)
# These resolve words that are ambiguous at the single-word level.
#   "shortlist this property" (ACTION → broker)  vs  "show shortlisted" (QUERY → profile)
#   "schedule a visit" (ACTION → booking)         vs  "my visits" (QUERY → profile)
# ---------------------------------------------------------------------------

PROFILE_PHRASES = [
    "my visit", "my visits", "my booking", "my bookings",
    "my schedule", "my event", "my events",
    "my preference", "my preferences", "my profile",
    "shortlisted properties", "saved properties",
    "booking status", "visit status", "scheduled event",
    # Shortlist query forms — must come before Phase 2 BROKER_WORDS catches "shortlist"
    "what did i shortlist", "did i shortlist", "i shortlisted",
    "my shortlist", "what i shortlisted",
]

BROKER_PHRASES = [
    "more about", "tell me about",
    "details of", "details about", "details for",
    "images of", "photos of", "pictures of",
    "far from", "distance from", "distance to",
    "shortlist this", "shortlist the",
]

# ---------------------------------------------------------------------------
# Phase 2: Single-word matching (word boundary, not substring)
# ---------------------------------------------------------------------------

PROFILE_WORDS = {
    "profile", "preference", "preferences", "upcoming",
    "events", "visits", "bookings", "shortlisted",
}

BOOKING_WORDS = {
    "visit", "schedule", "book", "appointment", "call", "video",
    "tour", "payment", "pay", "token", "kyc", "aadhaar", "otp",
    "reserve", "cancel", "reschedule",
}

BROKER_WORDS = {
    "find", "search", "looking", "property", "properties",
    "pg", "flat", "apartment", "hostel", "coliving", "co-living",
    "room", "rent", "budget", "area", "location", "available",
    "recommend", "suggest", "bhk", "1bhk", "2bhk", "rk",
    "single", "double", "girls", "boys", "sharing",
    "place", "stay", "accommodation", "housing", "near", "nearby",
    "shortlist", "details", "images", "photos",
    "landmark", "landmarks", "distance", "far",
    # Hindi/Hinglish
    "kamra", "kiraya", "ghar", "chahiye", "dikhao", "jagah", "rehne",
    # Marathi
    "खोली", "भाडे", "जागा", "पाहिजे", "दाखवा", "शोधा", "बुकिंग",
}

# ---------------------------------------------------------------------------
# Phase 3: Last-agent fallback constants
# ---------------------------------------------------------------------------

AFFIRMATIVES = {
    "yes", "ok", "okay", "sure", "go ahead", "please",
    "yeah", "yep", "yup", "haan", "ha", "theek hai",
    "kar do", "ho jayega", "confirm", "done", "proceed",
    # Marathi
    "हो", "चालेल", "ठीक आहे",
}

NEW_INTENT_WORDS = {
    "hello", "hi", "hey", "howdy", "namaste",
    "thanks", "thank", "bye", "goodbye",
    "what", "who", "where", "when", "how", "why", "which",
}


def apply_keyword_safety_net(
    agent_name: str,
    message: str,
    user_id: str,
) -> str:
    """Override ``agent_name`` when the supervisor misroutes to "default".

    Returns the (possibly corrected) agent name.  Only acts when
    ``agent_name == "default"`` — if the supervisor already picked a
    specific agent, this function is a no-op.
    """
    if agent_name != "default":
        return agent_name

    msg_lower = message.lower()
    # Normalize: strip punctuation so word matching works on "PG!" → "pg"
    msg_clean = re.sub(r"[^\w\s-]", " ", msg_lower)
    words = set(msg_clean.split())

    # --- Phase 1: Multi-word phrases ---
    if any(p in msg_lower for p in PROFILE_PHRASES):
        agent_name = "profile"
    elif any(p in msg_lower for p in BROKER_PHRASES):
        agent_name = "broker"
    else:
        # --- Phase 2: Single-word matching ---
        if words & PROFILE_WORDS:
            agent_name = "profile"
        elif words & BOOKING_WORDS:
            agent_name = "booking"
        elif words & BROKER_WORDS:
            agent_name = "broker"

    # --- Phase 3: Last-agent fallback for continuations ---
    if agent_name == "default":
        last = get_last_agent(user_id)
        if last and last != "default":
            msg_stripped = msg_lower.strip().rstrip(".!,?")
            is_new_intent = bool(words & NEW_INTENT_WORDS)
            if msg_stripped in AFFIRMATIVES or (len(message.split()) <= 5 and not is_new_intent):
                agent_name = last
                logger.debug("last_agent fallback → %s", last)

    return agent_name
