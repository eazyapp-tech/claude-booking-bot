"""
db/redis/user.py — User preferences, identity, language, KYC, and cross-session memory.

Covers:
  - User preferences
  - User name / phone / language
  - No-message flag
  - KYC (Aadhaar) fields
  - Full cross-session memory (persona, lead score, deal-breakers, funnel)
"""

import json
import time
from datetime import date
from typing import Optional

from db.redis._base import _r, _json_set, _json_get, LANGUAGE_TTL


# ---------------------------------------------------------------------------
# User preferences
# ---------------------------------------------------------------------------

def save_preferences(user_id: str, data: dict, profile_name: str | None = None) -> None:
    if profile_name:
        data["profile_name"] = profile_name
    # Ensure bytes values are decoded for JSON compatibility
    clean = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in data.items()}
    _json_set(f"{user_id}:preferences", clean)


def get_preferences(user_id: str) -> dict:
    return _json_get(f"{user_id}:preferences", default={})


# ---------------------------------------------------------------------------
# User name
# ---------------------------------------------------------------------------

def set_user_name(user_id: str, name: str) -> None:
    _r().set(f"{user_id}:user_name", name)


def get_user_name(user_id: str) -> Optional[str]:
    raw = _r().get(f"{user_id}:user_name")
    return raw.decode() if raw else None


# ---------------------------------------------------------------------------
# User phone number (web-chat users don't have a phone in user_id)
# ---------------------------------------------------------------------------

def set_user_phone(user_id: str, phone: str) -> None:
    """Store the user's real 10-digit phone number (collected from web chat)."""
    _r().set(f"{user_id}:user_phone", phone)


def get_user_phone(user_id: str) -> Optional[str]:
    """
    Returns the user's 10-digit local phone number.

    Priority order:
      1. Explicitly stored phone (set_user_phone — used by web chat).
      2. Derived from user_id when user_id IS a phone number (WhatsApp channel:
         user_id = 12-digit international format like 919876543210).

    Returns None if no valid phone can be determined (e.g. web-chat user who
    hasn't provided their number yet).
    """
    stored = _r().get(f"{user_id}:user_phone")
    if stored:
        return stored.decode()
    # WhatsApp fallback: user_id IS a phone number (pure digits, 10-13 chars)
    # Web-chat IDs like "uat_k7x2m9qf" contain non-digits → skip fallback
    if user_id.isdigit() and 10 <= len(user_id) <= 13:
        return user_id[-10:]
    return None


# ---------------------------------------------------------------------------
# No-message flag
# ---------------------------------------------------------------------------

def set_no_message(user_id: str) -> None:
    _r().set(f"{user_id}:no_message", "1")


def get_no_message(user_id: str) -> str:
    raw = _r().get(f"{user_id}:no_message")
    return raw.decode() if raw else "0"


def clear_no_message(user_id: str) -> None:
    _r().delete(f"{user_id}:no_message")


# ---------------------------------------------------------------------------
# User language preference
# ---------------------------------------------------------------------------

def set_user_language(user_id: str, lang: str) -> None:
    """Store detected/selected language. TTL = 24h (same as conversation)."""
    _r().set(f"{user_id}:language", lang, ex=LANGUAGE_TTL)


def get_user_language(user_id: str) -> str:
    """Return stored language or 'en' default."""
    raw = _r().get(f"{user_id}:language")
    return raw.decode() if raw else "en"


# ---------------------------------------------------------------------------
# KYC data (Aadhaar)
# ---------------------------------------------------------------------------

def set_aadhar_user_name(user_id: str, name: str) -> None:
    _r().set(f"{user_id}:aadhar_name", name)


def get_aadhar_user_name(user_id: str) -> Optional[str]:
    raw = _r().get(f"{user_id}:aadhar_name")
    return raw.decode() if raw else None


def delete_aadhar_user_name(user_id: str) -> None:
    _r().delete(f"{user_id}:aadhar_name")


def set_aadhar_gender(user_id: str, gender: str) -> None:
    _r().set(f"{user_id}:aadhar_gender", gender)


def get_aadhar_gender(user_id: str) -> Optional[str]:
    raw = _r().get(f"{user_id}:aadhar_gender")
    return raw.decode() if raw else None


def delete_aadhar_gender(user_id: str) -> None:
    _r().delete(f"{user_id}:aadhar_gender")


# ---------------------------------------------------------------------------
# Cross-session user memory (persistent — no TTL)
# ---------------------------------------------------------------------------

_MEMORY_DEFAULTS = {
    "first_seen": "",
    "last_seen": "",
    "session_count": 0,
    "properties_viewed": [],       # list of prop_ids
    "properties_shortlisted": [],  # list of prop_ids
    "properties_rejected": [],     # list of {"prop_id": ..., "traits": [...]}
    "visits_scheduled": [],        # list of prop_ids
    "deal_breakers": [],           # inferred: ["no AC", "far from metro"]
    "must_haves": [],              # inferred: ["AC", "WiFi"]
    "urgency": "",                 # inferred: "immediate" | "near" | "flexible" | ""
    "decision_authority": "",      # inferred: "self" | "family" | ""
    "lifestyle_tags": [],          # inferred: ["night_schedule", "has_vehicle", ...]
    "inferred_needs": [],          # derived from lifestyle_tags: ["parking", "24hr_access", ...]
    "topic_frequency": {},         # inferred: {"commute": 2, "price": 3, ...}
    "roommate_prefs": {},          # inferred: {"veg_only": True, "no_smoking": True, ...}
    "inferred_must_haves": [],     # inferred from strong-want language: ["AC", "WiFi"]
    "nice_to_haves": [],           # inferred from mild-want language: ["gym", "meals"]
    "amenity_freq": {},            # mention count per amenity across conversation
    "lead_score": 0,
    "last_search_location": "",
    "last_search_budget": "",
    "phone_collected": False,
    "funnel_max": "",              # highest funnel stage reached
    "persona": "",                 # "professional", "student", "family", or ""
}

# ---------------------------------------------------------------------------
# Persona detection keywords
# ---------------------------------------------------------------------------
_PERSONA_SIGNALS = {
    "professional": [
        "office", "work", "workplace", "company", "commute", "job",
        "corporate", "business park", "tech park", "it park", "bkc",
        "salary", "professional",
    ],
    "student": [
        "college", "university", "campus", "studies", "student",
        "hostel", "studying", "course", "engineering", "medical",
        "iit", "nit", "bits", "vit", "manipal", "amity",
    ],
    "family": [
        "family", "kids", "children", "school", "wife", "husband",
        "spouse", "parents", "daughter", "son", "married",
    ],
}


def detect_persona(text: str) -> str:
    """Detect user persona from conversation text. Returns persona string or empty."""
    lower = text.lower()
    scores = {"professional": 0, "student": 0, "family": 0}
    for persona, keywords in _PERSONA_SIGNALS.items():
        for kw in keywords:
            if kw in lower:
                scores[persona] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] >= 1 else ""


def update_persona(user_id: str, text: str) -> str:
    """Detect persona from text and persist if found (doesn't downgrade existing)."""
    detected = detect_persona(text)
    if not detected:
        return ""
    mem = get_user_memory(user_id)
    current = mem.get("persona", "")
    if not current:
        update_user_memory(user_id, persona=detected)
    return detected or current


FUNNEL_ORDER = (
    "search", "detail", "shortlist", "visit", "booking",
    "visit_attended", "booking_initiated", "payment_completed",
)


def get_user_memory(user_id: str) -> dict:
    """Return persistent cross-session memory for a user."""
    data = _json_get(f"{user_id}:user_memory")
    if data is None:
        return dict(_MEMORY_DEFAULTS)
    # Ensure all keys exist (forward compat if we add new fields)
    merged = dict(_MEMORY_DEFAULTS)
    merged.update(data)
    return merged


def save_user_memory(user_id: str, memory: dict) -> None:
    """Persist user memory (no TTL — survives across sessions)."""
    _json_set(f"{user_id}:user_memory", memory)


def update_user_memory(user_id: str, **updates) -> dict:
    """Merge updates into existing memory, recalculate lead score, and save.

    Convenience wrapper: ``update_user_memory(uid, session_count=mem["session_count"]+1)``
    """
    mem = get_user_memory(user_id)
    mem.update(updates)

    # Always refresh last_seen
    mem["last_seen"] = date.today().isoformat()
    if not mem["first_seen"]:
        mem["first_seen"] = mem["last_seen"]

    # Recalculate lead score
    mem["lead_score"] = _calculate_lead_score(mem, user_id=user_id)

    # Update funnel_max
    for stage in reversed(FUNNEL_ORDER):
        if stage == "visit" and mem.get("visits_scheduled"):
            mem["funnel_max"] = _max_funnel(mem.get("funnel_max", ""), "visit")
        elif stage == "shortlist" and mem.get("properties_shortlisted"):
            mem["funnel_max"] = _max_funnel(mem.get("funnel_max", ""), "shortlist")
        elif stage == "search" and mem.get("properties_viewed"):
            mem["funnel_max"] = _max_funnel(mem.get("funnel_max", ""), "search")

    save_user_memory(user_id, mem)
    return mem


def _max_funnel(current: str, new: str) -> str:
    """Return the deeper funnel stage."""
    cur_idx = FUNNEL_ORDER.index(current) if current in FUNNEL_ORDER else -1
    new_idx = FUNNEL_ORDER.index(new) if new in FUNNEL_ORDER else -1
    return FUNNEL_ORDER[max(cur_idx, new_idx)] if max(cur_idx, new_idx) >= 0 else current


def _calculate_lead_score(mem: dict, user_id: str = "") -> int:
    """Score 0-100 based on engagement signals. Higher = hotter lead."""
    score = 0

    # Session engagement (max 20)
    score += min(20, mem.get("session_count", 0) * 5)

    # Properties explored (max 15)
    score += min(15, len(mem.get("properties_viewed", [])) * 2)

    # Shortlisted (max 15)
    score += min(15, len(mem.get("properties_shortlisted", [])) * 5)

    # Visits scheduled (max 20)
    score += min(20, len(mem.get("visits_scheduled", [])) * 10)

    # Phone collected (10) — only award for web users; WhatsApp UIDs are phone numbers (all digits)
    is_web_user = "-" in user_id  # UUID format (web) vs all-digit WhatsApp number
    if mem.get("phone_collected") and is_web_user:
        score += 10

    # Funnel depth bonus — deeper stages override shallower (not additive)
    FUNNEL_BONUS = {
        "booking_initiated": 15,
        "payment_completed": 30,
        "visit_attended": 20,
    }
    funnel_stage = mem.get("funnel_max", "")
    score += FUNNEL_BONUS.get(funnel_stage, 0)

    # Preferences completeness (max 10)
    loc = mem.get("last_search_location", "")
    budget = mem.get("last_search_budget", "")
    if loc:
        score += 5
    if budget:
        score += 5

    # Recency decay: -5 per week of inactivity (max -20)
    last_seen = mem.get("last_seen", "")
    if last_seen:
        try:
            days_inactive = (date.today() - date.fromisoformat(last_seen)).days
            weeks_inactive = days_inactive // 7
            score -= min(20, weeks_inactive * 5)
        except (ValueError, TypeError):
            pass  # last_seen may be missing or malformed — skip decay, keep base score

    return max(0, min(100, score))


def get_lead_temperature(score: int) -> str:
    """Classify lead score into temperature."""
    if score >= 70:
        return "hot"
    if score >= 40:
        return "warm"
    return "cold"


def record_property_viewed(user_id: str, prop_id: str) -> None:
    """Record that user viewed/was shown a property."""
    if not prop_id:
        return
    mem = get_user_memory(user_id)
    viewed = mem.get("properties_viewed", [])
    if prop_id not in viewed:
        viewed.append(prop_id)
        mem["properties_viewed"] = viewed[-50:]  # cap at 50
    update_user_memory(user_id, properties_viewed=mem["properties_viewed"])


def record_property_shortlisted(user_id: str, prop_id: str) -> None:
    """Record that user shortlisted a property."""
    if not prop_id:
        return
    mem = get_user_memory(user_id)
    shortlisted = mem.get("properties_shortlisted", [])
    if prop_id not in shortlisted:
        shortlisted.append(prop_id)
        mem["properties_shortlisted"] = shortlisted[-20:]
    update_user_memory(user_id, properties_shortlisted=mem["properties_shortlisted"])


def record_visit_scheduled(user_id: str, prop_id: str) -> None:
    """Record that user scheduled a visit."""
    if not prop_id:
        return
    mem = get_user_memory(user_id)
    visits = mem.get("visits_scheduled", [])
    if prop_id not in visits:
        visits.append(prop_id)
        mem["visits_scheduled"] = visits[-20:]
    update_user_memory(user_id, visits_scheduled=mem["visits_scheduled"])


def add_deal_breaker(user_id: str, deal_breaker: str) -> None:
    """Add an inferred deal-breaker (e.g., 'no AC')."""
    if not deal_breaker:
        return
    mem = get_user_memory(user_id)
    dbs = mem.get("deal_breakers", [])
    if deal_breaker.lower() not in [d.lower() for d in dbs]:
        dbs.append(deal_breaker)
        mem["deal_breakers"] = dbs[-10:]  # cap at 10
    save_user_memory(user_id, mem)


def build_returning_user_context(user_id: str) -> str:
    """Build a prompt-injectable summary of the returning user's history.

    Returns empty string for new users (no memory).
    """
    from db.redis.property import get_last_search_results

    mem = get_user_memory(user_id)
    if not mem.get("first_seen") or mem.get("session_count", 0) < 1:
        return ""

    # Compute days since last interaction for freshness markers
    days_since = 0
    last_seen_str = mem.get("last_seen", "")
    if last_seen_str:
        try:
            days_since = (date.today() - date.fromisoformat(last_seen_str)).days
        except (ValueError, TypeError):
            pass  # last_seen missing or malformed — days_since stays 0, no freshness warning

    parts = []
    parts.append(f"RETURNING USER (session #{mem['session_count'] + 1}):")

    # Freshness / staleness markers (prevents context distraction + poisoning)
    if days_since > 30:
        parts.append(
            f"⚠️ STALE CONTEXT ({days_since} days since last visit): "
            "Treat the following preferences as background only — re-qualify budget and location before searching."
        )
    elif days_since > 7:
        parts.append(
            f"Note: preferences last updated {days_since} days ago — "
            "confirm budget/location are still current before searching."
        )

    loc = mem.get("last_search_location", "")
    budget = mem.get("last_search_budget", "")
    if loc:
        search_info = f"Last searched: {loc}"
        if budget:
            search_info += f", budget {budget}"
        parts.append(search_info)

    n_viewed = len(mem.get("properties_viewed", []))
    n_short = len(mem.get("properties_shortlisted", []))
    n_visits = len(mem.get("visits_scheduled", []))
    if n_viewed or n_short or n_visits:
        engagement = []
        if n_viewed:
            engagement.append(f"{n_viewed} viewed")
        if n_short:
            engagement.append(f"{n_short} shortlisted")
        if n_visits:
            engagement.append(f"{n_visits} visits scheduled")
        parts.append("Properties: " + ", ".join(engagement))

    persona = mem.get("persona", "")
    if persona:
        parts.append(f"Persona: {persona}")

    dbs = mem.get("deal_breakers", [])
    if dbs:
        parts.append(f"Deal-breakers: {', '.join(dbs)}")

    must = mem.get("must_haves", [])
    inferred_must = mem.get("inferred_must_haves", [])
    all_must = sorted(set(must) | set(inferred_must))
    if all_must:
        label = "Must-haves"
        if inferred_must and not must:
            label = "Must-haves (inferred from conversation)"
        elif inferred_must and must:
            label = "Must-haves (stated + inferred)"
        parts.append(f"{label}: {', '.join(all_must)}")

    nice = mem.get("nice_to_haves", [])
    if nice:
        parts.append(f"Nice-to-haves: {', '.join(nice)}")

    urgency = mem.get("urgency", "")
    if urgency:
        _urgency_label = {
            "immediate": "URGENT — needs to move ASAP",
            "near": "Near-term — within a week or two",
            "flexible": "Flexible timeline",
        }
        parts.append(f"Move-in urgency: {_urgency_label.get(urgency, urgency)}")

    authority = mem.get("decision_authority", "")
    if authority == "family":
        parts.append(
            "Decision authority: FAMILY INVOLVED — offer to share a summary card; "
            "don't push hard closes, they need to consult at home first"
        )

    roommate = mem.get("roommate_prefs", {})
    if roommate:
        flags = []
        _labels = {
            "veg_only":          "veg-only rooms preferred",
            "no_smoking":        "no smoking in room",
            "professional_only": "working professionals only",
            "student_friendly":  "student crowd okay",
            "no_curfew":         "no curfew required",
            "regional_preference": "same-community/language preference",
            "non_veg_ok":        "non-veg okay",
        }
        for key, label in _labels.items():
            if roommate.get(key):
                flags.append(label)
        if flags:
            parts.append(f"Roommate preferences: {', '.join(flags)}")

    lifestyle = mem.get("lifestyle_tags", [])
    inferred_needs = mem.get("inferred_needs", [])
    if lifestyle:
        parts.append(f"Lifestyle: {', '.join(lifestyle)}")
    if inferred_needs:
        # Only surface needs that map to visible amenities — skip non-amenity tags
        from core.signal_extractor import NEED_TO_AMENITY
        surfaceable = [n for n in inferred_needs if NEED_TO_AMENITY.get(n)]
        context_only = [n for n in inferred_needs if not NEED_TO_AMENITY.get(n)]
        if surfaceable:
            parts.append(f"Implied amenity needs (from lifestyle): {', '.join(surfaceable)}")
        if context_only:
            parts.append(f"Context needs (surface in conversation): {', '.join(context_only)}")

    score = mem.get("lead_score", 0)
    temp = get_lead_temperature(score)
    parts.append(f"Lead: {temp} ({score}/100)")

    if temp == "hot":
        parts.append("→ Use urgency and push for booking/visit NOW")
    elif temp == "warm":
        parts.append("→ Engage warmly, highlight new options, nudge toward action")
    else:
        parts.append("→ Be educational, build trust, don't push too hard")

    if loc and budget:
        parts.append("→ Skip qualifying questions — go straight to search or pick up where they left off")

    # Inject last search results for cross-session context. Property names come
    # from the Rentok API (third-party text) — fence them so a malicious listing
    # name replayed from memory can't act as an instruction. Matches the fence
    # applied when these names are first surfaced in tools/broker/search.py.
    last_search = get_last_search_results(user_id)
    if last_search:
        names = ", ".join(p["property_name"] for p in last_search if p.get("property_name"))
        if names:
            from core.untrusted import fence
            fenced = fence(names, "property listing names from a prior Rentok search")
            parts.append(f"Last search results (cached in property_info_map):\n{fenced}")

    return "\n".join(parts)
