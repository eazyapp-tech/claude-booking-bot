"""
test_language_mirroring.py — Language SCRIPT mirroring regression (UAT P1).

UAT finding: users typing romanized Hinglish ("Bombay mei pg batao") got
full-DEVANAGARI replies ("Bombay में PG…"), and the bot flip-flopped scripts
mid-conversation. detect_language collapsed BOTH romanized Hinglish and
Devanagari Hindi into "hi", and LANGUAGE_DIRECTIVE then said "respond in
Hindi (हिन्दी)" — naming Devanagari, with no instruction to mirror the user's
SCRIPT. Romanized input → Devanagari output.

Fix: detect romanized Hindi as its own language "hinglish" (distinct from
Devanagari "hi"), and emit a script-explicit directive — romanized in →
romanized reply (never Devanagari); Devanagari in → Devanagari reply.

Deterministic: no Redis/network/LLM. Run: `python test_language_mirroring.py`.
"""

import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

from core.language import detect_language          # noqa: E402
from core.prompts import format_prompt, LANGUAGE_NAMES  # noqa: E402

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


print("[1] detect_language — romanized Hinglish is its own language")
check("1a 'Bombay mei pg batao' → hinglish", detect_language("Bombay mei pg batao") == "hinglish",
      repr(detect_language("Bombay mei pg batao")))
check("1b 'kamra chahiye bhai' → hinglish", detect_language("kamra chahiye bhai") == "hinglish",
      repr(detect_language("kamra chahiye bhai")))
check("1c Devanagari 'मुझे कमरा चाहिए' → hi", detect_language("मुझे कमरा चाहिए") == "hi",
      repr(detect_language("मुझे कमरा चाहिए")))
check("1d Marathi 'मला खोली पाहिजे' → mr", detect_language("मला खोली पाहिजे") == "mr",
      repr(detect_language("मला खोली पाहिजे")))
check("1e English 'Show me PGs in Andheri' → en", detect_language("Show me PGs in Andheri") == "en",
      repr(detect_language("Show me PGs in Andheri")))

print("\n[2] LANGUAGE_NAMES registers hinglish")
check("2a hinglish in LANGUAGE_NAMES", "hinglish" in LANGUAGE_NAMES, list(LANGUAGE_NAMES))

print("\n[3] format_prompt — script-explicit directive")
T = "PRE {language_directive} POST"

d_hing = format_prompt(T, language="hinglish")
check("3a hinglish directive says Roman/Latin", ("roman" in d_hing.lower() or "latin" in d_hing.lower()), repr(d_hing))
check("3b hinglish directive FORBIDS Devanagari", "devanagari" in d_hing.lower(), repr(d_hing))

d_hi = format_prompt(T, language="hi")
check("3c Devanagari directive references Devanagari/Hindi", ("devanagari" in d_hi.lower() or "hindi" in d_hi.lower()), repr(d_hi))

d_en = format_prompt(T, language="en")
check("3d English → no directive injected", "{language_directive}" not in d_en and "LANGUAGE INSTRUCTION" not in d_en, repr(d_en))

print(f"\n{'='*50}\n  {_passed} passed, {_failed} failed\n{'='*50}")
sys.exit(0 if _failed == 0 else 1)
