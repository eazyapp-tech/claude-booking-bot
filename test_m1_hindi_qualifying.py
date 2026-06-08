"""
test_m1_hindi_qualifying.py — M1 regression: Hindi/Hinglish input gets a Hindi/Hinglish
qualifying response.

Root cause: every example in qualify_new.md and qualify_returning.md was English-only.
Haiku mirrors example language → always responded in English, even when the user wrote
in Hinglish or Devanagari Hindi.

Fix: added Hindi/Hinglish examples to both skill files
  - qualify_new.md: 2 new examples (bare location → Hinglish bundled question; location
    + gender + budget → immediate Hinglish search response)
  - qualify_returning.md: 1 new example (Hinglish returning-user greeting)

Tests:
  A. detect_language correctly identifies Hinglish (≥2 keyword matches), Devanagari Hindi,
     and English (no false positives)
  B. Skill files contain the Hindi/Hinglish examples (content assertions — prevents the fix
     from being silently reverted)
  C. Router routes common Hinglish property queries to "broker" (Hindi keywords already in
     BROKER_WORDS; no regression)

Hermetic: no Redis, no network, no LLM.
Run: python test_m1_hindi_qualifying.py  (exit 0 = pass)
"""

import os
import sys
from unittest.mock import patch

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

from core.language import detect_language       # noqa: E402
from core.router import apply_keyword_safety_net  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, condition: bool) -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}")


def route(message: str, last_agent: str = "broker") -> str:
    """Run the safety net as if the supervisor returned 'default'."""
    with patch("core.router.get_last_agent", return_value=last_agent):
        return apply_keyword_safety_net("default", message, user_id="test_user")


# ── A: detect_language — Hinglish (≥2 keyword matches) ───────────────────────
print("\n1. detect_language — Hinglish (needs ≥2 keyword matches)")
check("'Mumbai mein PG chahiye' → hinglish",
      detect_language("Mumbai mein PG chahiye") == "hinglish")
check("'Mujhe dikhao Andheri mein options' → hinglish",
      detect_language("Mujhe dikhao Andheri mein options") == "hinglish")
check("'rooms chahiye kitna rent hai' → hinglish",
      detect_language("rooms chahiye kitna rent hai") == "hinglish")
check("'Bhai wapas aa gaya phir se dekhna hai' → hinglish",
      detect_language("Bhai wapas aa gaya phir se dekhna hai") == "hinglish")

# ── A: detect_language — Devanagari Hindi ────────────────────────────────────
print("\n2. detect_language — Devanagari Hindi (Devanagari ratio ≥30%)")
check("'मुझे PG चाहिए' → hi",
      detect_language("मुझे PG चाहिए") == "hi")
check("'मुंबई में PG ढूंढना है' → hi",
      detect_language("मुंबई में PG ढूंढना है") == "hi")

# ── A: detect_language — English (no false positives) ────────────────────────
print("\n3. detect_language — English (no false positives)")
check("'I need a PG in Mumbai' → en",
      detect_language("I need a PG in Mumbai") == "en")
check("Single Hinglish keyword alone → en (needs 2+)",
      detect_language("okay") == "en")

# ── B: Skill files contain Hindi/Hinglish examples ───────────────────────────
print("\n4. qualify_new.md — Hinglish examples present")
_qn_path = os.path.join(os.path.dirname(__file__), "skills", "broker", "qualify_new.md")
with open(_qn_path, encoding="utf-8") as _f:
    _qn = _f.read().lower()
check("qualify_new.md has Hinglish bundled question form ('ke liye hai')",
      "ke liye hai" in _qn)
check("qualify_new.md has 'chahiye' (Hinglish 'need/want')",
      "chahiye" in _qn)
check("qualify_new.md has Hinglish location phrase ('mein')",
      "mein" in _qn)

print("\n5. qualify_returning.md — Hinglish example present")
_qr_path = os.path.join(os.path.dirname(__file__), "skills", "broker", "qualify_returning.md")
with open(_qr_path, encoding="utf-8") as _f:
    _qr = _f.read().lower()
check("qualify_returning.md has Hinglish welcome-back ('wapas')",
      "wapas" in _qr)
check("qualify_returning.md has Hinglish continuation form ('mein')",
      "mein" in _qr)

# ── C: Router routes Hinglish property queries to broker ─────────────────────
print("\n6. Router: Hinglish property queries → broker (Hindi keywords in BROKER_WORDS)")
check("'Mumbai mein PG chahiye' → broker",
      route("Mumbai mein PG chahiye") == "broker")
check("'Andheri mein kamra dikhao' → broker",
      route("Andheri mein kamra dikhao") == "broker")
check("'hostel chahiye Kurla mein' → broker",
      route("hostel chahiye Kurla mein") == "broker")
check("'ghar chahiye near station' → broker",
      route("ghar chahiye near station") == "broker")

# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{PASS} passed, {FAIL} failed")
if FAIL:
    sys.exit(1)
