"""LAT-1: image enrichment + geocoding run CONCURRENTLY, not back-to-back.

Both operate on the same top-N search results but touch disjoint keys
(p_image/_images vs lat/lng), so they are independent I/O and should overlap.
Running them sequentially wastes ~the slower of the two on every search.

The assertion is a deterministic concurrency proof (no timing/flakiness):
geocoding is wired to finish the instant image-fetch is *in flight*. If the two
run concurrently (asyncio.gather), `geo_end` lands BEFORE the (slow) `img_end`.
If they run sequentially, image-fetch finishes entirely first, so `geo_end`
lands AFTER `img_end` — and the test fails. No Redis/network/LLM.

Run: `python test_lat1_concurrent.py` (exit 0 = pass).
"""
import asyncio
import sys

import tools.broker.search as search

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


def test_runs_both_concurrently_and_applies_effects():
    order = []
    img_in_flight = asyncio.Event()

    async def fake_images(props, limit=5):
        order.append("img_start")
        img_in_flight.set()
        await asyncio.sleep(0.02)  # image fetch is the slow one
        order.append("img_end")
        for p in props[:limit]:
            p["p_image"] = "http://img"

    async def fake_geo(props, limit=5):
        order.append("geo_start")
        # Proceeds the moment image-fetch is concurrently in flight.
        await asyncio.wait_for(img_in_flight.wait(), timeout=1.0)
        order.append("geo_end")
        for p in props[:limit]:
            p["lat"] = "19.0"

    orig_img = search._enrich_with_images
    orig_geo = search._geocode_properties
    try:
        search._enrich_with_images = fake_images
        search._geocode_properties = fake_geo

        props = [{}, {}]
        asyncio.run(search._enrich_top_results(props, limit=5))

        check("both effects applied (image)", props[0].get("p_image") == "http://img")
        check("both effects applied (geocode)", props[0].get("lat") == "19.0")
        check(
            "ran concurrently (geo_end before the slow img_end)",
            "geo_end" in order and "img_end" in order
            and order.index("geo_end") < order.index("img_end"),
        )
    finally:
        search._enrich_with_images = orig_img
        search._geocode_properties = orig_geo


if __name__ == "__main__":
    test_runs_both_concurrently_and_applies_effects()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(0 if _failed == 0 else 1)
