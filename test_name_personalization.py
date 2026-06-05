"""
Name-personalization regression (NAME-1).

Proves the bot both CAPTURES the user's name (a `save_name` tool, the web-channel
analog of WhatsApp's Meta-profile name) and USES it: every conversational agent
appends an (uncached) name directive to its system prompt, so a known name is
addressed naturally — and an unknown name leaves the prompt byte-clean (no
`{name_directive}` / `{user_name}` placeholder leak, cached prefix unchanged).

Hermetic: no network / Redis / LLM. Run:
    ANTHROPIC_API_KEY=test-key-not-used python test_name_personalization.py
Exit 0 = pass.
"""

import os
import sys
import contextlib
from unittest import mock

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print("  ✓", msg)
    else:
        _failures.append(msg)
        print("  ✗", msg)


ACCOUNT = {"brand_name": "OxOtel", "cities": "Mumbai", "areas": "Kurla"}


# ---------------------------------------------------------------------------
# Part A — build_name_directive (pure)
# ---------------------------------------------------------------------------
print("\n[A] build_name_directive")
from core.prompts import build_name_directive

check(build_name_directive(None) == "", "None -> empty directive")
check(build_name_directive("") == "", "empty string -> empty directive")
check(build_name_directive("   ") == "", "whitespace-only -> empty directive")

d = build_name_directive("Rahul")
check("Rahul" in d, "directive contains the name")
check("{" not in d and "}" not in d, "directive has no placeholder braces")
check("every message" in d.lower(), "directive warns against overusing the name")

df = build_name_directive("Rahul Kumar Sharma")
check("Rahul" in df and "Sharma" not in df, "uses first name only (full names trimmed)")


# ---------------------------------------------------------------------------
# Part B — save_name handler (capture)
# ---------------------------------------------------------------------------
print("\n[B] save_name handler")
import tools.broker.save_name as sn

store: dict[str, str] = {}
with mock.patch.object(sn, "set_user_name", lambda uid, name: store.__setitem__(uid, name)):
    r = sn.save_name("u1", "Rahul")
    check(store.get("u1") == "Rahul", "save_name persists the name")
    check(isinstance(r, str) and "Rahul" in r, "save_name returns a confirmation with the name")

    store.clear()
    r2 = sn.save_name("u2", "")
    check("u2" not in store, "empty name is not stored")
    check(isinstance(r2, str), "empty name -> graceful string (no raise)")

    store.clear()
    r3 = sn.save_name("u3", "x" * 61)
    check("u3" not in store, "implausibly long name is not stored")
    check(isinstance(r3, str), "long name -> graceful string (no raise)")

    store.clear()
    r4 = sn.save_name("u4", "  Meera  ")
    check(store.get("u4") == "Meera", "name is trimmed before storing")


# ---------------------------------------------------------------------------
# Part C — registry + skill_map wiring
# ---------------------------------------------------------------------------
print("\n[C] registry + skill_map")
from tools.registry import init_registry, get_schemas_by_names, get_handlers_for_agent

init_registry()
schemas = get_schemas_by_names(["save_name"])
check(len(schemas) == 1 and schemas[0]["name"] == "save_name", "save_name schema registered")
check("save_name" in get_handlers_for_agent("broker"), "save_name handler in the broker tool set")

from skills.skill_map import ALWAYS_TOOLS, get_tools_for_skills

check("save_name" in ALWAYS_TOOLS, "save_name is an always-available broker tool")
check("save_name" in get_tools_for_skills(["qualify_new"]), "save_name available on qualify_new turns")
check("save_name" in get_tools_for_skills(["details"]), "save_name available on any broker turn (e.g. details)")


# ---------------------------------------------------------------------------
# Part D — agent wiring (the anti-regression guard)
# ---------------------------------------------------------------------------
print("\n[D] agent prompt threading")
# Use sentinel names that appear in NO prompt/skill file, so "name present" and
# "name absent" assertions are meaningful (skill examples mention Rahul/Meera).
NAME = "Zarnab"
NAME2 = "Vlorin"
import db.redis_store as rs
import agents.broker_agent as ba
import agents.default_agent as da
import agents.booking_agent as bk
import agents.profile_agent as pa


def _joined(system_prompt) -> str:
    return "\n".join(system_prompt) if isinstance(system_prompt, list) else system_prompt


def _broker_patches(name, dynamic):
    flags = {
        "DYNAMIC_SKILLS_ENABLED": dynamic,
        "PAYMENT_REQUIRED": False,
        "KYC_ENABLED": False,
        "SEMANTIC_KB_ENABLED": False,
    }
    return [
        mock.patch.object(ba, "get_account_values", lambda u: ACCOUNT),
        mock.patch.object(ba, "build_returning_user_context", lambda u: ""),
        mock.patch.object(ba, "get_property_id_for_search", lambda u: []),
        mock.patch.object(ba, "get_user_name", lambda u: name),
        mock.patch.object(rs, "get_user_brand", lambda u: "bh"),
        mock.patch.object(rs, "get_effective_flags", lambda bh: flags),
    ]


def _run(patches, fn):
    with contextlib.ExitStack() as st:
        for p in patches:
            st.enter_context(p)
        return fn()


# Broker — dynamic skill path, name known
cfg = _run(_broker_patches(NAME, True), lambda: ba.get_config("u", language="en"))
j = _joined(cfg["system_prompt"])
check(NAME in j, "broker(dynamic): known name threaded into prompt")
check("{name_directive}" not in j and "{user_name}" not in j, "broker(dynamic): no placeholder leak")

# Broker — dynamic, name UNKNOWN: prompt stays clean, no name, no leak
cfg = _run(_broker_patches(None, True), lambda: ba.get_config("u"))
j = _joined(cfg["system_prompt"])
check(NAME not in j, "broker(dynamic): unknown name -> no name injected")
check("{name_directive}" not in j and "{user_name}" not in j, "broker(dynamic): unknown -> clean prompt")

# Broker — legacy monolithic path, name known
cfg = _run(_broker_patches(NAME2, False), lambda: ba.get_config("u"))
j = _joined(cfg["system_prompt"])
check(NAME2 in j, "broker(legacy): known name threaded into prompt")
check("{name_directive}" not in j, "broker(legacy): no placeholder leak")

# Default agent
with mock.patch.object(da, "get_account_values", lambda u: ACCOUNT), \
     mock.patch.object(da, "get_user_name", lambda u: NAME), \
     mock.patch.object(da, "build_returning_user_context", lambda u: ""):
    cfg = da.get_config("u")
    j = _joined(cfg["system_prompt"])
    check(NAME in j, "default: known name threaded")
    check("{name_directive}" not in j and "{user_name}" not in j, "default: no placeholder leak")

with mock.patch.object(da, "get_account_values", lambda u: ACCOUNT), \
     mock.patch.object(da, "get_user_name", lambda u: None), \
     mock.patch.object(da, "build_returning_user_context", lambda u: ""):
    cfg = da.get_config("u")
    j = _joined(cfg["system_prompt"])
    check(NAME not in j, "default: unknown name -> no name injected")
    check("{name_directive}" not in j and "{user_name}" not in j, "default: unknown -> clean prompt")

# Booking agent (has function-local flag imports from db.redis_store)
with mock.patch.object(bk, "get_account_values", lambda u: ACCOUNT), \
     mock.patch.object(bk, "get_user_name", lambda u: NAME), \
     mock.patch.object(bk, "build_returning_user_context", lambda u: ""), \
     mock.patch.object(rs, "get_user_brand", lambda u: "bh"), \
     mock.patch.object(rs, "get_effective_flags", lambda bh: {"PAYMENT_REQUIRED": False, "KYC_ENABLED": False}):
    cfg = bk.get_config("u")
    j = _joined(cfg["system_prompt"])
    check(NAME in j, "booking: known name threaded")
    check("{name_directive}" not in j, "booking: no placeholder leak")

# Profile agent
with mock.patch.object(pa, "get_account_values", lambda u: ACCOUNT), \
     mock.patch.object(pa, "get_user_name", lambda u: NAME):
    cfg = pa.get_config("u")
    j = _joined(cfg["system_prompt"])
    check(NAME in j, "profile: known name threaded")
    check("{name_directive}" not in j, "profile: no placeholder leak")


# ---------------------------------------------------------------------------
print("\n" + ("=" * 50))
if _failures:
    print(f"FAILED ({len(_failures)} check(s)):")
    for f in _failures:
        print("  -", f)
    sys.exit(1)
print("ALL NAME-PERSONALIZATION CHECKS PASSED")
sys.exit(0)
