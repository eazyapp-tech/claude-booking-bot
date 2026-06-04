"""
test_classify_robustness.py — Supervisor classification JSON robustness (UAT P1).

UAT runtime logs showed frequent `classify attempt 1 failed: Expecting value:
line 1 column 1` and `Expecting property name enclosed in double quotes: line 1
column 2` — Haiku returning JSON the old _clean_json couldn't recover:
  - nested objects (the `\\{[^{}]*\\}` regex stopped at the first inner brace),
  - JSON wrapped in prose / trailing text,
  - single-quoted JSON.
Each failure burned a retry (extra Haiku call + latency) and, when all 3 retries
failed, dropped routing to the `default` agent.

Fix: _clean_json extracts the first BRACE-BALANCED object (handles nesting +
surrounding prose) and tolerates single-quoted JSON as a fallback.

Deterministic: pure string→string, no LLM/network/Redis.
Run: `python test_classify_robustness.py` (exit 0 = pass).
"""

import json
import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

from core.claude import AnthropicEngine  # noqa: E402

clean = AnthropicEngine._clean_json

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


def parses_to(raw, **expect):
    try:
        obj = json.loads(clean(raw))
    except Exception as e:
        return False, f"parse error: {e} | cleaned={clean(raw)!r}"
    for k, v in expect.items():
        if obj.get(k) != v:
            return False, f"{k}={obj.get(k)!r} != {v!r}"
    return True, ""


print("[1] clean JSON the old regex already handled")
ok, d = parses_to('{"agent":"broker","skills":["search"]}', agent="broker"); check("1a flat object", ok, d)
ok, d = parses_to('```json\n{"agent":"booking"}\n```', agent="booking"); check("1b fenced", ok, d)

print("\n[2] cases that BROKE the old regex (the UAT failures)")
ok, d = parses_to('{"agent":"broker","meta":{"src":"kw"}}', agent="broker"); check("2a nested object", ok, d)
ok, d = parses_to('Sure, here you go: {"agent":"profile","skills":[]}', agent="profile"); check("2b prose prefix", ok, d)
ok, d = parses_to('{"agent":"booking"} — routed.', agent="booking"); check("2c trailing prose", ok, d)
ok, d = parses_to("{'agent': 'default', 'skills': []}", agent="default"); check("2d single-quoted", ok, d)
ok, d = parses_to('{"agent":"broker","skills":["search","compare"]}', agent="broker"); check("2e list with commas", ok, d)

print("\n[3] genuinely empty / non-JSON → returns unparseable (classify retries)")
check("3a empty string stays empty-ish (no crash)", clean("") == "" or "{" not in clean(""))
check("3b prose-only doesn't fabricate JSON", "{" not in clean("I think broker"))

print(f"\n{'='*50}\n  {_passed} passed, {_failed} failed\n{'='*50}")
sys.exit(0 if _failed == 0 else 1)
