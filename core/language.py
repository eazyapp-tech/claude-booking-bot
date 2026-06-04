"""
Lightweight language detection for Hindi, Marathi, and Romanized Hinglish.

Uses Unicode script heuristic (Devanagari U+0900–U+097F) — no external API,
runs in <1ms.  Marathi is disambiguated from Hindi via a keyword set.
Romanized Hinglish is detected via a curated keyword set (requires 2+ matches).
"""

import re
from typing import Literal

Language = Literal["en", "hi", "mr", "hinglish"]

# ── Marathi-specific keywords (common words that differ from Hindi) ──────────
_MARATHI_KEYWORDS: set[str] = {
    "आहे", "नाही", "काय", "पाहिजे", "कसे", "कुठे", "मला", "तुम्ही",
    "आम्ही", "त्याला", "तिला", "हवे", "नको", "बघा", "सांगा", "करा",
    "होते", "असते", "दाखवा", "किती", "कोण", "जागा", "भाडे", "खोली",
    "महिना", "रुपये", "माहिती", "शोधा", "बुकिंग",
}

# ── Romanized Hinglish keywords (need 2+ matches to trigger) ─────────────────
_HINGLISH_KEYWORDS: set[str] = {
    "chahiye", "dikhao", "kamra", "kiraya", "kitna", "kahan", "kaise",
    "mujhe", "humko", "batao", "dekhna", "booking", "bhejo", "bhejiye",
    "karo", "kariye", "dijiye", "milega", "dedo", "dekho", "accha",
    "theek", "sahi", "nahi", "haan", "ji", "bhai", "yaar", "kya",
    "wala", "wali", "sala", "bata", "paise", "rupaye", "mahina",
    "jagah", "room", "flat", "pg", "hostel", "rent",
    "dhundho", "khojo", "pasand", "visit", "dekhne", "jaana",
    "paisa", "advance", "deposit", "shifting", "available",
}

# ── Devanagari Unicode range ─────────────────────────────────────────────────
_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")


def _devanagari_ratio(text: str) -> float:
    """Return the fraction of alphabetic characters that are Devanagari."""
    alpha_chars = [ch for ch in text if ch.isalpha()]
    if not alpha_chars:
        return 0.0
    deva_count = sum(1 for ch in alpha_chars if _DEVANAGARI_RE.match(ch))
    return deva_count / len(alpha_chars)


def _has_marathi_markers(text: str) -> bool:
    """Check if text contains Marathi-specific keywords."""
    words = set(text.split())
    matches = words & _MARATHI_KEYWORDS
    return len(matches) >= 1


def _count_hinglish_matches(text: str) -> int:
    """Count how many Romanized Hinglish keywords appear in the text."""
    words = set(text.lower().split())
    # Also check 2-char substrings aren't matching too aggressively
    matches = words & _HINGLISH_KEYWORDS
    return len(matches)


def detect_language(text: str) -> Language:
    """Detect the language of a user message.

    Returns
    -------
    "hi"       – Hindi in Devanagari script (non-Marathi)
    "mr"       – Marathi in Devanagari script (with Marathi markers)
    "hinglish" – Hindi written in Roman/Latin script (romanized)
    "en"       – English or undetected (default)

    "hi" and "hinglish" are deliberately distinct so the response directive can
    mirror the user's SCRIPT (Devanagari in → Devanagari out; romanized in →
    romanized out), instead of replying in Devanagari to a romanized message.

    Detection priority:
    1. If ≥30% Devanagari characters → check Marathi markers → "mr" or "hi"
    2. If ≥2 Romanized Hinglish keywords → "hinglish"
    3. Otherwise → "en"
    """
    if not text or not text.strip():
        return "en"

    cleaned = text.strip()

    # Step 1: Devanagari script detection
    ratio = _devanagari_ratio(cleaned)
    if ratio >= 0.30:
        # Devanagari detected — disambiguate Hindi vs Marathi
        if _has_marathi_markers(cleaned):
            return "mr"
        return "hi"

    # Step 2: Romanized Hinglish detection (requires 2+ keyword matches)
    if _count_hinglish_matches(cleaned) >= 2:
        return "hinglish"

    # Step 3: Default to English
    return "en"
