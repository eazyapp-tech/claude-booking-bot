"""
Property match scoring: calculate how well a property matches user preferences.

Supports:
- Budget, distance, amenity, property-type, gender scoring
- Fuzzy amenity matching (AC ↔ Air Conditioning, WiFi ↔ Internet, etc.)
- Weighted amenities: must-have (hard penalty) vs nice-to-have (bonus)
- Deal-breaker penalties from cross-session user memory
"""

import re
from typing import Optional


# ---------------------------------------------------------------------------
# R8 — Intent-tuned ranking weight profiles
#
# Each profile is the per-component point cap match_score() uses. `balanced` is
# the literal default (== the pre-R8 fixed weights) and is what an absent or
# ambiguous intent falls back to, so the default path is byte-identical to
# today. A named profile boosts the lever its intent cares about (~+40%) and
# trims the rest, holding the positive envelope near 100 so indicator()'s
# Excellent/Good/Fair thresholds keep their meaning across intents.
#
# match_score scales each component by (profile_cap / balanced_cap), so the
# `balanced` profile multiplies every component by exactly 1.0 — no-regression
# is structural, not just tested. Gender (hard constraint) and the deal-breaker
# penalty (user veto) are deliberately NOT in the profile: they never shift.
# ---------------------------------------------------------------------------

WEIGHT_PROFILES: dict[str, dict[str, float]] = {
    # component →            budget  commute  pin-dist  amenity  type  transit  outcome  no-show
    "balanced":    {"budget": 30, "prox_commute": 25, "prox_distance": 20, "amenity": 30, "property_type": 10, "transit": 5, "outcome_max": 9, "noshow": 5},
    "budget-led":  {"budget": 42, "prox_commute": 20, "prox_distance": 16, "amenity": 22, "property_type": 8, "transit": 3, "outcome_max": 6, "noshow": 5},
    "commute-led": {"budget": 22, "prox_commute": 38, "prox_distance": 30, "amenity": 22, "property_type": 8, "transit": 5, "outcome_max": 6, "noshow": 5},
    "amenity-led": {"budget": 22, "prox_commute": 20, "prox_distance": 16, "amenity": 42, "property_type": 8, "transit": 3, "outcome_max": 6, "noshow": 5},
    "quality-led": {"budget": 20, "prox_commute": 22, "prox_distance": 18, "amenity": 26, "property_type": 10, "transit": 5, "outcome_max": 15, "noshow": 10},
}
_BALANCED = WEIGHT_PROFILES["balanced"]


# Free-text intent anchors in the user's CURRENT message. Word-boundary matched
# (\b) so substrings never misfire ("top" inside "rooftop"/"topiary" is inert).
# Kept deliberately tight — a bare budget number ("my budget is 12k") is NOT a
# budget-LED signal; only an explicit anchor word is.
_BUDGET_RE = re.compile(
    r"\b(cheap|cheapest|affordable|economical|inexpensive|lowest|"
    r"budget[- ]friendly|pocket[- ]friendly|low[- ]budget|least expensive)\b"
)
_QUALITY_RE = re.compile(
    r"\b(best|nicest|premium|luxury|luxurious|high[- ]end|upscale|posh|finest|"
    r"well[- ]rated|highly rated|top[- ]rated|best quality)\b"
)


def classify_intent(
    preferences: dict,
    message: str = "",
    topic_frequency: Optional[dict] = None,
) -> Optional[str]:
    """Pick the ranking intent for this search — deterministic, no LLM.

    Three tiers, freshest-first:
      1. The CURRENT message (transient priority): a budget anchor → budget-led,
         a quality anchor → quality-led. Both anchors at once → ambiguous → None.
      2. Persistent deliberate preferences (the fallback): a saved commute
         destination → commute-led, ≥2 must-have amenities → amenity-led. Both
         set → ambiguous → None.
      3. Behavioral topic frequency (tiebreaker): if topic_frequency is provided
         and one topic clearly dominates (≥2 mentions, 2x the next topic), use
         it. This is the inference-based tier — revealed preference > stated.

    Returns one of budget-led / commute-led / amenity-led / quality-led, or
    None when no signal fires OR signals conflict — so the caller uses the
    balanced (byte-identical) profile and we only ever specialize when confident.
    """
    msg = (message or "").lower()

    # Tier 1 — current message wins (it's the freshest expressed priority).
    msg_intents = []
    if _BUDGET_RE.search(msg):
        msg_intents.append("budget-led")
    if _QUALITY_RE.search(msg):
        msg_intents.append("quality-led")
    if len(msg_intents) == 1:
        return msg_intents[0]
    if len(msg_intents) >= 2:
        return None  # mixed ask (e.g. "best cheap PG") → stay balanced

    # Tier 2 — persistent deliberate preferences.
    pref_intents = []
    if (preferences.get("commute_from") or "").strip():
        pref_intents.append("commute-led")
    if len(_parse_amenities(preferences.get("must_have_amenities", ""))) >= 2:
        pref_intents.append("amenity-led")
    if len(pref_intents) == 1:
        return pref_intents[0]
    if len(pref_intents) >= 2:
        return None

    # Tier 3 — behavioral topic frequency (inference-based, softest signal).
    if topic_frequency:
        from core.signal_extractor import dominant_intent_from_topics
        behavioral = dominant_intent_from_topics(topic_frequency, min_count=2)
        if behavioral:
            return behavioral

    return None


# ---------------------------------------------------------------------------
# Fuzzy amenity aliases — covers ~95% of real-world mismatches
# ---------------------------------------------------------------------------

_AMENITY_ALIASES: dict[str, str] = {
    "ac": "air conditioning",
    "air conditioning": "ac",
    "a/c": "ac",
    "wifi": "internet",
    "internet": "wifi",
    "wi-fi": "wifi",
    "broadband": "wifi",
    "meals": "food",
    "food": "meals",
    "tiffin": "meals",
    "mess": "meals",
    "laundry": "washing machine",
    "washing machine": "laundry",
    "washer": "laundry",
    "housekeeping": "cleaning",
    "cleaning": "housekeeping",
    "parking": "bike parking",
    "two wheeler parking": "bike parking",
    "cctv": "security",
    "security": "cctv",
    "guard": "security",
    "geyser": "hot water",
    "hot water": "geyser",
    "water heater": "geyser",
    "fridge": "refrigerator",
    "refrigerator": "fridge",
    "tv": "television",
    "television": "tv",
}


def _fuzzy_amenity_match(user_amenities: set, prop_amenities: set) -> int:
    """Count how many user amenities the property satisfies (fuzzy)."""
    matched = 0
    for ua in user_amenities:
        if ua in prop_amenities:
            matched += 1
            continue
        # Check aliases
        alias = _AMENITY_ALIASES.get(ua, "")
        if alias and alias in prop_amenities:
            matched += 1
            continue
        # Token overlap: "air conditioning" matches "air conditioned room"
        ua_tokens = set(ua.split())
        for pa in prop_amenities:
            pa_tokens = set(pa.split())
            if ua_tokens and pa_tokens and len(ua_tokens & pa_tokens) >= len(ua_tokens) * 0.5:
                matched += 1
                break
    return matched


def _gender_token(value: str) -> Optional[str]:
    """Normalise a free-text gender string to 'male' | 'female' | 'any' | None.

    Order matters: the female family is checked first so the 'male' ⊂ 'female'
    and 'men' ⊂ 'women' substring traps never misfire. None = unknown/unspecified.
    """
    s = (value or "").strip().lower()
    if not s:
        return None
    if "any" in s or "co-living" in s or "coliving" in s or "unisex" in s:
        return "any"
    if "girl" in s or "female" in s or "women" in s or "woman" in s or "ladies" in s:
        return "female"
    if "boy" in s or "male" in s or "men" in s or "man" in s or "gents" in s:
        return "male"
    return None


def gender_compatible(pref_gender: str, prop_gender: str) -> bool:
    """True if a property is physically bookable for the user's stated gender.

    Gender is a HARD constraint (a renter cannot book an opposite-gender-only
    PG), unlike amenities which stay soft/ranked. Permissive on 'Any'/co-living
    and on unknown values either side, so we never over-filter the predominantly
    'Any'-tagged inventory — only an explicit opposite-gender match is excluded.
    """
    pref = _gender_token(pref_gender)
    prop = _gender_token(prop_gender)
    if pref is None or prop is None:
        return True
    if pref == "any" or prop == "any":
        return True
    return pref == prop


# Strong gender labels managers embed in the free-text property NAME. Word-boundary
# matched (\b) so substrings never misfire: "cowboy"/"boyle"/"highgirls" carry no
# boundary before the token and are NOT detected. `girls?`/`boys?` also catch the
# possessive forms ("GIRL'S"/"BOY'S") because the apostrophe is a non-word char,
# so a boundary sits right after "girl"/"boy".
_NAME_FEMALE_RE = re.compile(r"\b(?:girls?|ladies|lady)\b")
_NAME_MALE_RE = re.compile(r"\b(?:boys?|gents?)\b")


def name_gender_token(name: str) -> Optional[str]:
    """Detect a deliberate gender label inside a property NAME.

    Returns 'male' | 'female' | 'any' (BOTH tokens → co-living) | None (no signal).

    Managers encode gender in the free-text name (e.g. "... KURLA GIRL'S") while the
    structured `pg_available_for` field is frequently left at the default "Any" — so
    for this inventory the name is a stronger gender signal than the tag.
    """
    s = (name or "").lower()
    if not s:
        return None
    has_female = bool(_NAME_FEMALE_RE.search(s))
    has_male = bool(_NAME_MALE_RE.search(s))
    if has_female and has_male:
        return "any"
    if has_female:
        return "female"
    if has_male:
        return "male"
    return None


def gender_compatible_listing(pref_gender: str, prop_gender: str, prop_name: str = "") -> bool:
    """Listing-aware gender compatibility — the predicate search.py actually uses.

    Same hard-constraint rule as gender_compatible(), but it also honours a gender
    label embedded in the property NAME. Because the structured tag is unreliable on
    this inventory (girls-only PGs are routinely tagged "Any"/blank), a single-gender
    NAME overrides the tag. A name carrying BOTH tokens (co-living "BOY'S/GIRL'S")
    stays bookable by anyone; a name with no gender token falls back to the structured
    tag (unchanged gender_compatible() behaviour).
    """
    pref = _gender_token(pref_gender)
    if pref is None or pref == "any":
        return True
    name_sig = name_gender_token(prop_name)
    if name_sig == "any":
        return True
    if name_sig is not None:
        return pref == name_sig
    return gender_compatible(pref_gender, prop_gender)


def match_score(
    property_data: dict,
    preferences: dict,
    amenity_weights: Optional[dict] = None,
    deal_breakers: Optional[list] = None,
    near_transit: bool = False,
    property_signals: Optional[dict] = None,
    weights: Optional[dict] = None,
) -> float:
    """Calculate a 0-100 match score between a property and user preferences.

    Scoring components (default caps; `weights` rescales them per intent — R8):
    - Budget match (0-30 pts)
    - Proximity (0-25 pts): real driving minutes to the user's destination
      (property_data["commute_minutes"]) → else straight-line km to it
      (property_data["commute_km"]) → else crow-flies distance from the search
      pin (0-20). The first two are office-proximity (R1); the last is the pin.
    - Amenity overlap (0-30 pts, with must-have vs nice-to-have weighting)
    - Property type match (0-10 pts)
    - Gender match (0-10 pts)
    - Transit proximity bonus (+5 if near metro/rail)
    - Deal-breaker penalty (-15 per match)

    `weights`: an intent profile from WEIGHT_PROFILES (see classify_intent).
    None → `balanced` → byte-identical to the pre-R8 fixed weights. Each
    component's sub-score is scaled by (profile_cap / balanced_cap), so the
    balanced profile multiplies everything by exactly 1.0. Gender and the
    deal-breaker penalty are never rescaled.
    """
    w = weights or _BALANCED
    score = 0.0

    # Budget match (30 pts)
    prop_rent = _parse_number(property_data.get("rent", property_data.get("rent_starts_from", 0)))
    min_budget = _parse_number(preferences.get("min_budget", 0))
    max_budget = _parse_number(preferences.get("max_budget", 100000))

    if prop_rent > 0:
        if min_budget <= prop_rent <= max_budget:
            budget_pts = 30
        elif prop_rent < min_budget:
            diff_pct = (min_budget - prop_rent) / max(min_budget, 1)
            budget_pts = max(0, 30 - diff_pct * 30)
        else:
            diff_pct = (prop_rent - max_budget) / max(max_budget, 1)
            budget_pts = max(0, 30 - diff_pct * 60)
        score += budget_pts * (w["budget"] / 30)

    # Proximity score (≤25 pts) — by REAL commute time when known, else by distance.
    # If the user told us their daily destination (office/college) we rank by actual
    # driving minutes to that place (the right-first signal), which REPLACES the
    # crow-flies distance from the searched area. When no commute time is known we
    # fall back to the original distance term, so users without a destination see
    # unchanged behaviour. Budget/amenities keep their weight, so commute complements
    # the score rather than overriding a clearly better-value property.
    commute_min = property_data.get("commute_minutes")
    commute_km = property_data.get("commute_km")
    if commute_min is not None:
        # Precise driving time to the destination (OSRM). Office-proximity cap.
        cm = _parse_number(commute_min)
        if cm <= 15:
            prox_pts = 25
        elif cm <= 30:
            prox_pts = 25 - (cm - 15) * (10.0 / 15.0)   # 25 → 15
        elif cm <= 60:
            prox_pts = max(0.0, 15 - (cm - 30) * 0.5)    # 15 → 0
        else:
            prox_pts = 0.0                                # beyond 60 min, too far
        score += prox_pts * (w["prox_commute"] / 25)
    elif commute_km is not None:
        # Straight-line distance to the destination (honest fallback when the
        # routing service is unavailable). Still office-proximity — the R1 signal
        # — just measured as the crow flies instead of by road.
        km = _parse_number(commute_km)
        if km <= 2:
            prox_pts = 25
        elif km <= 5:
            prox_pts = 25 - (km - 2) * (10.0 / 3.0)      # 25 → 15
        elif km <= 12:
            prox_pts = max(0.0, 15 - (km - 5) * (15.0 / 7.0))  # 15 → 0
        else:
            prox_pts = 0.0                                # beyond 12 km
        score += prox_pts * (w["prox_commute"] / 25)
    else:
        distance = property_data.get("distance", property_data.get("distanceBwPropertyAndSearchArea"))
        if distance is not None:
            dist_km = _parse_number(distance) / 1000.0
            if dist_km <= 2:
                dist_pts = 20
            elif dist_km <= 5:
                dist_pts = max(0, 20 - (dist_km - 2) * 4)
            elif dist_km <= 10:
                dist_pts = max(0, 8 - (dist_km - 5))
            else:
                dist_pts = 0                              # beyond 10 km
            score += dist_pts * (w["prox_distance"] / 20)

    # Amenity overlap (30 pts) — with weighted must-have / nice-to-have
    must_have = _parse_amenities(preferences.get("must_have_amenities", ""))
    nice_to_have = _parse_amenities(preferences.get("nice_to_have_amenities", ""))
    # Fallback: if no split preferences, use flat amenities list
    all_amenities = _parse_amenities(preferences.get("amenities", ""))
    if not must_have and not nice_to_have:
        must_have = all_amenities  # treat all as must-have by default

    prop_amenities = _parse_amenities(
        property_data.get("amenities", property_data.get("commonAmenities", ""))
    )

    if must_have or nice_to_have:
        amenity_score = 0.0
        # Must-have: 20 pts total. Missing any → cap amenity score at 10
        if must_have:
            must_matched = _fuzzy_amenity_match(must_have, prop_amenities)
            must_ratio = must_matched / len(must_have)
            amenity_score += must_ratio * 20
            if must_matched < len(must_have):
                # Hard penalty: cap total score later
                amenity_score = min(amenity_score, 10)
        else:
            amenity_score += 10  # neutral if no must-haves

        # Nice-to-have: 10 pts total (bonus)
        if nice_to_have:
            nice_matched = _fuzzy_amenity_match(nice_to_have, prop_amenities)
            amenity_score += (nice_matched / len(nice_to_have)) * 10
        else:
            amenity_score += 5  # neutral

        score += min(30, amenity_score) * (w["amenity"] / 30)
    else:
        score += 15 * (w["amenity"] / 30)  # No preference = neutral

    # Property type match (10 pts)
    pref_type = (preferences.get("property_type") or "").lower()
    prop_type = (property_data.get("property_type") or "").lower()
    if pref_type and prop_type:
        type_pts = 10 if (pref_type in prop_type or prop_type in pref_type) else 0
    else:
        type_pts = 5  # No preference
    score += type_pts * (w["property_type"] / 10)

    # Gender match (10 pts)
    pref_gender = (preferences.get("pg_available_for") or "").lower()
    prop_gender = (property_data.get("pg_available_for") or "").lower()
    if pref_gender and prop_gender:
        if pref_gender == "any" or prop_gender == "any" or pref_gender in prop_gender:
            score += 10
    else:
        score += 5

    # Transit proximity bonus (+5 if property is near metro/rail station)
    if near_transit:
        score += w["transit"]

    # Deal-breaker penalty (-15 per match, from cross-session memory)
    if deal_breakers:
        prop_text = " ".join([
            str(property_data.get("amenities", "")),
            str(property_data.get("commonAmenities", "")),
            str(property_data.get("pg_available_for", "")),
            str(property_data.get("property_type", "")),
        ]).lower()

        for db in deal_breakers:
            db_lower = db.lower()
            # "no AC" style: check if the amenity is absent
            if db_lower.startswith("no "):
                amenity = db_lower[3:].strip()
                if not _fuzzy_amenity_match({amenity}, prop_amenities):
                    score -= 15
            # "far from metro" style: check if keyword is present in property text
            elif db_lower in prop_text:
                score -= 15

    # Outcome-based signal adjustment (Sprint 5 — outcome-aware recommendations)
    # property_signals: {converted: N, lost: N, no_show: N} from admin lead outcomes
    if property_signals:
        converted = property_signals.get("converted", 0)
        no_show = property_signals.get("no_show", 0)
        # Social proof: conversions boost confidence (+3 per conversion, capped)
        if converted > 0:
            score += min(converted * 3, w["outcome_max"])
        # Risk signal: repeated no-shows suggest property issues (penalty if 2+)
        if no_show >= 2:
            score -= w["noshow"]

    return round(min(100, max(0, score)), 1)


def indicator(score: float) -> str:
    """Return a visual indicator for a match score."""
    if score >= 80:
        return "Excellent Match"
    elif score >= 60:
        return "Good Match"
    elif score >= 40:
        return "Fair Match"
    return "Low Match"


def _parse_number(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = "".join(c for c in value if c.isdigit() or c == ".")
        try:
            return float(cleaned)
        except ValueError:
            return 0.0
    return 0.0


def _parse_amenities(value) -> set:
    if isinstance(value, list):
        return {a.strip().lower() for a in value if a.strip()}
    if isinstance(value, str):
        return {a.strip().lower() for a in value.split(",") if a.strip()}
    return set()
