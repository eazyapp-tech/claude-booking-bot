"""
test_gender_filter.py — search relevance: gender is a HARD constraint.

Locks in the relevance fix found on 2026-06-01. Two coupled facts on the
property-filtering surface:

  1. Gender is a HARD constraint, not a soft preference. A renter physically
     cannot book an opposite-gender-only PG, so utils/scoring.py exposes a
     pure `gender_compatible(pref, prop)` predicate and tools/broker/search.py
     EXCLUDES incompatible inventory after scoring (rather than merely ranking
     it a few points lower, which left girls-only PGs visible to boys seekers).
     "Any"/co-living and unknown values stay permissive so we never over-filter
     the predominantly "Any"-tagged inventory. If the filter empties the list
     (area has only opposite-gender stock) the bot is HONEST about it instead of
     padding results with unbookable options.

     Amenities are deliberately LEFT as a soft ranking signal — renters
     compromise on amenities, and hard-filtering them would risk empty results.

  2. The dead `sharing_type_enabled` (singular) payload key is removed. The pref
     is stored as `sharing_types_enabled` (plural) and the Rentok backend reads
     the plural form, so the singular key was a silent no-op. Per the documented
     contract (see test_contract_alignment.py §3) gender/unit-type/sharing
     filters are intentionally NOT sent to the search API — they over-exclude
     "Any" co-living inventory — so the key is dropped, not "corrected".

Deterministic: in-memory fake replaces Redis; geocode / image-enrich / search
API are stubbed. No network, no LLM. Run: `python test_gender_filter.py`.
"""

import asyncio
import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

import db.redis._base as _base  # noqa: E402


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, key, val, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = val
        return True

    def setex(self, key, ttl, val):
        self.store[key] = val
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)


_fake = _FakeRedis()
_base._r = lambda: _fake

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


def arun(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# 1. gender_compatible — pure predicate, substring-trap safe
# --------------------------------------------------------------------------- #
print("\n[1] gender_compatible — hard-constraint predicate")
from utils.scoring import (  # noqa: E402
    gender_compatible,
    gender_compatible_listing,
    name_gender_token,
)

# Compatible cases
check("1a boys pref + boys prop", gender_compatible("All Boys", "All Boys"))
check("1b girls pref + girls prop", gender_compatible("All Girls", "All Girls"))
check("1c boys pref + Any prop (co-living)", gender_compatible("All Boys", "Any"))
check("1d Any pref + girls prop", gender_compatible("Any", "All Girls"))
check("1e empty pref → permissive", gender_compatible("", "All Girls"))
check("1f empty prop → permissive", gender_compatible("All Boys", ""))
check("1g unknown prop string → permissive", gender_compatible("All Boys", "Co-Living Hostel"))

# Incompatible cases — the bug being fixed
check("1h boys pref EXCLUDES girls prop", not gender_compatible("All Boys", "All Girls"))
check("1i girls pref EXCLUDES boys prop", not gender_compatible("All Girls", "All Boys"))

# Substring traps: 'male' ⊂ 'female', 'men' ⊂ 'women'
check("1j 'male' pref vs 'female' prop EXCLUDED", not gender_compatible("Male", "Female"))
check("1k 'female' pref vs 'male' prop EXCLUDED", not gender_compatible("Female", "Male"))
check("1l 'men' pref vs 'women' prop EXCLUDED", not gender_compatible("men", "women"))
check("1m 'women' pref vs 'women' prop OK", gender_compatible("women", "women"))


# --------------------------------------------------------------------------- #
# search_properties — wire up hermetic stubs
# --------------------------------------------------------------------------- #
import tools.broker.search as search  # noqa: E402


async def _noop_async(*a, **k):
    return None


def _install_stubs(props, prefs):
    """Patch every side-effecting seam so only the filter logic runs live."""
    _captured = {"payloads": []}

    async def _geocode(_loc):
        return (19.07, 72.87)

    async def _call_api(payload):
        _captured["payloads"].append(payload)
        return [dict(p) for p in props]

    search.geocode_address = _geocode
    search._call_search_api = _call_api
    search._enrich_with_images = _noop_async
    search._geocode_properties = _noop_async
    search.get_preferences = lambda uid: dict(prefs)
    search.redis_save_preferences = lambda *a, **k: None
    search.get_whitelabel_pg_ids = lambda uid: ["pg1"]
    search.get_user_memory = lambda uid: {"deal_breakers": []}
    search.get_user_brand = lambda uid: ""
    search.get_property_info_map = lambda uid: []
    search.set_property_info_map = lambda *a, **k: None
    search.set_property_id_for_search = lambda *a, **k: None
    search.set_last_search_results = lambda *a, **k: None
    search.save_property_template = lambda *a, **k: None
    search.track_funnel = lambda *a, **k: None
    search.record_property_viewed = lambda *a, **k: None
    search.track_property_event = lambda *a, **k: None
    search.update_user_memory = lambda *a, **k: None
    return _captured


_BOYS = {"p_id": "1", "p_pg_name": "Boys Haven", "p_pg_available_for": "All Boys",
         "p_rent_starts_from": 9000, "p_pg_id": "pgb"}
_ANY = {"p_id": "2", "p_pg_name": "Coliving Co", "p_pg_available_for": "Any",
        "p_rent_starts_from": 9500, "p_pg_id": "pgc"}
_GIRLS = {"p_id": "3", "p_pg_name": "Jyoti Sparkle", "p_pg_available_for": "All Girls",
          "p_rent_starts_from": 8500, "p_pg_id": "pgg"}


# --------------------------------------------------------------------------- #
# 2. Boys seeker — girls-only inventory EXCLUDED, Any retained
# --------------------------------------------------------------------------- #
print("\n[2] boys seeker — girls-only PG excluded, co-living kept")
_install_stubs([_BOYS, _ANY, _GIRLS],
               {"location": "Kurla, Mumbai", "pg_available_for": "All Boys",
                "max_budget": 15000})
res2 = arun(search.search_properties("u_boys"))
check("2a girls-only 'Jyoti Sparkle' NOT shown to boys seeker",
      "Jyoti Sparkle" not in res2, res2)
check("2b boys property IS shown", "Boys Haven" in res2, res2)
check("2c co-living (Any) IS shown", "Coliving Co" in res2, res2)


# --------------------------------------------------------------------------- #
# 3. No gender preference — nothing filtered
# --------------------------------------------------------------------------- #
print("\n[3] no gender preference — all inventory retained")
_install_stubs([_BOYS, _ANY, _GIRLS],
               {"location": "Kurla, Mumbai", "max_budget": 15000})
res3 = arun(search.search_properties("u_nopref"))
check("3a no pref → girls property still shown", "Jyoti Sparkle" in res3, res3)
check("3b no pref → boys property still shown", "Boys Haven" in res3, res3)


# --------------------------------------------------------------------------- #
# 4. Area has ONLY opposite-gender stock — honest, not padded
# --------------------------------------------------------------------------- #
print("\n[4] all inventory opposite-gender — honest empty state")
_install_stubs([_GIRLS],
               {"location": "Kurla, Mumbai", "pg_available_for": "All Boys",
                "max_budget": 15000})
res4 = arun(search.search_properties("u_boys2"))
check("4a does NOT pad with unbookable girls-only PG",
      "Jyoti Sparkle" not in res4, res4)
check("4b honest about gender mismatch",
      "different gender" in res4.lower() or "for a different gender" in res4.lower(), res4)


# --------------------------------------------------------------------------- #
# 6. name_gender_token + gender_compatible_listing — NAME overrides a bad tag
#    Live-confirmed (2026-06-05): on OxOtel inventory, girls-only PGs like
#    "Mass U Foria E 201 KURLA GIRL'S" / "NATASHA AVENUE GHATKOPAR GIRL'S" are
#    tagged p_pg_available_for="Any", so the tag-only filter leaked them into a
#    boys search. The deliberate GIRL'S/BOY'S name label is the reliable signal.
# --------------------------------------------------------------------------- #
print("\n[6] name_gender_token + listing predicate — name overrides unreliable tag")

# name_gender_token — pure detection, word-boundary + possessive forms
check("6a 'GIRL'S' name → female", name_gender_token("Mass U Foria E 201 KURLA GIRL'S") == "female")
check("6b 'BOY'S' name → male", name_gender_token("ROHA VATIKA 1406 BOY'S KURLA") == "male")
check("6c plain 'Girls' name → female", name_gender_token("Hill View Vikhroli B 603 Girls") == "female")
check("6d co-living 'BOY'S/GIRL'S' → any", name_gender_token("Indrayani GHATKOPAR BOY'S/GIRL'S") == "any")
check("6e no gender token → None", name_gender_token("Mass Metropolis A 1003 (NEW)") is None)
# Substring traps must NOT misfire (no word boundary before the token)
check("6f 'Cowboy' not detected as male", name_gender_token("Cowboy Residency") is None)
check("6g 'Boyle Mansion' not detected as male", name_gender_token("Boyle Mansion") is None)
check("6h empty name → None", name_gender_token("") is None)

# gender_compatible_listing — the predicate search.py uses
# The live bug: tag "Any" but name says GIRL'S → must EXCLUDE for a boys seeker.
check("6i boys pref EXCLUDES 'GIRL'S'-named even when tag=Any",
      not gender_compatible_listing("All Boys", "Any", "NATASHA AVENUE GHATKOPAR GIRL'S"))
check("6j boys pref KEEPS 'BOY'S'-named with tag=Any",
      gender_compatible_listing("All Boys", "Any", "ROHA VATIKA 1406 BOY'S KURLA"))
check("6k girls pref EXCLUDES 'BOY'S'-named with tag=Any",
      not gender_compatible_listing("All Girls", "Any", "PUNEET 1506 KURLA BOY'S"))
check("6l girls pref KEEPS 'GIRL'S'-named with tag=Any",
      gender_compatible_listing("All Girls", "Any", "Mass U Foria E 201 KURLA GIRL'S"))
check("6m co-living 'BOY'S/GIRL'S' name kept for BOTH genders",
      gender_compatible_listing("All Boys", "Any", "Indrayani BOY'S/GIRL'S")
      and gender_compatible_listing("All Girls", "Any", "Indrayani BOY'S/GIRL'S"))
check("6n no-token name falls back to tag (Any kept)",
      gender_compatible_listing("All Boys", "Any", "Mass Metropolis B504"))
check("6o no-token name falls back to tag (real opposite tag excluded)",
      not gender_compatible_listing("All Boys", "All Girls", "Plain Residency"))
check("6p no gender pref → name never excludes",
      gender_compatible_listing("", "Any", "NATASHA AVENUE GHATKOPAR GIRL'S"))


# --------------------------------------------------------------------------- #
# 7. End-to-end through search — mis-tagged GIRL'S inventory excluded for boys
# --------------------------------------------------------------------------- #
print("\n[7] search excludes mis-tagged GIRL'S-named PGs from a boys search")
_MISTAGGED_GIRLS = {"p_id": "10", "p_pg_name": "NATASHA AVENUE GHATKOPAR GIRL'S",
                    "p_pg_available_for": "Any", "p_rent_starts_from": 9000, "p_pg_id": "pgm1"}
_MISTAGGED_GIRLS2 = {"p_id": "11", "p_pg_name": "Mass U Foria E 201 KURLA GIRL'S",
                     "p_pg_available_for": "Any", "p_rent_starts_from": 9500, "p_pg_id": "pgm2"}
_NAMED_BOYS_ANY = {"p_id": "12", "p_pg_name": "ROHA VATIKA 1406 BOY'S KURLA",
                   "p_pg_available_for": "Any", "p_rent_starts_from": 9200, "p_pg_id": "pgm3"}
_COLIVING_NAME = {"p_id": "13", "p_pg_name": "Indrayani GHATKOPAR BOY'S/GIRL'S",
                  "p_pg_available_for": "Any", "p_rent_starts_from": 8800, "p_pg_id": "pgm4"}

_install_stubs([_MISTAGGED_GIRLS, _MISTAGGED_GIRLS2, _NAMED_BOYS_ANY, _COLIVING_NAME],
               {"location": "Kurla, Mumbai", "pg_available_for": "All Boys",
                "max_budget": 15000})
res7 = arun(search.search_properties("u_boys_mistag"))
check("7a 'NATASHA ... GIRL'S' (tag=Any) NOT shown to boys seeker",
      "NATASHA AVENUE" not in res7, res7)
check("7b 'Mass U Foria ... GIRL'S' (tag=Any) NOT shown to boys seeker",
      "Mass U Foria" not in res7, res7)
check("7c 'ROHA VATIKA ... BOY'S' (tag=Any) IS shown", "ROHA VATIKA" in res7, res7)
check("7d co-living 'BOY'S/GIRL'S' IS shown", "Indrayani" in res7, res7)


# --------------------------------------------------------------------------- #
# 5. Payload hygiene — dead sharing_type_enabled key removed
# --------------------------------------------------------------------------- #
print("\n[5] payload omits sharing-type filter (singular + plural)")
cap = _install_stubs([_BOYS, _ANY],
                     {"location": "Kurla, Mumbai", "pg_available_for": "All Boys",
                      "sharing_types_enabled": "2", "max_budget": 15000})
arun(search.search_properties("u_sharing"))
check("5a at least one payload built", len(cap["payloads"]) >= 1)
for i, p in enumerate(cap["payloads"]):
    check(f"5b[{i}] singular 'sharing_type_enabled' NOT sent",
          "sharing_type_enabled" not in p, p)
    check(f"5c[{i}] plural 'sharing_types_enabled' NOT sent",
          "sharing_types_enabled" not in p, p)
    check(f"5d[{i}] pg_ids IS sent (hard backend requirement)", "pg_ids" in p, p)


# --------------------------------------------------------------------------- #
print(f"\n{'='*60}")
print(f"RESULTS: {_passed} passed, {_failed} failed")
print(f"{'='*60}")
sys.exit(1 if _failed else 0)
