"""
core/signal_extractor.py — Inference-based lead signal extraction.

Runs as a background asyncio.create_task after each agent turn. Reads recent
conversation messages and writes inferred signals to user memory — no LLM call,
no network I/O beyond the existing Redis user_memory write.

Six signal families:
  urgency             — how soon the lead needs to move (immediate/near/flexible)
  decision_authority  — who has final say (self/family)
  lifestyle_tags      — implicit lifestyle context (night_schedule, student, has_vehicle, values_privacy)
  topic_frequency     — which topics the lead keeps returning to (price, commute, safety, …)
  roommate_prefs      — compatibility requirements for shared rooms (veg, no-smoking, professional-only, …)
  inferred_needs      — amenity/feature needs inferred from lifestyle_tags (no questionnaire needed)

Design rules:
  - Pure extraction functions take list[dict] messages → return signals
    (testable without Redis or network)
  - extract_and_update() is the thin async I/O wrapper that persists results
  - Never raises: any exception is swallowed so a bad signal run never kills a turn
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger("signal_extractor")


# ---------------------------------------------------------------------------
# Pattern tables (all lowercase match targets)
# ---------------------------------------------------------------------------

_URGENCY_IMMEDIATE = [
    "asap", "urgent", "urgently", "today", "tomorrow", "this week",
    "immediately", "abhi", "jaldi", "jald", "abhi chahiye", "bahut jaldi",
    "shifting soon", "shifting this week", "shifting tomorrow", "move in this week",
    "aaj", "kal", "parso", "1-2 days", "2-3 days", "right away",
    "emergency", "need it now", "need asap",
]

_URGENCY_NEAR = [
    "this month", "next week", "couple of weeks", "in a week",
    "2 weeks", "3 weeks", "by end of month", "month end",
    "is mahine", "is hafte", "agle hafte",
]

_URGENCY_FLEXIBLE = [
    "next month", "exploring", "just looking", "not sure yet",
    "no rush", "take my time", "sometime", "whenever",
    "dekh raha hoon", "dekhte hain", "soch raha hoon",
    "planning for", "planning to move", "eventually", "in future",
]

_FAMILY_SIGNALS = [
    "mummy", "papa", "dad", "mom", "parents", "family",
    "wife", "husband", "spouse", "brother", "sister",
    "ghar waale", "ghar wale", "gharwale", "approval",
    "will ask", "will check", "let me ask", "discuss with",
    "show them", "show her", "show him", "they need to see",
    "need to tell", "bata ke aaunga", "dekhna hai unhe",
]

# Topics are matched as whole words where possible
_TOPIC_PATTERNS: dict[str, list[str]] = {
    "commute": [
        "how far", r"\bkm\b", "kilometre", "kilometer", "minutes", "mins",
        "station", "metro", "commute", "office", "distance", "travel time",
        "how long", "how much time", "kaise jaate", "kitni door",
    ],
    "price": [
        r"expensive", r"\bcheap\b", "cheaper", "budget", "how much", r"₹", r"\brs\b",
        "rent", "cost", r"\bprice\b", "affordable", "costly", "mahanga",
        "sasta", "kam rent", "kam budget", "kitna rent",
    ],
    "safety": [
        "safe", "safety", "secure", "security", "gated", "guard",
        "cctv", "ladies only", "girls only", "safe area", "safe locality",
        "surakshit", "crime",
    ],
    "social": [
        "who lives", "tenants", "people", "roommate", "flatmates",
        "working professionals", "students only", "mix", "vibe",
        "atmosphere", "kitne log", "kaun log",
    ],
    "amenity": [
        r"\bac\b", "air condition", "wifi", "wi-fi", "meals", "food",
        "gym", "laundry", "wash", "hot water", "geyser", "housekeeping",
        "cleaning", "parking", "lift", "elevator", "power backup",
    ],
    "quality": [
        "nice", "clean", "maintained", "good condition", "well kept",
        "modern", "new", "fresh", "spacious", "acha", "sahi",
        "badhiya", "sundar", "clean",
    ],
    "deposit": [
        "deposit", "security deposit", "advance", "security", "refund",
        "kitna deposit", "jyada deposit", "deposit wapas",
    ],
}

_LIFESTYLE_RULES: dict[str, list[str]] = {
    "night_schedule": [
        "late night", "night shift", "after midnight", r"\b12am\b", r"\b1am\b",
        r"\b2am\b", "raat ko", "late aa jaata", "night duty",
    ],
    "student": [
        "study room", "library", "college nearby", "hostel-like", "hostel like",
        "college", "university", "campus", r"\bstudent\b", "semester",
        "iit", "nit", "engineering", "medical college",
    ],
    "has_vehicle": [
        "parking", r"\bbike\b", r"\bcar\b", "two-wheeler", "two wheeler",
        "gaadi", "scooter", "activa", "vehicle",
    ],
    "values_privacy": [
        "attached bathroom", "single room", r"\bprivate\b", "attached bath",
        "attach", "own bathroom", "private bathroom", "single occupancy",
        "single sharing",
    ],
}

# ---------------------------------------------------------------------------
# Roommate compatibility signals
# ---------------------------------------------------------------------------

# Each key maps to True if the signal fires (presence = preference expressed)
_ROOMMATE_PATTERNS: dict[str, list[str]] = {
    "veg_only": [
        r"\bveg\b", "vegetarian", "veg only", "pure veg", "no non-veg",
        "no meat", "no chicken", "jain", "satvik", "shakahari",
    ],
    "non_veg_ok": [
        r"\bnon.?veg\b", "non vegetarian", "chicken", "mutton", "egg",
        "meat", "non veg is fine", "non veg allowed",
    ],
    "no_smoking": [
        "no smoking", "non smoking", "no smokers", "don't smoke",
        "i don't smoke", "smoking nahi", "smoking free", "dhoomrapan",
    ],
    "professional_only": [
        "working professional", "professionals only", "office going",
        "only professionals", "no students", "corporate crowd",
        "it professionals", "salaried",
    ],
    "student_friendly": [
        "students okay", "college students", "student crowd",
        "student atmosphere", "studious environment", "college crowd okay",
    ],
    "no_curfew": [
        "no curfew", "24 hour", "24hr", "any time", "flexible timing",
        "anytime", "late night allowed", "no timing restriction",
        "koi timing nahi", "curfew nahi chahiye",
    ],
    "regional_preference": [
        "gujarati", "marwari", "south indian", "tamil", "telugu", "malayali",
        "bengali", "punjabi", "marathi", "same community", "same language",
        "hindi speaking", "hindi medium",
    ],
}

# ---------------------------------------------------------------------------
# Inferred needs: lifestyle_tag → list of implied amenity/feature needs
# These are soft signals (not hard filters) — factored into scoring at
# lower weight than explicit must_have_amenities.
# ---------------------------------------------------------------------------

LIFESTYLE_TO_NEEDS: dict[str, list[str]] = {
    "night_schedule": ["24hr_access", "no_curfew", "quiet_building"],
    "student":        ["wifi", "study_area", "no_strict_curfew"],
    "has_vehicle":    ["parking"],
    "values_privacy": ["attached_bathroom", "max_2_sharing"],
}

# Map inferred need labels → fuzzy amenity strings that match_score already understands
NEED_TO_AMENITY: dict[str, str] = {
    "wifi":             "internet",
    "parking":          "parking",
    "attached_bathroom": "attached bathroom",
    "study_area":       "study room",
    # These have no direct Rentok amenity — surfaced in prompt only
    "24hr_access":      "",
    "no_curfew":        "",
    "no_strict_curfew": "",
    "quiet_building":   "",
    "max_2_sharing":    "",
}

# ---------------------------------------------------------------------------
# Amenity preference extraction — must-have / nice-to-have / deal-breaker
# Inferred from tone + amenity co-occurrence in the same message or nearby turns.
# ---------------------------------------------------------------------------

# Canonical amenity labels (normalised across aliases)
_AMENITY_PATTERNS: dict[str, list[str]] = {
    "AC":               [r"\bac\b", "air condition", "air-condition", "aircondition", "cooling"],
    "WiFi":             [r"\bwifi\b", r"\bwi-fi\b", "internet", "broadband", "high speed net"],
    "meals":            [r"\bmeals\b", r"\bfood\b", r"\btiffin\b", r"\bmess\b", "home cooked", "khana", "breakfast included"],
    "gym":              [r"\bgym\b", "fitness", "workout", "exercise room"],
    "laundry":          ["laundry", "washing machine", r"\bwasher\b", "wash clothes"],
    "parking":          ["parking", "bike stand", "car park", "two-wheeler stand"],
    "attached bathroom":["attached bath", "attached bathroom", "private bath", r"\battach\b", "apna bathroom"],
    "housekeeping":     ["housekeeping", "cleaning", "maid", "sweep", "jhadu"],
    "power backup":     ["power backup", "generator", "inverter", "no power cut", "24hr power"],
    "geyser":           ["geyser", "hot water", "warm water", "garam paani"],
    "CCTV":             [r"\bcctv\b", "camera", "surveillance", "security camera"],
    "study room":       ["study room", "library", r"\bdesk\b", "reading room", "quiet room"],
    "lift":             [r"\blift\b", "elevator", "lifts"],
    "terrace":          ["terrace", "rooftop", "roof top", r"\bterrace\b"],
    "TV":               [r"\btv\b", "television", "cable"],
    "furnished":        ["furnished", "furniture", "bed included", "almirah", "cupboard"],
    "security guard":   ["security guard", "guard", "watchman", "chowkidar", "gated"],
}

# Tone patterns — detected in the SAME sentence as the amenity mention
# Strong-want: "must have", "need X", "X chahiye"
_MUST_PATTERNS = [
    r"\bmust\b", r"\bneed\b", r"\brequire\b", r"\bwant\b.*\bnecessary\b",
    "chahiye", "zaruri", "mandatory", "compulsory", "without this",
    "can't live without", "cannot do without", "nahi toh nahi",
    "chahiye hi", "must have", "non-negotiable",
]
# Nice-to-have: "prefer", "if possible", "ideally", "bonus"
_NICE_PATTERNS = [
    r"\bprefer\b", "if possible", "ideally", "would be nice", "bonus",
    "agar ho toh", "agar milta hai", "toh acha", "if available",
    "not mandatory but", "not necessary but", "good to have",
    "nice to have", "plus point", "added bonus",
]
# Deal-breaker: strong rejection / "never"
_DEALBREAKER_PATTERNS = [
    r"\bno\b.{0,20}(ac|wifi|parking|meals|gym|bath)",  # "no AC", "no WiFi"
    "no way", "never", "absolutely not", "bilkul nahi", "nahi chahiye",
    "deal breaker", "dealbreaker", "rule out", "hate", r"\bcan't\b.{0,10}without",
    "not okay", "not acceptable", "won't take", "won't go",
    "nahi lunga", "nahi lena", "reject", "ugh", "disgusting",
]


def _extract_amenity_tone(text: str) -> tuple[list[str], list[str], list[str]]:
    """Return (must_haves, nice_to_haves, deal_breakers) from a single message.

    Strategy: for each amenity found in the text, check if a tone marker
    (must / nice / dealbreaker) appears within the same sentence.
    Falls back to neutral (no bucket) when tone is ambiguous.
    """
    must_haves: list[str] = []
    nice_to_haves: list[str] = []
    deal_breakers: list[str] = []

    # Split into sentences for localised tone detection
    sentences = re.split(r"[.!?\n,;]+", text)

    for sentence in sentences:
        s = sentence.strip()
        if not s:
            continue

        # Which amenities appear in this sentence?
        found_amenities = [
            label for label, patterns in _AMENITY_PATTERNS.items()
            if _any_match(s, patterns)
        ]
        if not found_amenities:
            continue

        # Check tone
        is_must = _any_match(s, _MUST_PATTERNS)
        is_nice = _any_match(s, _NICE_PATTERNS)
        is_db = _any_match(s, _DEALBREAKER_PATTERNS)

        # "no AC" pattern — explicit negation of amenity → deal-breaker
        for label, patterns in _AMENITY_PATTERNS.items():
            for pat in patterns:
                if re.search(r"\bno\b.{0,15}" + pat.strip(r"\b"), s):
                    if label not in deal_breakers:
                        deal_breakers.append(f"no {label}")

        for amenity in found_amenities:
            if is_db:
                if amenity not in deal_breakers:
                    deal_breakers.append(f"no {amenity}")
            elif is_must and not is_nice:
                if amenity not in must_haves:
                    must_haves.append(amenity)
            elif is_nice:
                if amenity not in nice_to_haves:
                    nice_to_haves.append(amenity)
            # else: neutral mention — counted in topic_frequency only

    return must_haves, nice_to_haves, deal_breakers


def extract_amenity_preferences(messages: list[dict]) -> dict:
    """Scan all user messages and return inferred preference buckets.

    Returns:
        {
          "must_haves":    ["AC", "WiFi"],
          "nice_to_haves": ["gym"],
          "deal_breakers": ["no meals"],
          "freq":          {"AC": 3, "WiFi": 2}   # mention count per amenity
        }

    Amenity mentioned ≥ 3 turns with no negative tone → promoted to must_have.
    """
    agg_must: set[str] = set()
    agg_nice: set[str] = set()
    agg_db: set[str] = set()
    freq: dict[str, int] = {}

    user_turns = _user_text(messages)
    per_turn_mentions: list[set[str]] = []

    for text in user_turns:
        turn_amenities: set[str] = set()
        for label, patterns in _AMENITY_PATTERNS.items():
            if _any_match(text, patterns):
                turn_amenities.add(label)
                freq[label] = freq.get(label, 0) + 1
        per_turn_mentions.append(turn_amenities)

        must, nice, db = _extract_amenity_tone(text)
        agg_must.update(must)
        agg_nice.update(nice)
        agg_db.update(db)

    # Frequency escalation: amenity in ≥3 turns with no dealbreaker → must_have
    for label, count in freq.items():
        if count >= 3 and f"no {label}" not in agg_db:
            agg_must.add(label)

    # Nice-to-have should not overlap must-have or dealbreaker
    agg_nice -= agg_must
    agg_nice = {n for n in agg_nice if n not in agg_db and f"no {n}" not in agg_db}

    return {
        "must_haves": sorted(agg_must),
        "nice_to_haves": sorted(agg_nice),
        "deal_breakers": sorted(agg_db),
        "freq": freq,
    }


def infer_needs_from_lifestyle(lifestyle_tags: list[str]) -> list[str]:
    """Map lifestyle_tags → implied amenity/feature needs (no questionnaire)."""
    needs: set[str] = set()
    for tag in lifestyle_tags:
        for need in LIFESTYLE_TO_NEEDS.get(tag, []):
            needs.add(need)
    return sorted(needs)


def extract_roommate_preferences(messages: list[dict]) -> dict[str, bool]:
    """Return dict of roommate compatibility flags seen across user messages.

    Each key is True only when a positive signal fires. Missing key = not detected.
    Callers should treat these as sticky-True flags: once set, they do not clear.
    """
    user_turns = _user_text(messages)
    result: dict[str, bool] = {}
    for text in user_turns:
        for pref, patterns in _ROOMMATE_PATTERNS.items():
            if _any_match(text, patterns):
                result[pref] = True
    return result

# ---------------------------------------------------------------------------
# Pure extraction functions
# ---------------------------------------------------------------------------

def _user_text(messages: list[dict]) -> list[str]:
    """Extract lowercase text from user turns only."""
    return [
        m.get("content", "").lower()
        for m in messages
        if m.get("role") == "user" and m.get("content")
    ]


def _any_match(text: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if re.search(pat, text):
            return True
    return False


def extract_urgency(messages: list[dict]) -> str:
    """Return highest urgency seen across user messages.

    Returns 'immediate', 'near', 'flexible', or '' (not detected).
    'immediate' dominates: once seen, it stays.
    """
    user_turns = _user_text(messages)
    for text in user_turns:
        if _any_match(text, _URGENCY_IMMEDIATE):
            return "immediate"
    for text in user_turns:
        if _any_match(text, _URGENCY_NEAR):
            return "near"
    for text in user_turns:
        if _any_match(text, _URGENCY_FLEXIBLE):
            return "flexible"
    return ""


def extract_decision_authority(messages: list[dict]) -> str:
    """Return 'family' if any family-involvement signal found, else 'self' (or '' if too early)."""
    user_turns = _user_text(messages)
    if not user_turns:
        return ""
    for text in user_turns:
        if _any_match(text, _FAMILY_SIGNALS):
            return "family"
    # Only commit to 'self' after at least 2 user turns (not on the first message)
    if len(user_turns) >= 2:
        return "self"
    return ""


def extract_topic_frequency(messages: list[dict]) -> dict[str, int]:
    """Count how many user turns mention each topic.

    Returns a dict like {"commute": 2, "price": 1, "deposit": 3}.
    Counts per-TURN (not per-occurrence) to avoid over-weighting long messages.
    """
    user_turns = _user_text(messages)
    counts: dict[str, int] = {}
    for text in user_turns:
        for topic, patterns in _TOPIC_PATTERNS.items():
            if _any_match(text, patterns):
                counts[topic] = counts.get(topic, 0) + 1
    return counts


def extract_lifestyle_tags(messages: list[dict]) -> list[str]:
    """Return list of inferred lifestyle tags across all user messages."""
    user_turns = _user_text(messages)
    tags: set[str] = set()
    for text in user_turns:
        for tag, patterns in _LIFESTYLE_RULES.items():
            if _any_match(text, patterns):
                tags.add(tag)
    return sorted(tags)


def merge_topic_frequency(existing: dict, new: dict) -> dict:
    """Merge new topic counts into existing, keeping cumulative totals."""
    merged = dict(existing)
    for topic, count in new.items():
        merged[topic] = merged.get(topic, 0) + count
    return merged


def merge_lifestyle_tags(existing: list, new: list) -> list:
    """Accumulate lifestyle tags without duplicates."""
    combined = set(existing) | set(new)
    return sorted(combined)


# ---------------------------------------------------------------------------
# Dominant topic → intent profile mapping (used by classify_intent override)
# ---------------------------------------------------------------------------

TOPIC_TO_INTENT: dict[str, str] = {
    "price": "budget",
    "commute": "commute",
    "amenity": "amenity",
    "quality": "quality-led",
    "safety": "amenity",
    "social": "amenity",
    "deposit": "budget",
}


def dominant_intent_from_topics(topic_frequency: dict, min_count: int = 2) -> Optional[str]:
    """Derive an intent profile override from cumulative topic frequency.

    Only fires when a single topic has appeared in ≥ min_count turns AND
    no other topic matches it in count (clear dominance). Returns None if
    ambiguous or not enough signal.
    """
    if not topic_frequency:
        return None
    sorted_topics = sorted(topic_frequency.items(), key=lambda x: x[1], reverse=True)
    top_topic, top_count = sorted_topics[0]
    if top_count < min_count:
        return None
    # Only override if dominant topic is at least 2x the second (clear leader)
    if len(sorted_topics) > 1:
        second_count = sorted_topics[1][1]
        if top_count < second_count * 2:
            return None
    return TOPIC_TO_INTENT.get(top_topic)


# ---------------------------------------------------------------------------
# Async I/O wrapper — called from pipeline.py as a background task
# ---------------------------------------------------------------------------

async def extract_and_update(user_id: str, messages: list[dict]) -> None:
    """Run all six extractors and merge results into user memory.

    Designed to run as asyncio.create_task — never raises.
    Uses only the last 10 messages for per-turn signals (urgency/authority/lifestyle/roommate)
    but the full conversation for topic_frequency and amenity preferences (cumulative pattern).
    """
    try:
        from db.redis.user import get_user_memory, update_user_memory, add_deal_breaker

        recent = messages[-10:] if len(messages) > 10 else messages

        # --- per-turn signals (recent window) ---
        new_urgency = extract_urgency(recent)
        new_authority = extract_decision_authority(recent)
        new_lifestyle = extract_lifestyle_tags(recent)
        new_roommate = extract_roommate_preferences(recent)

        # --- cumulative signals (full history, replace not accumulate) ---
        new_topics = extract_topic_frequency(messages)
        amenity_prefs = extract_amenity_preferences(messages)

        mem = get_user_memory(user_id)

        # Urgency: only upgrade (immediate > near > flexible > ""), never downgrade
        _URGENCY_RANK = {"immediate": 3, "near": 2, "flexible": 1, "": 0}
        current_urgency = mem.get("urgency", "")
        if _URGENCY_RANK.get(new_urgency, 0) > _URGENCY_RANK.get(current_urgency, 0):
            mem["urgency"] = new_urgency

        # Decision authority: family is sticky once detected
        current_authority = mem.get("decision_authority", "")
        if new_authority == "family" or (new_authority == "self" and not current_authority):
            mem["decision_authority"] = new_authority

        # Lifestyle tags: cumulative union
        current_lifestyle = mem.get("lifestyle_tags", [])
        merged_lifestyle = merge_lifestyle_tags(current_lifestyle, new_lifestyle)
        mem["lifestyle_tags"] = merged_lifestyle

        # Inferred needs: re-derived from merged lifestyle tags
        mem["inferred_needs"] = infer_needs_from_lifestyle(merged_lifestyle)

        # Topic frequency: replace with full-history scan (reflects actual state)
        mem["topic_frequency"] = new_topics

        # Roommate preferences: accumulate — True flags are sticky
        current_roommate = mem.get("roommate_prefs", {})
        for key, val in new_roommate.items():
            if val:  # only set True flags, never overwrite with False
                current_roommate[key] = True
        mem["roommate_prefs"] = current_roommate

        # Amenity preferences: replace with full-history scan
        mem["inferred_must_haves"] = amenity_prefs["must_haves"]
        mem["nice_to_haves"] = amenity_prefs["nice_to_haves"]
        mem["amenity_freq"] = amenity_prefs["freq"]

        # Deal-breakers from amenity extraction: merge into the existing
        # deal_breakers list via add_deal_breaker (deduplicates, caps at 10)
        for db in amenity_prefs["deal_breakers"]:
            add_deal_breaker(user_id, db)

        update_user_memory(user_id, **{
            "urgency":           mem["urgency"],
            "decision_authority": mem["decision_authority"],
            "lifestyle_tags":    mem["lifestyle_tags"],
            "inferred_needs":    mem["inferred_needs"],
            "topic_frequency":   mem["topic_frequency"],
            "roommate_prefs":    mem["roommate_prefs"],
            "inferred_must_haves": mem["inferred_must_haves"],
            "nice_to_haves":     mem["nice_to_haves"],
            "amenity_freq":      mem["amenity_freq"],
        })

    except Exception as exc:
        logger.debug("signal_extractor: suppressed error for %s: %s", user_id, exc)
