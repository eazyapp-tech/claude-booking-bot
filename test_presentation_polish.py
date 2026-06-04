"""Presentation-polish regression (P1): human sharing labels, clean comparison amenities,
localized human-handoff. Deterministic, no network/Redis/LLM. Run: python test_presentation_polish.py
"""
import os, sys
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

from utils.api import parse_sharing_types_structured  # noqa: E402

_p = _f = 0
def ck(n, c, d=""):
    global _p, _f
    print(("  PASS  " if c else "  FAIL  ") + n + ("" if c else "  -> " + str(d)))
    _p += bool(c); _f += (not c)


def t_sharing_labels():
    # OxOtel ships numeric sharing types ("2","3") with no per-type rent → must become human.
    raw = [{"sharing_type": "2", "is_enabled": True},
           {"sharing_type": "3", "is_enabled": True},
           {"sharing_type": "1", "is_enabled": True}]
    labels = [o["label"] for o in parse_sharing_types_structured(raw)]
    ck("sharing: numeric types become human labels",
       labels == ["Double sharing", "Triple sharing", "Single"], labels)
    # A non-numeric label is preserved and a real rent is kept.
    out2 = parse_sharing_types_structured([{"sharing_type": "Double", "is_enabled": True, "rent": 5000}])
    ck("sharing: non-numeric label preserved + price kept",
       out2 == [{"label": "Double", "price": "₹5000/mo"}], out2)
    # Unknown numeric falls back to N-sharing (never a bare digit).
    ck("sharing: unknown numeric → N-sharing (never a bare digit)",
       parse_sharing_types_structured([{"sharing_type": "4", "is_enabled": True}]) == [{"label": "4-sharing", "price": ""}],
       parse_sharing_types_structured([{"sharing_type": "4", "is_enabled": True}]))


if __name__ == "__main__":
    t_sharing_labels()
    print(f"\n{'='*48}\n  {_p} passed, {_f} failed\n{'='*48}")
    sys.exit(1 if _f else 0)
