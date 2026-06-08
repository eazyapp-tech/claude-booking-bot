"""
test_h3_profile_routing.py — H3 regression: "What did I shortlist?" routes to profile.

Root cause: "shortlist" (verb form) was in BROKER_WORDS but not PROFILE_PHRASES, so
"What did I shortlist?" → Phase 2 matched BROKER_WORDS → broker (which lacks the
get_shortlisted_properties tool → "trouble processing").

Fix:
  1. core/prompts.py SUPERVISOR_PROMPT — added "what did I shortlist", "did I shortlist",
     "my shortlist" to profile clue examples; removed "shortlist" from broker key words.
  2. core/router.py — added "what did i shortlist", "did i shortlist", "i shortlisted",
     "my shortlist", "what i shortlisted" to PROFILE_PHRASES (Phase 1, checked before
     Phase 2 BROKER_WORDS).

Deterministic: no Redis, no network, no LLM. apply_keyword_safety_net needs only
get_last_agent; patched to return None (no last-agent fallback noise).

Run: python test_h3_profile_routing.py  (exit 0 = pass)
"""

import os
import sys
from unittest.mock import patch

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

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


# ── H3: shortlist query forms → profile ──────────────────────────────────────
print("\n1. Shortlist query forms (H3 fix)")
check('"What did I shortlist?" → profile',         route("What did I shortlist?") == "profile")
check('"what did i shortlist" (lowercase) → profile', route("what did i shortlist") == "profile")
check('"Did I shortlist anything?" → profile',     route("Did I shortlist anything?") == "profile")
check('"I shortlisted 3 places" → profile',        route("I shortlisted 3 places") == "profile")
check('"What I shortlisted last time" → profile',  route("What I shortlisted last time") == "profile")
check('"Show my shortlist" → profile',             route("Show my shortlist") == "profile")

# ── Existing profile phrases — regression ────────────────────────────────────
print("\n2. Existing profile phrases (regression)")
check('"my visits" → profile',                     route("my visits") == "profile")
check('"my bookings" → profile',                   route("my bookings") == "profile")
check('"shortlisted properties" → profile',        route("shortlisted properties") == "profile")
check('"saved properties" → profile',              route("saved properties") == "profile")
check('"booking status" → profile',                route("booking status") == "profile")
check('"my preference" → profile',                 route("my preference") == "profile")

# ── PROFILE_WORDS — single word regression ────────────────────────────────────
print("\n3. Single-word profile matches (regression)")
check('"shortlisted" alone → profile',             route("shortlisted") == "profile")
check('"events" → profile',                        route("events") == "profile")
check('"upcoming" → profile',                      route("upcoming") == "profile")

# ── Broker action forms — must NOT regress ────────────────────────────────────
print("\n4. Broker action forms (no regression)")
check('"shortlist this property" → broker',        route("shortlist this property") == "broker")
check('"shortlist the Andheri one" → broker',      route("shortlist the Andheri one") == "broker")
check('"find me a PG in Andheri" → broker',        route("find me a PG in Andheri") == "broker")
check('"search for boys PG in Kurla" → broker',    route("search for boys PG in Kurla") == "broker")
check('"show images of this place" → broker',      route("show images of this place") == "broker")

# ── Booking action forms — must NOT regress ───────────────────────────────────
print("\n5. Booking action forms (no regression)")
check('"schedule a visit" → booking',              route("schedule a visit") == "booking")
check('"book a tour" → booking',                   route("book a tour") == "booking")
check('"pay the token" → booking',                 route("pay the token") == "booking")

# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{PASS} passed, {FAIL} failed")
if FAIL:
    sys.exit(1)
