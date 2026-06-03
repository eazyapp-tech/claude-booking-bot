"""
test_listing_leak.py — standalone regression (no Redis/network/LLM).

Proves the broker-listing-leak fix in core/message_parser.py:
  1. A drifted numbered format ("1. **Name**" — number OUTSIDE the bold, the
     real Haiku drift captured in prod) is still extracted into a
     property_carousel, with its "Image: <url>" line consumed into the card
     (never left as raw text).
  2. When NO detector matches (truly unparseable listing), raw media/CDN URLs
     are stripped from the fallback text so a user can NEVER see a raw
     azureedge/blob .mp4/.jpg URL in chat.
  3. The strict "**N. Name**" format still parses (regression guard).
  4. Legitimate non-media URLs in plain prose are preserved (no over-strip).

Run: python test_listing_leak.py   (exit 0 = pass)
"""
import os, re, sys
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

import core.message_parser as mp
# Stub the Redis-backed enrichers so the parser is pure/offline.
mp.get_property_info_map = lambda uid: []
mp.get_preferences = lambda uid: {}

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

def _types(parts):
    return [p.get("type") for p in parts]

def _carousel(parts):
    return next((p for p in parts if p.get("type") == "property_carousel"), None)

def _text_blob(parts):
    return "\n".join(p.get("markdown", "") for p in parts if p.get("type") == "text")

MEDIA = re.compile(r"azureedge\.net|blob\.core\.windows\.net|rentok-?storage", re.I)


def section_drift_extracts_carousel():
    md = (
        "Here's what's live in Andheri and nearby 🏠\n"
        "1. **WOODSTOCK ANDHERI BOY'S**\n"
        "📍 Andheri, Mumbai · ₹10,000/mo · Boys Only\n"
        "Image: https://rentok-storage-cdn.azureedge.net/due-type-images/x/123.mp4\n"
        "2. **JYOTI SPARKLE 02 ANDHERI GIRL'S**\n"
        "📍 Andheri East, Mumbai · ₹9,000/mo · Any\n"
        "Image: https://rentok-storage-cdn.azureedge.net/room-images/y.jpg\n"
    )
    parts = mp.parse_message_parts(md, "u1")
    car = _carousel(parts)
    check("drift: carousel extracted", car is not None, f"types={_types(parts)}")
    check("drift: >=2 properties", bool(car) and len(car.get("properties", [])) >= 2,
          f"n={len(car.get('properties', [])) if car else 0}")
    if car and car.get("properties"):
        check("drift: name parsed", car["properties"][0].get("name", "").startswith("WOODSTOCK"),
              repr(car["properties"][0].get("name")))
        check("drift: Image: url consumed into card", car["properties"][0].get("image", "").startswith("http"),
              repr(car["properties"][0].get("image")))
    blob = _text_blob(parts)
    check("drift: no raw media URL leaked in text", not MEDIA.search(blob), repr(blob[:120]))
    check("drift: no 'Image:' label leaked in text", "Image:" not in blob, repr(blob[:120]))


def section_unparseable_strips_urls():
    # No 📍 lines, no bold numbering → NO detector matches → fallback text part.
    md = (
        "Some options I found:\n"
        "WOODSTOCK ANDHERI — rent 10000\n"
        "Image: https://rentokstorage1753704434.blob.core.windows.net/room-images/1.mp4\n"
        "JYOTI SPARKLE — rent 9000\n"
        "Image: https://rentok-storage-cdn.azureedge.net/room-images/2.jpg\n"
    )
    parts = mp.parse_message_parts(md, "u2")
    blob = _text_blob(parts)
    check("unparseable: still returns a text part", any(p.get("type") == "text" for p in parts), _types(parts))
    check("unparseable: raw media URLs stripped", not MEDIA.search(blob), repr(blob))
    check("unparseable: no 'Image: http' leaked", "Image: http" not in blob, repr(blob))
    check("unparseable: descriptive text retained", "WOODSTOCK" in blob, repr(blob))


def section_strict_still_parses():
    md = (
        "Here are 2 picks:\n"
        "**1. Green Heights Andheri**\n"
        "📍 Andheri East · ₹12,000/mo · Boys · ~3.5 km from Andheri\n"
        "**2. Urban Nest Lokhandwala**\n"
        "📍 Andheri West · ₹14,500/mo · Boys · ~1.8 km from Andheri\n"
    )
    parts = mp.parse_message_parts(md, "u3")
    car = _carousel(parts)
    check("strict: carousel extracted (regression)", car is not None, f"types={_types(parts)}")
    check("strict: >=2 properties", bool(car) and len(car.get("properties", [])) >= 2,
          f"n={len(car.get('properties', [])) if car else 0}")


def section_legit_url_preserved():
    md = "Sure — you can read reviews at https://example.com/reviews before deciding. Want me to book a visit?"
    parts = mp.parse_message_parts(md, "u4")
    blob = _text_blob(parts)
    check("legit: non-media URL preserved", "example.com/reviews" in blob, repr(blob))


if __name__ == "__main__":
    section_drift_extracts_carousel()
    section_unparseable_strips_urls()
    section_strict_still_parses()
    section_legit_url_preserved()
    print("=" * 48)
    print(f"  {_passed} passed, {_failed} failed")
    print("=" * 48)
    sys.exit(1 if _failed else 0)
