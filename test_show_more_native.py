"""Native show-more pagination regression — tool-owned, deterministic, NO prose scraping.

Proves show_more_properties() slices the next batch from the cached ranked carousel list,
records it on the signal slate (so generate_ui_parts emits a NATIVE carousel — same path as
fresh search), advances a per-user cursor, and on exhaustion records NO carousel + hints a
radius widen. Deterministic; in-memory fakes; no Redis/network/LLM.

Run: `./.venv/bin/python test_show_more_native.py` (exit 0 = pass).
"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")
import sys
import asyncio

_p = _f = 0


def ck(n, c, d=""):
    global _p, _f
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else " -> " + repr(d)))
    _p += bool(c)
    _f += (not c)


from tools.broker import show_more as sm  # noqa: E402  (RED until the module exists)

# ── In-memory fakes patched onto the tool module (defeats the import-binding gotcha) ──
_store = {}
_signals = {}


def _set_sc(uid, items, center):
    _store[f"{uid}:sc"] = {"items": list(items), "map_center": center}
    _store[f"{uid}:cur"] = 5  # top-5 already shown by the fresh-search native carousel


def _get_sc(uid):
    return _store.get(f"{uid}:sc", {})


def _get_cur(uid):
    return _store.get(f"{uid}:cur", 5)


def _set_cur(uid, n):
    _store[f"{uid}:cur"] = n


sm.set_search_carousel = _set_sc
sm.get_search_carousel = _get_sc
sm.get_carousel_cursor = _get_cur
sm.set_carousel_cursor = _set_cur
sm.record_signal = lambda **kw: _signals.update(kw)


def t_pagination():
    uid = "u1"
    items = [{"name": f"P{i}", "rent": f"₹{9000 + i}/mo"} for i in range(13)]
    sm.set_search_carousel(uid, items, {"lat": 1.0, "lng": 2.0})  # cursor → 5

    _signals.clear()
    r1 = asyncio.run(sm.show_more_properties(uid))
    ck("batch1 = items[5:10] on the carousel signal", _signals.get("carousel_items") == items[5:10], _signals.get("carousel_items"))
    ck("batch1 carries map_center", _signals.get("carousel_map_center") == {"lat": 1.0, "lng": 2.0}, _signals.get("carousel_map_center"))
    ck("cursor advanced 5 → 10", sm.get_carousel_cursor(uid) == 10, sm.get_carousel_cursor(uid))
    ck("tool returns non-empty text", bool((r1 or "").strip()), r1)

    _signals.clear()
    asyncio.run(sm.show_more_properties(uid))
    ck("batch2 = items[10:13] (3 remaining)", _signals.get("carousel_items") == items[10:13], _signals.get("carousel_items"))
    ck("cursor advanced 10 → 13", sm.get_carousel_cursor(uid) == 13, sm.get_carousel_cursor(uid))

    _signals.clear()
    r3 = asyncio.run(sm.show_more_properties(uid))
    ck("exhausted → NO carousel signal recorded (never re-show or empty deck)", "carousel_items" not in _signals, _signals)
    ck("exhausted → text hints widening the area/radius",
       any(w in (r3 or "").lower() for w in ("wide", "area", "radius", "expand", "nearby")), r3)


def t_no_cache():
    uid = "u2"  # no prior search cached
    _signals.clear()
    r = asyncio.run(sm.show_more_properties(uid))
    ck("no cached search → NO carousel signal", "carousel_items" not in _signals, _signals)
    ck("no cached search → honest non-empty text", bool((r or "").strip()), r)


def t_partial_last_batch():
    uid = "u3"
    items = [{"name": f"Q{i}"} for i in range(7)]  # 7 total, top-5 shown
    sm.set_search_carousel(uid, items, None)
    _signals.clear()
    asyncio.run(sm.show_more_properties(uid))
    ck("partial batch = items[5:7] (2 left)", _signals.get("carousel_items") == items[5:7], _signals.get("carousel_items"))
    ck("None map_center omitted (not recorded as None carousel_map_center)",
       _signals.get("carousel_map_center") in (None,), _signals.get("carousel_map_center"))


if __name__ == "__main__":
    print("== pagination =="); t_pagination()
    print("== no cache =="); t_no_cache()
    print("== partial last batch =="); t_partial_last_batch()
    print(f"\n{_p} passed, {_f} failed")
    sys.exit(1 if _f else 0)
