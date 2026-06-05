"""
test_commute_ranking.py — R1: rank by the user's REAL daily commute, not the search pin.

The marquee relevance fix. Today search ranks by crow-flies distance from the area
the user *named* (the search pin). But people pick a home for how easy it is to reach
the place they go every day — office, college. R1 captures that destination once
(the existing optional `commute_from` preference) and, when present, re-ranks the top
candidates by REAL driving minutes to it.

Five coupled facts are locked here, all deterministic (no network / Redis / LLM):

  1. SCORING BLEND (utils/scoring.match_score) — a commute term REPLACES the
     area-distance term ONLY when a real commute time is known. Graded: ≤15 min is
     excellent (full points), decaying to 0 by ~60 min. Budget and amenities keep
     their weight, so commute COMPLEMENTS the score, never OVERRIDES a clearly
     better-value property. When no commute time is present the score is
     BYTE-IDENTICAL to today (zero regression for users without a destination).

  2. COMPUTE (search._compute_commute_minutes) — ONE OSRM table call
     (source = destination, destinations = top-N properties) fills `_commute_min`
     per property. Efficient: a single matrix request, not N point-to-point calls.

  3. GRACEFUL — every failure path (no destination coords, no property coords,
     OSRM error) leaves properties untouched so ranking quietly degrades to area
     distance. It NEVER raises and never blanks the result set.

  4. RE-RANK (search.search_properties, end-to-end) — a property that is far from
     the searched area but CLOSE by commute rises above one that is near the area
     but far by commute. With no destination set, the original area order stands.

  5. SURFACE (search.build_carousel_items) — when a commute time is known the
     native card item carries `commute` = "X min to <dest>", additively (absent
     otherwise, so the carousel contract is unchanged for non-commute searches).

Run: `python test_commute_ranking.py`  (exit 0 = pass).
"""

import asyncio
import math
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
# 1. Scoring blend — commute replaces distance, complements (never overrides)
# --------------------------------------------------------------------------- #
print("\n[1] match_score — commute term blends without overriding budget/amenities")
from utils.scoring import match_score  # noqa: E402

# Common prefs: budget 8000-15000, one must-have amenity.
_prefs = {"min_budget": 8000, "max_budget": 15000, "must_have_amenities": "wifi"}

# A property in budget, has wifi, 1 km from the search pin (no commute known).
_base_prop = {"rent": 10000, "distance": 1000, "amenities": "wifi, parking"}
_baseline = match_score(_base_prop, _prefs)

# 1a — commute_minutes ABSENT ⇒ score identical to today (regression guard).
_again = match_score(dict(_base_prop), _prefs)
check("1a no commute → score unchanged (regression guard)", _again == _baseline,
      f"{_again} != {_baseline}")

# 1b — an explicit None commute_minutes is treated as 'unknown' (still distance).
_none = match_score({**_base_prop, "commute_minutes": None}, _prefs)
check("1b commute_minutes=None → falls back to distance, == baseline",
      _none == _baseline, f"{_none} != {_baseline}")

# 1c — a short commute scores AT LEAST as well as the same prop ranked by distance
#      (≤15 min commute = 25 pts vs ≤2 km distance = 20 pts).
_near = match_score({**_base_prop, "commute_minutes": 8}, _prefs)
check("1c short commute (8 min) ≥ baseline distance score", _near >= _baseline,
      f"{_near} < {_baseline}")

# 1d — grading is monotonic: nearer by commute always scores higher.
_g5 = match_score({**_base_prop, "commute_minutes": 5}, _prefs)
_g20 = match_score({**_base_prop, "commute_minutes": 20}, _prefs)
_g45 = match_score({**_base_prop, "commute_minutes": 45}, _prefs)
_g90 = match_score({**_base_prop, "commute_minutes": 90}, _prefs)
check("1d commute grading monotonic (5 > 20 > 45 ≥ 90)",
      _g5 > _g20 > _g45 >= _g90, f"{_g5},{_g20},{_g45},{_g90}")

# 1e — a very long commute (>60 min) contributes no proximity points.
_g0 = match_score({**_base_prop, "commute_minutes": 5000, "distance": None}, _prefs)
_g_far = match_score({**_base_prop, "commute_minutes": 75, "distance": None}, _prefs)
check("1e >60 min commute → 0 proximity pts (== no-distance baseline)",
      _g_far == _g0, f"{_g_far} != {_g0}")

# 1f — COMPLEMENT, NOT OVERRIDE: a great-value property with a long commute still
#      beats a poor-value property that happens to be 5 min away.
_strong = match_score(
    {"rent": 11000, "amenities": "wifi, ac", "commute_minutes": 60}, _prefs)   # in budget + wifi, far commute
_weak = match_score(
    {"rent": 40000, "amenities": "pool", "commute_minutes": 5}, _prefs)        # way over budget, no wifi, near
check("1f budget/amenity winner still outranks a near-but-bad-value option",
      _strong > _weak, f"strong={_strong} weak={_weak}")


# --------------------------------------------------------------------------- #
# 2 + 3 + 4. search.py — compute, graceful degradation, end-to-end re-rank
# --------------------------------------------------------------------------- #
import tools.broker.search as search  # noqa: E402


async def _noop_async(*a, **k):
    return None


# OSRM table stub: source (index 0) → 0 s; each property keyed by a lat marker in
# the coord string. PropA (lat 19.075) is 45 min away; PropB (lat 19.25) is 5 min.
async def _osrm_stub(url, params=None):
    coord_part = url.rsplit("/", 1)[-1]
    coords = coord_part.split(";")
    row = []
    for i, c in enumerate(coords):
        if i == 0:
            row.append(0.0)                 # destination → itself
        elif "19.075" in c:
            row.append(2700.0)              # PropA: 45 min — far by commute
        elif "19.25" in c:
            row.append(300.0)               # PropB: 5 min — near by commute
        else:
            row.append(1800.0)              # default 30 min
    return {"durations": [row]}


async def _geocode_stub(loc):
    l = (loc or "").lower()
    if "powai" in l:
        return (19.25, 72.96)               # commute destination (near PropB)
    return (19.07, 72.87)                   # search pin


# PropA: near the search area (0.5 km), but FAR by commute.
_PROP_A = {"p_id": "A", "p_pg_name": "Near-Area Far-Commute PG",
           "p_rent_starts_from": 10000, "p_pg_id": "pgA", "p_distance": 500,
           "p_latitude": "19.075", "p_longitude": "72.875"}
# PropB: FAR from the search area (9 km), but NEAR by commute.
_PROP_B = {"p_id": "B", "p_pg_name": "Far-Area Near-Commute PG",
           "p_rent_starts_from": 10000, "p_pg_id": "pgB", "p_distance": 9000,
           "p_latitude": "19.25", "p_longitude": "72.96"}


def _install_stubs(props, prefs):
    captured = {}

    async def _call_api(payload):
        return [dict(p) for p in props]

    search.geocode_address = _geocode_stub
    search.http_get = _osrm_stub
    search._call_search_api = _call_api
    search._enrich_with_images = _noop_async
    search._geocode_properties = _noop_async        # props carry coords already
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
    search.set_search_carousel = lambda uid, items, center: captured.update(
        carousel=items, center=center)
    return captured


# 2. compute helper — one OSRM call assigns per-property minutes.
print("\n[2] _compute_commute_minutes — one OSRM table call fills _commute_min")
_install_stubs([_PROP_A, _PROP_B], {})
_props = [dict(_PROP_A), dict(_PROP_B)]
arun(search._compute_commute_minutes(_props, "Powai, Mumbai"))
_by_name = {p["p_pg_name"]: p for p in _props}
check("2a PropA got a far drive time (45 min)",
      _by_name["Near-Area Far-Commute PG"].get("_commute_min") == 45,
      _by_name["Near-Area Far-Commute PG"].get("_commute_min"))
check("2b PropB got a near drive time (5 min)",
      _by_name["Far-Area Near-Commute PG"].get("_commute_min") == 5,
      _by_name["Far-Area Near-Commute PG"].get("_commute_min"))


# 3. graceful degradation — failures leave properties untouched, never raise.
print("\n[3] graceful — every failure path degrades to area ranking, never raises")

# 3a — empty / vague destination → no compute, no crash.
_p = [dict(_PROP_A)]
arun(search._compute_commute_minutes(_p, ""))
check("3a empty destination → nothing computed", "_commute_min" not in _p[0])
_p = [dict(_PROP_A)]
arun(search._compute_commute_minutes(_p, "office"))
check("3b vague destination ('office') → nothing computed", "_commute_min" not in _p[0])

# 3c — destination geocode fails → nothing computed, no raise.
async def _geo_none(_loc):
    return (None, None)
search.geocode_address = _geo_none
_p = [dict(_PROP_A)]
arun(search._compute_commute_minutes(_p, "Powai"))
check("3c destination geocode fails → nothing computed", "_commute_min" not in _p[0])
search.geocode_address = _geocode_stub

# 3d — OSRM raises → nothing computed, no raise.
async def _osrm_boom(url, params=None):
    raise RuntimeError("OSRM down")
search.http_get = _osrm_boom
_p = [dict(_PROP_A)]
arun(search._compute_commute_minutes(_p, "Powai"))
check("3d OSRM error → nothing computed, no raise", "_commute_min" not in _p[0])
search.http_get = _osrm_stub

# 3e — property without coordinates is skipped quietly.
_no_coords = {"p_id": "C", "p_pg_name": "No Coords PG", "p_rent_starts_from": 10000}
_p = [dict(_no_coords)]
arun(search._compute_commute_minutes(_p, "Powai"))
check("3e property without coords → skipped, no _commute_min", "_commute_min" not in _p[0])


# 4. end-to-end re-rank through search_properties.
print("\n[4] search_properties — commute re-ranks the top candidates")

# 4a — WITHOUT a commute destination: original area order stands (A before B).
_install_stubs([_PROP_A, _PROP_B], {"location": "Kurla, Mumbai", "max_budget": 15000})
_res_area = arun(search.search_properties("u_area"))
_a_idx = _res_area.find("Near-Area Far-Commute PG")
_b_idx = _res_area.find("Far-Area Near-Commute PG")
check("4a no destination → near-area PG ranked first (unchanged)",
      0 <= _a_idx < _b_idx, f"a={_a_idx} b={_b_idx}")

# 4b — WITH a commute destination: the near-by-commute PG rises to the top.
cap = _install_stubs([_PROP_A, _PROP_B],
                     {"location": "Kurla, Mumbai", "max_budget": 15000,
                      "commute_from": "Powai, Mumbai"})
_res_commute = arun(search.search_properties("u_commute"))
_a_idx2 = _res_commute.find("Near-Area Far-Commute PG")
_b_idx2 = _res_commute.find("Far-Area Near-Commute PG")
check("4b commute destination → near-commute PG ranked first (re-ranked)",
      0 <= _b_idx2 < _a_idx2, f"a={_a_idx2} b={_b_idx2}")

# 4c — the surfaced carousel carries the commute label on the top (re-ranked) card.
_top = (cap.get("carousel") or [{}])[0]
check("4c top card surfaces 'X min to <dest>'",
      _top.get("commute") == "5 min to Powai", _top.get("commute"))


# --------------------------------------------------------------------------- #
# 5. build_carousel_items — commute is additive (absent when not computed)
# --------------------------------------------------------------------------- #
print("\n[5] build_carousel_items — commute field additive, contract unchanged")
_info_with = {"property_name": "X", "property_rent": "10000",
              "commute_minutes": 12, "commute_label": "Powai"}
_info_without = {"property_name": "Y", "property_rent": "9000"}

_items_w, _ = search.build_carousel_items([_info_with], "19.07", "72.87")
check("5a info with commute → item['commute'] == '12 min to Powai'",
      _items_w[0].get("commute") == "12 min to Powai", _items_w[0].get("commute"))

_items_wo, _ = search.build_carousel_items([_info_without], "19.07", "72.87")
check("5b info without commute → NO 'commute' key (contract preserved)",
      "commute" not in _items_wo[0], _items_wo[0])


# --------------------------------------------------------------------------- #
print(f"\n{'='*60}")
print(f"RESULT: {_passed} passed, {_failed} failed")
print(f"{'='*60}")
sys.exit(0 if _failed == 0 else 1)
