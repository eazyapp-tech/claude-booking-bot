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
) -> float:
    """Calculate a 0-100 match score between a property and user preferences.

    Scoring components:
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
    """
    score = 0.0

    # Budget match (30 pts)
    prop_rent = _parse_number(property_data.get("rent", property_data.get("rent_starts_from", 0)))
    min_budget = _parse_number(preferences.get("min_budget", 0))
    max_budget = _parse_number(preferences.get("max_budget", 100000))

    if prop_rent > 0:
        if min_budget <= prop_rent <= max_budget:
            score += 30
        elif prop_rent < min_budget:
            diff_pct = (min_budget - prop_rent) / max(min_budget, 1)
            score += max(0, 30 - diff_pct * 30)
        else:
            diff_pct = (prop_rent - max_budget) / max(max_budget, 1)
            score += max(0, 30 - diff_pct * 60)

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
        # Precise driving time to the destination (OSRM).
        cm = _parse_number(commute_min)
        if cm <= 15:
            score += 25
        elif cm <= 30:
            score += 25 - (cm - 15) * (10.0 / 15.0)   # 25 → 15
        elif cm <= 60:
            score += max(0.0, 15 - (cm - 30) * 0.5)    # 15 → 0
        # Beyond 60 min, 0 points — too far to count as proximity
    elif commute_km is not None:
        # Straight-line distance to the destination (honest fallback when the
        # routing service is unavailable). Still office-proximity — the R1 signal
        # — just measured as the crow flies instead of by road.
        km = _parse_number(commute_km)
        if km <= 2:
            score += 25
        elif km <= 5:
            score += 25 - (km - 2) * (10.0 / 3.0)      # 25 → 15
        elif km <= 12:
            score += max(0.0, 15 - (km - 5) * (15.0 / 7.0))  # 15 → 0
        # Beyond 12 km, 0 points
    else:
        distance = property_data.get("distance", property_data.get("distanceBwPropertyAndSearchArea"))
        if distance is not None:
            dist_km = _parse_number(distance) / 1000.0
            if dist_km <= 2:
                score += 20
            elif dist_km <= 5:
                score += max(0, 20 - (dist_km - 2) * 4)
            elif dist_km <= 10:
                score += max(0, 8 - (dist_km - 5))
            # Beyond 10km, 0 points

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

        score += min(30, amenity_score)
    else:
        score += 15  # No preference = neutral

    # Property type match (10 pts)
    pref_type = (preferences.get("property_type") or "").lower()
    prop_type = (property_data.get("property_type") or "").lower()
    if pref_type and prop_type:
        if pref_type in prop_type or prop_type in pref_type:
            score += 10
    else:
        score += 5  # No preference

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
        score += 5

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
        # Social proof: conversions boost confidence (+3 per conversion, max +9)
        if converted > 0:
            score += min(converted * 3, 9)
        # Risk signal: repeated no-shows suggest property issues (-5 if 2+)
        if no_show >= 2:
            score -= 5

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
