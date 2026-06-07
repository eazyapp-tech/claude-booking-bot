"""
test_signal_extractor.py — Hermetic tests for core/signal_extractor.py

No Redis, no network, no LLM. Pure extraction functions only.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.signal_extractor import (
    extract_urgency,
    extract_decision_authority,
    extract_topic_frequency,
    extract_lifestyle_tags,
    merge_topic_frequency,
    merge_lifestyle_tags,
    dominant_intent_from_topics,
    TOPIC_TO_INTENT,
    extract_roommate_preferences,
    extract_amenity_preferences,
    infer_needs_from_lifestyle,
    _extract_amenity_tone,
    LIFESTYLE_TO_NEEDS,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def msgs(user_texts):
    """Build a minimal message list from user strings."""
    return [{"role": "user", "content": t} for t in user_texts]


def conv(pairs):
    """Build a user/assistant conversation from (user, assistant) pairs."""
    result = []
    for u, a in pairs:
        result.append({"role": "user", "content": u})
        result.append({"role": "assistant", "content": a})
    return result


_PASS = 0
_FAIL = 0


def check(label, actual, expected):
    global _PASS, _FAIL
    if actual == expected:
        print(f"  PASS  {label}")
        _PASS += 1
    else:
        print(f"  FAIL  {label}: got {actual!r}, expected {expected!r}")
        _FAIL += 1


# ---------------------------------------------------------------------------
# extract_urgency
# ---------------------------------------------------------------------------

print("\n=== extract_urgency ===")

check("immediate — 'asap'",
      extract_urgency(msgs(["need a PG asap"])), "immediate")
check("immediate — Hindi 'abhi'",
      extract_urgency(msgs(["mujhe abhi chahiye"])), "immediate")
check("immediate — 'shifting soon'",
      extract_urgency(msgs(["I am shifting soon this week"])), "immediate")
check("near — 'next week'",
      extract_urgency(msgs(["I'll need it next week"])), "near")
check("near — 'is mahine'",
      extract_urgency(msgs(["is mahine shift karna hai"])), "near")
check("flexible — 'just looking'",
      extract_urgency(msgs(["just looking for now"])), "flexible")
check("flexible — 'dekhte hain'",
      extract_urgency(msgs(["dekhte hain, koi rush nahi"])), "flexible")
check("no signal — empty",
      extract_urgency(msgs(["I need a PG in Kurla"])), "")
check("no signal — empty messages",
      extract_urgency([]), "")
check("immediate beats near — both in same conversation",
      extract_urgency(msgs(["next week", "actually need it asap"])), "immediate")
check("assistant messages ignored",
      extract_urgency([
          {"role": "assistant", "content": "I need it urgently today asap"},
          {"role": "user", "content": "what options do you have"},
      ]), "")

# ---------------------------------------------------------------------------
# extract_decision_authority
# ---------------------------------------------------------------------------

print("\n=== extract_decision_authority ===")

check("family — 'mummy'",
      extract_decision_authority(msgs(["mummy ko dikhana hai pehle"])), "family")
check("family — 'parents'",
      extract_decision_authority(msgs(["need parents approval first"])), "family")
check("family — 'will ask'",
      extract_decision_authority(msgs(["I need a PG", "will ask at home"])), "family")
check("self — after 2 turns, no family signal",
      extract_decision_authority(msgs(["need a PG in Kurla", "budget is 12k"])), "self")
check("empty — only 1 turn, no signal",
      extract_decision_authority(msgs(["I need a PG"])), "")
check("family sticks — family mentioned, then self-referential turn",
      extract_decision_authority(msgs(["I decide myself", "let me check with mummy"])), "family")
check("assistant turns ignored",
      extract_decision_authority([
          {"role": "assistant", "content": "I'll ask my parents"},
          {"role": "user", "content": "I need a PG"},
          {"role": "user", "content": "my budget is 10k"},
      ]), "self")

# ---------------------------------------------------------------------------
# extract_topic_frequency
# ---------------------------------------------------------------------------

print("\n=== extract_topic_frequency ===")

result = extract_topic_frequency(msgs(["how far is it from the metro station", "how far from office"]))
check("commute — counted across 2 turns", result.get("commute", 0), 2)

result = extract_topic_frequency(msgs(["₹ 10000 rent", "too expensive", "budget kam hai"]))
check("price — 3 turns", result.get("price", 0), 3)

result = extract_topic_frequency(msgs(["how far", "how far again", "how far metro"]))
check("same topic in all 3 turns = 3", result.get("commute", 0), 3)

result = extract_topic_frequency(msgs(["is it safe for girls", "security guard hai"]))
check("safety — 2 turns", result.get("safety", 0), 2)

result = extract_topic_frequency(msgs(["kitna deposit hai", "deposit wapas milega"]))
check("deposit — 2 turns", result.get("deposit", 0), 2)

result = extract_topic_frequency(msgs(["I need a PG"]))
check("no topic signal — empty dict", result, {})

result = extract_topic_frequency([{"role": "assistant", "content": "distance is 2km from office"}])
check("assistant turn not counted", result.get("commute", 0), 0)

# One turn with two topics — each gets count 1, not 2
result = extract_topic_frequency(msgs(["needs to be cheap and AC is must"]))
check("two topics in one turn — each counted once per turn",
      result.get("price", 0) + result.get("amenity", 0), 2)

# ---------------------------------------------------------------------------
# extract_lifestyle_tags
# ---------------------------------------------------------------------------

print("\n=== extract_lifestyle_tags ===")

check("night_schedule — 'night shift'",
      extract_lifestyle_tags(msgs(["I work night shift"])), ["night_schedule"])
check("has_vehicle — 'bike'",
      extract_lifestyle_tags(msgs(["I have a bike, need parking"])), ["has_vehicle"])
check("values_privacy — 'attached bathroom'",
      extract_lifestyle_tags(msgs(["need attached bathroom"])), ["values_privacy"])
check("student — 'college'",
      extract_lifestyle_tags(msgs(["near my college"])), ["student"])
check("multiple tags — sorted",
      sorted(extract_lifestyle_tags(msgs(["night shift and need parking for bike"]))),
      sorted(["night_schedule", "has_vehicle"]))
check("no tags",
      extract_lifestyle_tags(msgs(["I need a PG in Kurla"])), [])
check("assistant turns ignored",
      extract_lifestyle_tags([
          {"role": "assistant", "content": "I need bike parking and night shift"},
          {"role": "user", "content": "just need a normal PG"},
      ]), [])

# ---------------------------------------------------------------------------
# merge_topic_frequency
# ---------------------------------------------------------------------------

print("\n=== merge_topic_frequency ===")

check("merge adds counts",
      merge_topic_frequency({"commute": 1, "price": 2}, {"commute": 1, "safety": 1}),
      {"commute": 2, "price": 2, "safety": 1})
check("merge into empty existing",
      merge_topic_frequency({}, {"commute": 2}), {"commute": 2})
check("merge empty new",
      merge_topic_frequency({"price": 3}, {}), {"price": 3})

# ---------------------------------------------------------------------------
# merge_lifestyle_tags
# ---------------------------------------------------------------------------

print("\n=== merge_lifestyle_tags ===")

check("merge deduplicates",
      merge_lifestyle_tags(["has_vehicle", "student"], ["student", "night_schedule"]),
      sorted(["has_vehicle", "student", "night_schedule"]))
check("merge empty existing",
      merge_lifestyle_tags([], ["has_vehicle"]), ["has_vehicle"])
check("merge empty new",
      merge_lifestyle_tags(["student"], []), ["student"])

# ---------------------------------------------------------------------------
# dominant_intent_from_topics
# ---------------------------------------------------------------------------

print("\n=== dominant_intent_from_topics ===")

check("price dominant → budget",
      dominant_intent_from_topics({"price": 4, "commute": 1}), "budget")
check("commute dominant → commute",
      dominant_intent_from_topics({"commute": 3, "safety": 1}), "commute")
check("safety dominant → amenity",
      dominant_intent_from_topics({"safety": 4, "price": 1}), "amenity")
check("deposit dominant → budget",
      dominant_intent_from_topics({"deposit": 3, "commute": 1}), "budget")
check("ambiguous — commute and price tied",
      dominant_intent_from_topics({"commute": 2, "price": 2}), None)
check("too close — leader not 2x runner-up",
      dominant_intent_from_topics({"price": 3, "commute": 2}), None)
check("below min_count — single mention",
      dominant_intent_from_topics({"commute": 1}), None)
check("empty dict",
      dominant_intent_from_topics({}), None)
check("custom min_count=1",
      dominant_intent_from_topics({"commute": 1}, min_count=1), "commute")
check("social → amenity",
      dominant_intent_from_topics({"social": 3, "commute": 1}), "amenity")
check("quality → quality-led",
      dominant_intent_from_topics({"quality": 4, "price": 1}), "quality-led")

# ---------------------------------------------------------------------------
# extract_roommate_preferences
# ---------------------------------------------------------------------------

print("\n=== extract_roommate_preferences ===")

check("veg_only — 'pure veg'",
      extract_roommate_preferences(msgs(["pure veg only please"])).get("veg_only"), True)
check("no_smoking — 'no smoking'",
      extract_roommate_preferences(msgs(["no smoking please"])).get("no_smoking"), True)
check("professional_only — 'working professionals'",
      extract_roommate_preferences(msgs(["only working professionals"])).get("professional_only"), True)
check("no_curfew — '24 hour'",
      extract_roommate_preferences(msgs(["need 24 hour access, no curfew"])).get("no_curfew"), True)
check("regional_preference — 'gujarati'",
      extract_roommate_preferences(msgs(["prefer gujarati community"])).get("regional_preference"), True)
check("student_friendly — 'students okay'",
      extract_roommate_preferences(msgs(["students okay in the room"])).get("student_friendly"), True)
check("non_veg_ok — 'chicken'",
      extract_roommate_preferences(msgs(["I eat chicken, non veg is fine with me"])).get("non_veg_ok"), True)
check("multiple flags in one message",
      extract_roommate_preferences(msgs(["pure veg and no smoking required"])),
      {"veg_only": True, "no_smoking": True})
check("assistant turns ignored",
      extract_roommate_preferences([
          {"role": "assistant", "content": "pure veg and no smoking available"},
          {"role": "user", "content": "just need a normal PG"},
      ]), {})
check("veg and non-veg both present — both flags set",
      set(extract_roommate_preferences(msgs(["veg only", "actually non veg is fine"])).keys()),
      {"veg_only", "non_veg_ok"})

# ---------------------------------------------------------------------------
# _extract_amenity_tone
# ---------------------------------------------------------------------------

print("\n=== _extract_amenity_tone ===")

# Note: _extract_amenity_tone expects pre-lowercased text (matching production path
# via _user_text which calls .lower() before passing to this function).
must, nice, db = _extract_amenity_tone("need ac, must have wifi")
check("must — 'need ac' → AC in must_haves", "AC" in must, True)
check("must — 'must have wifi' → WiFi in must_haves", "WiFi" in must, True)

must, nice, db = _extract_amenity_tone("prefer gym if possible")
check("nice — 'prefer gym if possible' → gym in nice_to_haves", "gym" in nice, True)
check("nice — gym not in must_haves", "gym" in must, False)

must, nice, db = _extract_amenity_tone("no meals in the room please")
check("dealbreaker — 'no meals' fires", len(db) > 0, True)

must, nice, db = _extract_amenity_tone("do you have ac?")
check("neutral — AC mentioned with no tone → not in must or nice",
      len(must) + len(nice), 0)

must, nice, db = _extract_amenity_tone("ac must have, wifi would be nice")
check("mixed — AC in must, WiFi in nice", "AC" in must and "WiFi" in nice, True)
check("mixed — no dealbreakers", len(db), 0)

# ---------------------------------------------------------------------------
# extract_amenity_preferences
# ---------------------------------------------------------------------------

print("\n=== extract_amenity_preferences ===")

# Explicit must-have tone
result = extract_amenity_preferences(msgs(["I need WiFi, it's a must"]))
check("must-have tone → WiFi in must_haves", "WiFi" in result["must_haves"], True)

# Explicit nice-to-have tone
result = extract_amenity_preferences(msgs(["gym would be a good to have if possible"]))
check("nice-to-have tone → gym in nice_to_haves", "gym" in result["nice_to_haves"], True)
check("nice-to-have → gym not in must_haves", "gym" in result["must_haves"], False)

# Frequency escalation: AC mentioned in 3 separate turns without dealbreaker → must_have
result = extract_amenity_preferences(msgs([
    "do you have AC?",
    "is AC available?",
    "what about AC in the room",
]))
check("freq escalation — AC in 3 turns → must_have", "AC" in result["must_haves"], True)
check("freq escalation — AC count = 3", result["freq"].get("AC"), 3)

# Frequency escalation BLOCKED by dealbreaker on same amenity
result = extract_amenity_preferences(msgs([
    "AC enquiry",
    "AC availability?",
    "actually no AC is fine",  # negation fires dealbreaker
]))
check("freq escalation blocked by dealbreaker", "AC" not in result["must_haves"], True)

# Must-have should not appear in nice-to-have
result = extract_amenity_preferences(msgs(["need AC must have, gym if available"]))
check("must-have not in nice-to-have", "AC" not in result["nice_to_haves"], True)

# Empty conversation
result = extract_amenity_preferences(msgs(["I need a PG in Kurla"]))
check("no amenity signal — all buckets empty",
      result["must_haves"] + result["nice_to_haves"] + result["deal_breakers"], [])

# ---------------------------------------------------------------------------
# infer_needs_from_lifestyle
# ---------------------------------------------------------------------------

print("\n=== infer_needs_from_lifestyle ===")

result = infer_needs_from_lifestyle(["night_schedule"])
check("night_schedule → includes 24hr_access", "24hr_access" in result, True)
check("night_schedule → includes no_curfew", "no_curfew" in result, True)
check("night_schedule → includes quiet_building", "quiet_building" in result, True)

result = infer_needs_from_lifestyle(["student"])
check("student → includes wifi", "wifi" in result, True)
check("student → includes study_area", "study_area" in result, True)

result = infer_needs_from_lifestyle(["has_vehicle"])
check("has_vehicle → parking", result, ["parking"])

result = infer_needs_from_lifestyle(["values_privacy"])
check("values_privacy → attached_bathroom", "attached_bathroom" in result, True)
check("values_privacy → max_2_sharing", "max_2_sharing" in result, True)

result = infer_needs_from_lifestyle(["night_schedule", "has_vehicle"])
check("multiple tags → union of needs includes parking", "parking" in result, True)
check("multiple tags → union includes 24hr_access", "24hr_access" in result, True)

result = infer_needs_from_lifestyle([])
check("empty tags → empty needs", result, [])

# LIFESTYLE_TO_NEEDS completeness — every defined tag maps to a non-empty list
print("\n=== LIFESTYLE_TO_NEEDS completeness ===")
for tag, needs in LIFESTYLE_TO_NEEDS.items():
    check(f"{tag} maps to needs", len(needs) > 0, True)

# ---------------------------------------------------------------------------
# TOPIC_TO_INTENT completeness — every defined topic maps somewhere
# ---------------------------------------------------------------------------

print("\n=== TOPIC_TO_INTENT coverage ===")

from core.signal_extractor import _TOPIC_PATTERNS

for topic in _TOPIC_PATTERNS:
    mapped = TOPIC_TO_INTENT.get(topic)
    check(f"{topic} maps to an intent", mapped is not None, True)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print(f"\n{'='*50}")
print(f"RESULT: {_PASS} passed, {_FAIL} failed")
if _FAIL:
    sys.exit(1)
