"""
Support-contact regression (G-20).

The bot must be able to share a property's PUBLIC customer-care line
(`communication_contact` / microsite `customer_support_*`) when the user asks
for a number, asks for a human, or is stuck — but must NEVER expose the owner's
private `personal_contact`. Surfacing is via the `get_support_contact` tool,
which the broker calls ONLY in that scenario (so the number is never volunteered).

Hermetic: no network / Redis / LLM. Run:
    ANTHROPIC_API_KEY=test-key-not-used python test_support_contact.py
Exit 0 = pass.
"""

import os
import sys
import asyncio
from unittest import mock

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print("  ✓", msg)
    else:
        _failures.append(msg)
        print("  ✗", msg)


OWNER = "7977106781"      # personal_contact — private, must NEVER surface
SUPPORT = "7304531989"    # communication_contact / customer-care — public


# ---------------------------------------------------------------------------
# Part A — extract_support_contact (pure fallback chain)
# ---------------------------------------------------------------------------
print("\n[A] extract_support_contact")
import tools.broker.support_contact as sc

check(sc.extract_support_contact({}, {"customer_support_whatsapp": SUPPORT}) == SUPPORT,
      "microsite customer_support_whatsapp wins")
check(sc.extract_support_contact({}, {"customer_support_number": SUPPORT}) == SUPPORT,
      "falls back to microsite customer_support_number")
check(sc.extract_support_contact({"communication_contact": SUPPORT}, {}) == SUPPORT,
      "falls back to property communication_contact")
check(sc.extract_support_contact({"communication_contact": SUPPORT},
                                 {"customer_support_whatsapp": "111"}) == "111",
      "microsite beats property-level communication_contact")
check(sc.extract_support_contact({"personal_contact": OWNER}, {}) == "",
      "owner personal_contact is NEVER used as support contact")
check(sc.extract_support_contact({}, {}) == "", "no contact fields -> empty")
check(sc.extract_support_contact({"communication_contact": "  "}, {}) == "",
      "whitespace-only -> empty")


# ---------------------------------------------------------------------------
# Part B — get_support_contact handler
# ---------------------------------------------------------------------------
print("\n[B] get_support_contact handler")


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _no_fetch(*a, **k):
    raise AssertionError("_fetch_support_contact must NOT be called on a cache hit")


# Cache hit → returns cached support contact, never fetches
prop_cached = {"property_name": "ROHA VATIKA", "prop_id": "uuid-1", "support_contact": SUPPORT, "phone_number": OWNER}
with mock.patch.object(sc, "_find_property", lambda u, n: prop_cached), \
     mock.patch.object(sc, "get_property_info_map", lambda u: [prop_cached]), \
     mock.patch.object(sc, "_fetch_support_contact", _no_fetch):
    r = run(sc.get_support_contact("u", "ROHA VATIKA"))
    check(SUPPORT in r, "cache hit: returns the support number")
    check(OWNER not in r, "cache hit: never leaks the owner number")

# No property in view → graceful, no crash
with mock.patch.object(sc, "_find_property", lambda u, n: None), \
     mock.patch.object(sc, "get_property_info_map", lambda u: []):
    r = run(sc.get_support_contact("u", ""))
    check(isinstance(r, str) and OWNER not in r and SUPPORT not in r, "no property -> graceful generic message")

# Property present, no cache, fetch succeeds → returns + caches
prop_nocache = {"property_name": "Mass Metropolis", "prop_id": "uuid-2", "phone_number": OWNER}
the_map = [prop_nocache]
saved = {}
with mock.patch.object(sc, "_find_property", lambda u, n: prop_nocache), \
     mock.patch.object(sc, "get_property_info_map", lambda u: the_map), \
     mock.patch.object(sc, "set_property_info_map", lambda u, m: saved.update({"map": m})), \
     mock.patch.object(sc, "_fetch_support_contact", mock.AsyncMock(return_value=SUPPORT)):
    r = run(sc.get_support_contact("u", "Mass Metropolis"))
    check(SUPPORT in r and OWNER not in r, "fetch path: returns support number, not owner")
    check(prop_nocache.get("support_contact") == SUPPORT, "fetch path: caches support_contact onto the property")
    check("map" in saved, "fetch path: persists the updated info map")

# Fetch returns nothing → graceful callback offer, never errors, never owner number
prop_nores = {"property_name": "X", "prop_id": "uuid-3", "phone_number": OWNER}
with mock.patch.object(sc, "_find_property", lambda u, n: prop_nores), \
     mock.patch.object(sc, "get_property_info_map", lambda u: [prop_nores]), \
     mock.patch.object(sc, "set_property_info_map", lambda u, m: None), \
     mock.patch.object(sc, "_fetch_support_contact", mock.AsyncMock(return_value="")):
    r = run(sc.get_support_contact("u", "X"))
    check(isinstance(r, str) and OWNER not in r, "no contact found -> graceful, never the owner number")


# ---------------------------------------------------------------------------
# Part C — registry + skill_map wiring
# ---------------------------------------------------------------------------
print("\n[C] registry + skill_map")
from tools.registry import init_registry, get_schemas_by_names, get_handlers_for_agent

init_registry()
schemas = get_schemas_by_names(["get_support_contact"])
check(len(schemas) == 1 and schemas[0]["name"] == "get_support_contact", "get_support_contact schema registered")
for ag in ("broker", "booking", "default"):
    check("get_support_contact" in get_handlers_for_agent(ag), f"get_support_contact in the {ag} tool set")

from skills.skill_map import ALWAYS_TOOLS, get_tools_for_skills
check("get_support_contact" in ALWAYS_TOOLS, "get_support_contact is an always-available broker tool")
check("get_support_contact" in get_tools_for_skills(["search"]), "get_support_contact available on any broker turn")


# ---------------------------------------------------------------------------
# Part D — prompt wiring (broker knows the tool + refined contact rule)
# ---------------------------------------------------------------------------
print("\n[D] prompt wiring")
from skills.loader import load_skill
base_content = load_skill("broker", "_base")["content"]
check("get_support_contact" in base_content, "_base.md tells the broker about get_support_contact")
# The contact rule must distinguish the private owner number from the public support line.
import re
low = base_content.lower()
check("personal" in low or "owner" in low, "_base.md still forbids the owner's private number")

from core.prompts import BROKER_AGENT_PROMPT
check("get_support_contact" in BROKER_AGENT_PROMPT, "legacy broker prompt references get_support_contact")


# ---------------------------------------------------------------------------
print("\n" + ("=" * 50))
if _failures:
    print(f"FAILED ({len(_failures)} check(s)):")
    for f in _failures:
        print("  -", f)
    sys.exit(1)
print("ALL SUPPORT-CONTACT CHECKS PASSED")
sys.exit(0)
