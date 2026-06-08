"""
Tenant similarity + room availability tool regression tests.

Checks:
  A) fetch_room_availability output formatting (working_type match, hometown match, tenure)
  B) save_preferences captures working_type + hometown
  C) skill_map includes fetch_room_availability in details tools
  D) registry imports the tool without error
  E) _compat_hint logic: professional match, student match, mixed → no hint, unknown → no hint
"""
import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Minimal stubs so tools import without real Redis / config
# ---------------------------------------------------------------------------

def _stub_modules():
    for name in ["config", "db.redis_store", "utils.properties", "utils.api"]:
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)

    cfg_mod = sys.modules["config"]
    settings = MagicMock()
    settings.RENTOK_API_BASE_URL = "https://apiv2.rentok.com"
    cfg_mod.settings = settings

    redis_mod = sys.modules["db.redis_store"]
    redis_mod.get_preferences = MagicMock(return_value={})
    redis_mod.save_preferences = MagicMock()
    redis_mod.get_preferences = MagicMock(return_value={})
    redis_mod.add_deal_breaker = MagicMock()

    props_mod = sys.modules["utils.properties"]
    props_mod.find_property = MagicMock()

    api_mod = sys.modules["utils.api"]
    api_mod.parse_amenities = MagicMock(return_value="")
    api_mod.parse_sharing_types = MagicMock(return_value="")

    # db package
    if "db" not in sys.modules:
        sys.modules["db"] = types.ModuleType("db")


_stub_modules()

from tools.broker.room_availability import (  # noqa: E402
    _compat_hint,
    _format_room,
    fetch_room_availability,
)
from tools.broker.preferences import save_preferences  # noqa: E402


def check(cond: bool, msg: str):
    if not cond:
        print(f"  FAIL: {msg}")
        return False
    print(f"  PASS: {msg}")
    return True


# ---------------------------------------------------------------------------
# A) _compat_hint logic
# ---------------------------------------------------------------------------

def test_compat_hint():
    print("\n[A] _compat_hint")
    ok = True
    ok &= check(_compat_hint("professional", "professional") == " ← fellow professionals",
                "professional vs professional → hint")
    ok &= check(_compat_hint("working professional", "professional") == " ← fellow professionals",
                "working professional vs professional → hint")
    ok &= check(_compat_hint("student", "student") == " ← fellow students",
                "student vs student → hint")
    ok &= check(_compat_hint("professional", "student") == "",
                "mismatch → no hint")
    ok &= check(_compat_hint("professional", "mixed") == "",
                "mixed room → no hint")
    ok &= check(_compat_hint("professional", "unknown") == "",
                "unknown working_type → no hint")
    ok &= check(_compat_hint("", "professional") == "",
                "unknown user → no hint")
    return ok


# ---------------------------------------------------------------------------
# B) _format_room output
# ---------------------------------------------------------------------------

def _make_room(kind="vacant_now", total=2, wtype="professional",
               gender="all_male", tenure=8, cities=None, tags=None) -> dict:
    return {
        "room_name": "Room 3",
        "sharing_type": 2,
        "rent_per_bed": 9000,
        "availability_kind": kind,
        "next_available_from": None,
        "is_available": kind != "occupied",
        "tenant_mix": {
            "total_tenants": total,
            "gender": gender,
            "working_type": wtype,
            "avg_tenure_months": tenure,
            "top_origin_cities": cities if cities is not None else ["Pune", "Bangalore"],
        },
        "tags": tags or ["AC", "attached bathroom"],
    }


def test_format_room():
    print("\n[B] _format_room output")
    ok = True

    room = _make_room()
    lines = _format_room(room, "professional", "pune")
    text = "\n".join(lines)
    ok &= check("✅ VACANT NOW" in text, "vacant_now status shown")
    ok &= check("working professionals" in text, "professional wtype label shown")
    ok &= check("← fellow professionals" in text, "compatibility hint shown")
    ok &= check("Pune" in text, "city shown")
    ok &= check("← your city too" in text, "hometown match flagged")
    ok &= check("Settled" in text, "tenure >=6 → Settled label")

    room2 = _make_room(tenure=2)
    lines2 = _format_room(room2, "professional", "")
    text2 = "\n".join(lines2)
    ok &= check("Fresh room" in text2, "tenure <=3 → Fresh label")

    room3 = _make_room(kind="occupied", wtype="student", total=3, tenure=None, cities=[])
    lines3 = _format_room(room3, "professional", "")
    text3 = "\n".join(lines3)
    ok &= check("🔴 OCCUPIED" in text3, "occupied status shown")
    ok &= check("← fellow" not in text3, "no compat hint for mismatch")
    ok &= check("Hometowns" not in text3, "no city line when cities empty")

    # Completely vacant room (total_tenants=0)
    room4 = _make_room(kind="vacant_now", total=0, cities=[])
    room4["tenant_mix"]["total_tenants"] = 0
    lines4 = _format_room(room4, "", "")
    text4 = "\n".join(lines4)
    ok &= check("among the first" in text4, "vacant room → first-mover message")

    return ok


# ---------------------------------------------------------------------------
# C) fetch_room_availability — graceful failures
# ---------------------------------------------------------------------------

def test_graceful_failures():
    print("\n[C] fetch_room_availability graceful failures")
    ok = True

    from utils.properties import find_property

    # Unknown property
    find_property.return_value = None
    result = asyncio.run(fetch_room_availability("u1", "Unknown PG"))
    ok &= check("not found" in result.lower(), "unknown property → friendly error")

    # Property has no pg_id
    find_property.return_value = {"property_name": "Test PG", "property_id": ""}
    result = asyncio.run(fetch_room_availability("u1", "Test PG"))
    ok &= check("not available" in result.lower() or "id" in result.lower(),
                "missing pg_id → friendly error")

    return ok


# ---------------------------------------------------------------------------
# D) fetch_room_availability — happy path via mocked HTTP
# ---------------------------------------------------------------------------

def test_happy_path():
    print("\n[D] fetch_room_availability happy path")
    ok = True

    from utils.properties import find_property
    find_property.return_value = {
        "property_name": "Orchid Parc",
        "property_id": "abc123",
    }

    mock_response = {
        "status": 200,
        "data": {
            "abc123": [
                {
                    "room_name": "Room A",
                    "sharing_type": 2,
                    "rent_per_bed": 9500,
                    "availability_kind": "vacant_now",
                    "next_available_from": None,
                    "is_available": True,
                    "tenant_mix": {
                        "total_tenants": 1,
                        "gender": "all_male",
                        "working_type": "professional",
                        "avg_tenure_months": 10,
                        "top_origin_cities": ["Pune"],
                    },
                    "tags": ["AC"],
                }
            ]
        },
    }

    mock_resp = MagicMock()
    mock_resp.json.return_value = mock_response
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        result = asyncio.run(fetch_room_availability("u1", "Orchid Parc"))

    ok &= check("Orchid Parc" in result, "property name in output")
    ok &= check("VACANT NOW" in result, "availability status in output")
    ok &= check("professional" in result.lower(), "wtype in output")
    ok &= check("Pune" in result, "city in output")
    ok &= check("AC" in result, "tags in output")
    return ok


# ---------------------------------------------------------------------------
# E) save_preferences captures working_type + hometown
# ---------------------------------------------------------------------------

def test_preferences_fields():
    print("\n[E] save_preferences — working_type + hometown")
    ok = True

    from db.redis_store import save_preferences as _redis_save, get_preferences
    get_preferences.return_value = {}

    saved = {}

    def capture(uid, prefs):
        saved.update(prefs)

    _redis_save.side_effect = capture

    save_preferences("u1", location="Pune", working_type="professional", hometown="Nashik")
    ok &= check(saved.get("working_type") == "professional", "working_type saved")
    ok &= check(saved.get("hometown") == "Nashik", "hometown saved")

    saved.clear()
    save_preferences("u1", location="Mumbai", working_type="STUDENT")
    ok &= check(saved.get("working_type") == "student", "working_type lowercased")

    return ok


# ---------------------------------------------------------------------------
# F) skill_map includes fetch_room_availability in details
# ---------------------------------------------------------------------------

def test_skill_map():
    print("\n[F] skill_map — fetch_room_availability in details tools")
    ok = True
    import importlib
    sm = importlib.import_module("skills.skill_map")
    details_tools = sm.SKILL_TOOLS.get("details", [])
    ok &= check("fetch_room_availability" in details_tools,
                "fetch_room_availability in details skill tools")
    return ok


if __name__ == "__main__":
    results = [
        test_compat_hint(),
        test_format_room(),
        test_graceful_failures(),
        test_happy_path(),
        test_preferences_fields(),
        test_skill_map(),
    ]
    total = len(results)
    passed = sum(results)
    print(f"\n{'='*50}")
    print(f"Result: {passed}/{total} test groups passed")
    sys.exit(0 if passed == total else 1)
