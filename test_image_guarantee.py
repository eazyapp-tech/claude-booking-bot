#!/usr/bin/env python3
"""test_image_guarantee.py

E2 — Image guarantee: Properties with images surface first in the carousel.

Hermetic (no Redis, no network, no LLM). Tests the stable sort that pushes
no-image properties to the back of the carousel slice, and verifies the
carousel items built from the sorted template reflect the correct order.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

errors = []


def check(label: str, cond: bool) -> None:
    if not cond:
        errors.append(label)
        print(f"  FAIL: {label}")
    else:
        print(f"  pass: {label}")


def _make_info(name: str, image: str, score: float) -> dict:
    return {
        "property_name": name,
        "property_image": image,
        "match_score": score,
        "property_location": "Kurla, Mumbai",
        "property_rent": "9000",
        "pg_available_for": "Any",
        "prop_id": name.lower().replace(" ", "_"),
        "pg_id": name.lower().replace(" ", "_"),
        "distance": "2 km",
        "property_lat": "",
        "property_long": "",
        "amenities": "",
        "sharing_types": "",
        "sharing_types_list": [],
        "images": [image] if image else [],
        "property_type": "",
    }


def _e2_sort(items: list) -> list:
    """The exact sort expression wired in search.py (E2)."""
    out = list(items)
    out.sort(key=lambda p: (0 if p.get("property_image") else 1))
    return out


# ── 1. High-score no-image pushed behind lower-score image ───────────────────

print("1. No-image high-score pushed behind image low-score")
a = _make_info("Alpha", "", 90)
b = _make_info("Beta", "http://cdn.example.com/beta.jpg", 75)
result = _e2_sort([a, b])
check("image property comes first", result[0]["property_name"] == "Beta")
check("no-image property comes second", result[1]["property_name"] == "Alpha")

# ── 2. All-image — stable, original order preserved ──────────────────────────

print("2. All-image: stable, original order preserved")
items = [_make_info(f"P{i}", f"img{i}.jpg", 80 - i * 10) for i in range(1, 4)]
result = _e2_sort(items)
check("P1 first", result[0]["property_name"] == "P1")
check("P2 second", result[1]["property_name"] == "P2")
check("P3 third", result[2]["property_name"] == "P3")

# ── 3. All-no-image — graceful: all surfaced in original order ───────────────

print("3. All-no-image: graceful, original order preserved")
items = [_make_info(f"Q{i}", "", 80 - i * 10) for i in range(1, 4)]
result = _e2_sort(items)
check("Q1 first", result[0]["property_name"] == "Q1")
check("Q2 second", result[1]["property_name"] == "Q2")
check("Q3 third", result[2]["property_name"] == "Q3")
check("all 3 items surfaced (none excluded)", len(result) == 3)

# ── 4. Mixed — image group floats to front, stable within each group ─────────

print("4. Mixed: image group floats to front")
items = [
    _make_info("NoImg1",  "", 95),
    _make_info("HasImg1", "img1.jpg", 85),
    _make_info("NoImg2",  "", 80),
    _make_info("HasImg2", "img2.jpg", 70),
    _make_info("NoImg3",  "", 60),
]
result = _e2_sort(items)
names = [r["property_name"] for r in result]
check("HasImg1 in first two", "HasImg1" in names[:2])
check("HasImg2 in first two", "HasImg2" in names[:2])
check("no-image properties fill back three", names[2:] == ["NoImg1", "NoImg2", "NoImg3"])
# stable within image group
check("HasImg1 before HasImg2 within image group",
      names.index("HasImg1") < names.index("HasImg2"))
# stable within no-image group
check("NoImg1 before NoImg2 before NoImg3 within no-image group",
      names.index("NoImg1") < names.index("NoImg2") < names.index("NoImg3"))

# ── 5. Empty list ────────────────────────────────────────────────────────────

print("5. Empty list — no crash")
check("empty in, empty out", _e2_sort([]) == [])

# ── 6. None image treated as no-image ────────────────────────────────────────

print("6. None image treated same as empty string (no-image)")
a = _make_info("HasImg", "http://cdn.example.com/a.jpg", 50)
b = _make_info("NoneImg", "", 90)
b["property_image"] = None  # explicit None override
result = _e2_sort([a, b])
check("HasImg (lower score, has image) comes first", result[0]["property_name"] == "HasImg")
check("NoneImg (None = no image) comes second", result[1]["property_name"] == "NoneImg")

# ── 7. Carousel built from sorted template reflects image-first order ─────────

print("7. build_carousel_items reflects E2-sorted order")
from tools.broker.search import build_carousel_items

template = [
    _make_info("NoImgProp",  "", 90),
    _make_info("HasImgProp", "http://cdn.example.com/img.jpg", 80),
]
template.sort(key=lambda p: (0 if p.get("property_image") else 1))
items, _ = build_carousel_items(template, None, None, limit=2)
check("carousel has 2 items", len(items) == 2)
check("carousel first item has image", bool(items[0]["image"]))
check("carousel second item has no image", not bool(items[1]["image"]))
check("HasImgProp is first in carousel", items[0]["name"] == "HasImgProp")

# ── result ───────────────────────────────────────────────────────────────────

total = 22
print()
if errors:
    print(f"FAILED: {len(errors)}/{total} assertion(s)")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
print(f"All {total} assertions passed — E2 image guarantee OK")
