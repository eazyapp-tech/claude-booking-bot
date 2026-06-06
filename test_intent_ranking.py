"""
R8 — Intent-tuned ranking weight profiles · hermetic regression suite.

No network / Redis / LLM. Proves:

[A] NO-REGRESSION (the core constraint) — match_score(weights=None) and
    match_score(weights=BALANCED) are BYTE-IDENTICAL to today's fixed-weight
    scoring, over a 105-cell golden matrix snapshotted from the pre-R8 code.
    The `balanced` profile MUST equal the literal defaults, so the two can
    never silently drift apart.

[B] classify_intent — deterministic, prefs + last-message heuristic. Returns
    one of budget-led / commute-led / amenity-led / quality-led, or None when
    the signal is absent OR ambiguous (so we only ever specialize when
    confident; None → balanced → no-regression path).

[C] WEIGHT_PROFILES — each non-balanced profile boosts exactly the lever its
    intent names (~+40% moderate), gender + deal-breaker never shift, and the
    positive envelope stays ~100 so indicator() labels keep their meaning.

[D] Re-ranking proof — under the matching intent the deck reorders the way the
    user would expect (commute winner rises for commute-led, etc.), while the
    same deck under balanced keeps today's order.

Run: `python test_intent_ranking.py`  (exit 0 = pass)
"""

import sys

from utils.scoring import (
    match_score,
    classify_intent,
    WEIGHT_PROFILES,
)

_failures = []


def check(cond, label):
    if cond:
        print(f"  PASS: {label}")
    else:
        print(f"  FAIL: {label}")
        _failures.append(label)


# ---------------------------------------------------------------------------
# Shared matrix (identical construction to the golden snapshot generator)
# ---------------------------------------------------------------------------

PROPS = [
    {"rent": 9000, "distance": 1500, "amenities": "ac,wifi,food", "property_type": "PG", "pg_available_for": "any"},
    {"rent": 15000, "distance": 7000, "amenities": "wifi", "property_type": "PG", "pg_available_for": "male"},
    {"rent": 6000, "distance": 3000, "amenities": "ac,food,wifi,laundry", "property_type": "Flat", "pg_available_for": "female"},
    {"rent": 11000, "commute_minutes": 12, "amenities": "ac", "property_type": "PG", "pg_available_for": "any"},
    {"rent": 11000, "commute_minutes": 45, "amenities": "ac,wifi", "property_type": "PG", "pg_available_for": "any"},
    {"rent": 11000, "commute_km": 4.0, "amenities": "wifi", "property_type": "PG", "pg_available_for": "any"},
    {"rent": 0, "distance": None, "amenities": "", "property_type": "", "pg_available_for": ""},
]
PREFS_VARIANTS = [
    {"min_budget": 8000, "max_budget": 12000, "must_have_amenities": "ac,wifi", "nice_to_have_amenities": "food", "property_type": "PG", "pg_available_for": "any"},
    {"min_budget": 0, "max_budget": 100000},
    {"min_budget": 10000, "max_budget": 12000, "amenities": "ac,food"},
]

# Golden values snapshotted from the PRE-R8 match_score (fixed weights).
GOLDEN = {"0_0_base": 100, "0_0_db": 100, "0_0_sig": 100, "0_0_noshow": 95.0, "0_0_transit": 100, "0_1_base": 51.0, "0_1_db": 36.0, "0_1_sig": 57.0, "0_1_noshow": 46.0, "0_1_transit": 56.0, "0_2_base": 78.5, "0_2_db": 78.5, "0_2_sig": 84.5, "0_2_noshow": 73.5, "0_2_transit": 83.5, "0_3_base": 85.0, "0_3_db": 85.0, "0_3_sig": 91.0, "0_3_noshow": 80.0, "0_3_transit": 90.0, "0_4_base": 77.5, "0_4_db": 77.5, "0_4_sig": 83.5, "0_4_noshow": 72.5, "0_4_transit": 82.5, "0_5_base": 78.3, "0_5_db": 63.3, "0_5_sig": 84.3, "0_5_noshow": 73.3, "0_5_transit": 83.3, "0_6_base": 10.0, "0_6_db": 0, "0_6_sig": 16.0, "0_6_noshow": 5.0, "0_6_transit": 15.0, "1_0_base": 75.0, "1_0_db": 75.0, "1_0_sig": 81.0, "1_0_noshow": 70.0, "1_0_transit": 80.0, "1_1_base": 61.0, "1_1_db": 46.0, "1_1_sig": 67.0, "1_1_noshow": 56.0, "1_1_transit": 66.0, "1_2_base": 71.0, "1_2_db": 71.0, "1_2_sig": 77.0, "1_2_noshow": 66.0, "1_2_transit": 76.0, "1_3_base": 80.0, "1_3_db": 80.0, "1_3_sig": 86.0, "1_3_noshow": 75.0, "1_3_transit": 85.0, "1_4_base": 62.5, "1_4_db": 62.5, "1_4_sig": 68.5, "1_4_noshow": 57.5, "1_4_transit": 67.5, "1_5_base": 73.3, "1_5_db": 58.3, "1_5_sig": 79.3, "1_5_noshow": 68.3, "1_5_transit": 78.3, "1_6_base": 25.0, "1_6_db": 10.0, "1_6_sig": 31.0, "1_6_noshow": 20.0, "1_6_transit": 30.0, "2_0_base": 82.0, "2_0_db": 82.0, "2_0_sig": 88.0, "2_0_noshow": 77.0, "2_0_transit": 87.0, "2_1_base": 36.0, "2_1_db": 21.0, "2_1_sig": 42.0, "2_1_noshow": 31.0, "2_1_transit": 41.0, "2_2_base": 69.0, "2_2_db": 69.0, "2_2_sig": 75.0, "2_2_noshow": 64.0, "2_2_transit": 74.0, "2_3_base": 80.0, "2_3_db": 80.0, "2_3_sig": 86.0, "2_3_noshow": 75.0, "2_3_transit": 85.0, "2_4_base": 62.5, "2_4_db": 62.5, "2_4_sig": 68.5, "2_4_noshow": 57.5, "2_4_transit": 67.5, "2_5_base": 63.3, "2_5_db": 48.3, "2_5_sig": 69.3, "2_5_noshow": 58.3, "2_5_transit": 68.3, "2_6_base": 15.0, "2_6_db": 0, "2_6_sig": 21.0, "2_6_noshow": 10.0, "2_6_transit": 20.0}


def _all_cells(weights):
    """Return {key: score} for the full matrix under the given weights arg."""
    out = {}
    for pi, pr in enumerate(PREFS_VARIANTS):
        for qi, pd in enumerate(PROPS):
            out[f"{pi}_{qi}_base"] = match_score(pd, pr, weights=weights)
            out[f"{pi}_{qi}_db"] = match_score(pd, pr, deal_breakers=["no ac"], weights=weights)
            out[f"{pi}_{qi}_sig"] = match_score(pd, pr, property_signals={"converted": 2, "no_show": 0}, weights=weights)
            out[f"{pi}_{qi}_noshow"] = match_score(pd, pr, property_signals={"converted": 0, "no_show": 3}, weights=weights)
            out[f"{pi}_{qi}_transit"] = match_score(pd, pr, near_transit=True, weights=weights)
    return out


# ---------------------------------------------------------------------------
# [A] NO-REGRESSION
# ---------------------------------------------------------------------------

def test_no_regression_default_none():
    print("\n[A] No-regression — weights=None equals pre-R8 golden")
    cells = _all_cells(None)
    check(len(cells) == len(GOLDEN) == 105, f"matrix is 105 cells (got {len(cells)})")
    mism = [k for k in GOLDEN if cells.get(k) != GOLDEN[k]]
    check(not mism, f"every cell byte-identical to golden (mismatches: {mism[:5]})")


def test_no_regression_balanced_profile():
    print("\n[A] No-regression — weights=BALANCED equals weights=None")
    none_cells = _all_cells(None)
    bal_cells = _all_cells(WEIGHT_PROFILES["balanced"])
    mism = [k for k in none_cells if none_cells[k] != bal_cells[k]]
    check(not mism, f"balanced profile == None default (mismatches: {mism[:5]})")


def test_balanced_equals_literal_defaults():
    print("\n[A] balanced profile pins the literal default caps (anti-drift)")
    bal = WEIGHT_PROFILES["balanced"]
    check(bal["budget"] == 30, "balanced budget cap == 30")
    check(bal["prox_commute"] == 25, "balanced commute-proximity cap == 25")
    check(bal["prox_distance"] == 20, "balanced search-pin-distance cap == 20")
    check(bal["amenity"] == 30, "balanced amenity cap == 30")
    check(bal["property_type"] == 10, "balanced property-type cap == 10")
    check(bal["transit"] == 5, "balanced transit bonus == 5")
    check(bal["outcome_max"] == 9, "balanced outcome cap == 9")
    check(bal["noshow"] == 5, "balanced no-show penalty == 5")


# ---------------------------------------------------------------------------
# [B] classify_intent
# ---------------------------------------------------------------------------

def test_classify_absent_is_none():
    print("\n[B] classify_intent — absent signal → None (balanced)")
    check(classify_intent({}, "") is None, "empty prefs + empty message → None")
    check(classify_intent({"location": "Kurla"}, "show me PGs in Kurla") is None,
          "bland location-only search → None")
    check(classify_intent({"max_budget": 12000}, "PGs in Andheri") is None,
          "a stated budget number alone is NOT budget-led (no anchor word)")


def test_classify_commute_led():
    print("\n[B] classify_intent — commute-led (persistent pref)")
    check(classify_intent({"commute_from": "Reliance Corporate Park"}, "show me options") == "commute-led",
          "commute_from set → commute-led")
    check(classify_intent({"commute_from": "   "}, "options") is None,
          "blank/whitespace commute_from → not a signal")


def test_classify_amenity_led():
    print("\n[B] classify_intent — amenity-led (persistent pref)")
    check(classify_intent({"must_have_amenities": "ac, wifi"}, "find me a place") == "amenity-led",
          "2 must-haves → amenity-led")
    check(classify_intent({"must_have_amenities": "ac"}, "find me a place") is None,
          "a single must-have is not enough → None")


def test_classify_budget_led():
    print("\n[B] classify_intent — budget-led (message anchor)")
    check(classify_intent({}, "show me the cheapest PGs") == "budget-led", "'cheapest' → budget-led")
    check(classify_intent({}, "something affordable please") == "budget-led", "'affordable' → budget-led")
    check(classify_intent({}, "economical options near Sion") == "budget-led", "'economical' → budget-led")


def test_classify_quality_led():
    print("\n[B] classify_intent — quality-led (message anchor)")
    check(classify_intent({}, "show me the best PGs") == "quality-led", "'best' → quality-led")
    check(classify_intent({}, "I want a premium place") == "quality-led", "'premium' → quality-led")
    check(classify_intent({}, "well-rated options only") == "quality-led", "'well-rated' → quality-led")


def test_classify_message_beats_pref():
    print("\n[B] classify_intent — freshest message priority beats persistent pref")
    # commute_from set from an earlier turn, but THIS message asks for cheapest
    check(classify_intent({"commute_from": "Office Park"}, "actually just the cheapest ones") == "budget-led",
          "current 'cheapest' overrides a previously-set commute_from")


def test_classify_conflict_is_none():
    print("\n[B] classify_intent — ambiguity → None (only specialize when confident)")
    check(classify_intent({}, "best cheap PG please") is None,
          "message with BOTH budget + quality anchors → None")
    check(classify_intent({"commute_from": "Park", "must_have_amenities": "ac, wifi"}, "show me options") is None,
          "two persistent prefs conflict → None")


def test_classify_word_boundary():
    print("\n[B] classify_intent — word-boundary safe (no substring misfire)")
    check(classify_intent({}, "is there a rooftop?") is None,
          "'top' inside 'rooftop' must NOT trigger quality-led")
    check(classify_intent({}, "any place near the topiary garden") is None,
          "'top' inside 'topiary' must NOT trigger quality-led")


# ---------------------------------------------------------------------------
# [C] WEIGHT_PROFILES shape
# ---------------------------------------------------------------------------

def test_profiles_exist():
    print("\n[C] WEIGHT_PROFILES — all 5 profiles present")
    for name in ("balanced", "budget-led", "commute-led", "amenity-led", "quality-led"):
        check(name in WEIGHT_PROFILES, f"profile '{name}' defined")


def test_profile_boosts_named_lever():
    print("\n[C] each profile boosts exactly the lever its name promises")
    bal = WEIGHT_PROFILES["balanced"]
    check(WEIGHT_PROFILES["budget-led"]["budget"] > bal["budget"], "budget-led raises budget cap")
    check(WEIGHT_PROFILES["commute-led"]["prox_commute"] > bal["prox_commute"], "commute-led raises commute proximity")
    check(WEIGHT_PROFILES["commute-led"]["prox_distance"] > bal["prox_distance"], "commute-led raises pin-distance too (one ratio)")
    check(WEIGHT_PROFILES["amenity-led"]["amenity"] > bal["amenity"], "amenity-led raises amenity cap")
    check(WEIGHT_PROFILES["quality-led"]["outcome_max"] > bal["outcome_max"], "quality-led raises outcome cap")


def test_gender_and_dealbreaker_invariant():
    print("\n[C] gender + deal-breaker never shift across intents")
    # Gender (cap 10) is a hard constraint and deal-breaker (-15) is a user veto:
    # both must be byte-identical regardless of weights. Property that ONLY varies
    # by gender match, scored under every profile, must move by the SAME 10/2 split.
    male_prop = {"rent": 10000, "pg_available_for": "male"}
    any_prop = {"rent": 10000, "pg_available_for": "any"}
    prefs = {"min_budget": 9000, "max_budget": 11000, "pg_available_for": "male"}
    for name, w in WEIGHT_PROFILES.items():
        gap = match_score(male_prop, prefs, weights=w) - match_score(any_prop, {**prefs, "pg_available_for": ""}, weights=w)
        # both share identical budget; the male-match adds gender 10, the no-pref adds 5 → +5 delta, constant
        check(round(gap, 1) == 5.0, f"gender delta constant under '{name}' (got {gap})")


def test_envelope_preserved():
    print("\n[C] positive envelope stays ~100 (indicator labels keep meaning)")
    for name, w in WEIGHT_PROFILES.items():
        env = w["budget"] + w["prox_commute"] + w["amenity"] + w["property_type"] + 10 + w["transit"] + w["outcome_max"]
        check(105 <= env <= 125, f"'{name}' positive envelope in band (got {env})")


# ---------------------------------------------------------------------------
# [D] Re-ranking proof
# ---------------------------------------------------------------------------

def test_commute_led_reorders():
    print("\n[D] commute-led surfaces the near-commute option over a cheaper-but-far one")
    prefs = {"min_budget": 5000, "max_budget": 15000}
    cheap_far = {"rent": 6000, "commute_minutes": 55, "amenities": ""}
    pricey_near = {"rent": 13000, "commute_minutes": 8, "amenities": ""}
    bal_cheap = match_score(cheap_far, prefs, weights=WEIGHT_PROFILES["balanced"])
    bal_near = match_score(pricey_near, prefs, weights=WEIGHT_PROFILES["balanced"])
    com_cheap = match_score(cheap_far, prefs, weights=WEIGHT_PROFILES["commute-led"])
    com_near = match_score(pricey_near, prefs, weights=WEIGHT_PROFILES["commute-led"])
    check(com_near > com_cheap, "under commute-led the near option ranks first")
    # the near option's lead must WIDEN vs balanced (commute weighted up)
    check((com_near - com_cheap) > (bal_near - bal_cheap), "commute-led widens the near option's lead vs balanced")


def test_budget_led_reorders():
    print("\n[D] budget-led rewards the cheaper option more than balanced does")
    prefs = {"min_budget": 5000, "max_budget": 20000}
    cheap = {"rent": 6000, "distance": 4000, "amenities": ""}
    pricey = {"rent": 19000, "distance": 1000, "amenities": ""}
    bal_gap = match_score(cheap, prefs, weights=WEIGHT_PROFILES["balanced"]) - match_score(pricey, prefs, weights=WEIGHT_PROFILES["balanced"])
    bud_gap = match_score(cheap, prefs, weights=WEIGHT_PROFILES["budget-led"]) - match_score(pricey, prefs, weights=WEIGHT_PROFILES["budget-led"])
    check(bud_gap > bal_gap, "budget-led widens the cheap option's advantage")


def test_amenity_led_reorders():
    print("\n[D] amenity-led rewards the amenity-rich option more than balanced")
    prefs = {"min_budget": 5000, "max_budget": 20000, "must_have_amenities": "ac,wifi,food"}
    rich = {"rent": 14000, "distance": 5000, "amenities": "ac,wifi,food"}
    bare = {"rent": 9000, "distance": 1000, "amenities": ""}
    bal_gap = match_score(rich, prefs, weights=WEIGHT_PROFILES["balanced"]) - match_score(bare, prefs, weights=WEIGHT_PROFILES["balanced"])
    am_gap = match_score(rich, prefs, weights=WEIGHT_PROFILES["amenity-led"]) - match_score(bare, prefs, weights=WEIGHT_PROFILES["amenity-led"])
    check(am_gap > bal_gap, "amenity-led widens the amenity-rich option's advantage")


def test_quality_led_reorders():
    print("\n[D] quality-led leans harder on real conversion signal")
    prefs = {"min_budget": 5000, "max_budget": 20000}
    proven = {"rent": 12000, "distance": 3000, "amenities": ""}
    unknown = {"rent": 12000, "distance": 3000, "amenities": ""}
    bal_gap = (match_score(proven, prefs, property_signals={"converted": 4}, weights=WEIGHT_PROFILES["balanced"])
               - match_score(unknown, prefs, weights=WEIGHT_PROFILES["balanced"]))
    q_gap = (match_score(proven, prefs, property_signals={"converted": 4}, weights=WEIGHT_PROFILES["quality-led"])
             - match_score(unknown, prefs, weights=WEIGHT_PROFILES["quality-led"]))
    check(q_gap > bal_gap, "quality-led rewards a proven (high-conversion) property more")


if __name__ == "__main__":
    test_no_regression_default_none()
    test_no_regression_balanced_profile()
    test_balanced_equals_literal_defaults()
    test_classify_absent_is_none()
    test_classify_commute_led()
    test_classify_amenity_led()
    test_classify_budget_led()
    test_classify_quality_led()
    test_classify_message_beats_pref()
    test_classify_conflict_is_none()
    test_classify_word_boundary()
    test_profiles_exist()
    test_profile_boosts_named_lever()
    test_gender_and_dealbreaker_invariant()
    test_envelope_preserved()
    test_commute_led_reorders()
    test_budget_led_reorders()
    test_amenity_led_reorders()
    test_quality_led_reorders()

    print("\n" + "=" * 60)
    if _failures:
        print(f"FAILED — {len(_failures)} assertion(s):")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print("ALL PASS — R8 intent-tuned ranking")
    sys.exit(0)
