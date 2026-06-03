"""Channel-capability adapter. Downgrades native contract units per the §5 matrix.

Invariant: text always survives; rich components are accelerators, never load-bearing.
Web supports everything (passthrough). WhatsApp degrades to list/buttons/text.
Plain text flattens everything to text units.
"""
from __future__ import annotations

from typing import Any

from core.contract import make_unit

CHANNELS = ("web", "whatsapp", "plain")

# Per-channel capability profile: which kinds render natively.
_CAPS: dict[str, set[str]] = {
    "web": {
        "text", "carousel", "choice_list", "quick_replies", "action_buttons",
        "comparison", "map", "confirmation", "status_rail", "input_request",
    },
    "whatsapp": {
        # WhatsApp renders text, list messages (choice_list), reply buttons
        # (quick_replies <=3 / action_buttons <=3), and native input asks.
        # NOTE: "carousel" is intentionally NOT in this set. Valid carousels are
        # handled by the explicit listing/visit/media branches in _adapt_whatsapp
        # FIRST; leaving "carousel" here would let a MALFORMED carousel (missing/
        # unknown payload) fall through as a raw, unrenderable carousel instead of
        # degrading to text — violating the "text or list, never a raw carousel"
        # guarantee. Omitting it routes malformed carousels to the text fallback.
        "text", "choice_list", "quick_replies", "action_buttons", "input_request",
    },
    "plain": {"text"},
}

_WA_LIST_MAX = 10
_WA_BUTTON_MAX = 3


def to_plain_text(unit: dict[str, Any]) -> str:
    """Render any unit as plain text (the universal fallback)."""
    d = unit.get("data", {})
    kind = unit.get("kind")

    if kind == "text":
        return d.get("text", "")

    if kind == "carousel":
        items = d.get("items", [])
        if d.get("payload") == "media":
            return "\n".join(it.get("url", "") for it in items if it.get("url"))
        lines = []
        for i, it in enumerate(items, 1):
            name = it.get("name") or it.get("title") or f"Option {i}"
            price = f" — {it['price']}" if it.get("price") else ""
            lines.append(f"{i}. {name}{price}")
        return "\n".join(lines)

    if kind == "choice_list":
        opts = d.get("options", [])
        return "\n".join(f"{i}. {o.get('label', '')}" for i, o in enumerate(opts, 1))

    if kind == "quick_replies":
        reps = d.get("chips", [])
        return "Reply with: " + " / ".join(str(r) for r in reps) if reps else ""

    if kind == "action_buttons":
        btns = d.get("buttons", d.get("actions", []))
        parts = []
        for b in btns:
            label = b.get("label", "") if isinstance(b, dict) else str(b)
            url = b.get("url", "") if isinstance(b, dict) else ""
            parts.append(f"{label}: {url}" if url else label)
        return "\n".join(parts)

    if kind == "comparison":
        rows = d.get("rows", [])
        names = d.get("names", [])
        head = " | ".join(names)
        body = "\n".join(" | ".join(str(c) for c in r) for r in rows)
        return f"{head}\n{body}".strip()

    if kind == "map":
        pins = d.get("pins", [])
        if pins:
            p = pins[0]
            link = f"https://www.google.com/maps/search/?api=1&query={p.get('lat')},{p.get('lng')}"
            label = p.get("label", "")
            return f"{label}\n{link}".strip()
        return "https://www.google.com/maps"

    if kind == "confirmation":
        d_ok = "\n".join(f"✓ {x}" for x in d.get("ok", []))
        d_warn = "\n".join(f"⚠ {x}" for x in d.get("warn", []))
        return "\n".join(x for x in [d.get("title", ""), d_ok, d_warn, d.get("body", "")] if x)

    if kind == "status_rail":
        prefix = {"err": "⚠ ", "warn": "ℹ ", "ok": "✓ "}.get(d.get("variant"), "")
        line = f"{prefix}{d.get('title', '')}"
        if d.get("body"):
            line += f"\n{d['body']}"
        return line

    if kind == "input_request":
        return d.get("prompt", "")

    return d.get("text", "")


def _as_text_unit(unit: dict[str, Any]) -> dict[str, Any]:
    # Preserve error/empty state so the mood + visibility survive degradation.
    state = unit.get("state", "result")
    if state not in ("error", "empty", "partial"):
        state = "result"
    return make_unit("text", state, {"text": to_plain_text(unit)})


def _adapt_whatsapp(unit: dict[str, Any]) -> list[dict[str, Any]]:
    kind = unit.get("kind")
    d = unit.get("data", {})

    if kind == "carousel" and d.get("payload") in ("listing", "visit"):
        # → list message (choice_list) capped at 10 rows.
        items = d.get("items", [])[:_WA_LIST_MAX]
        options = [
            {"id": it.get("id") or it.get("pg_id") or str(i),
             "label": it.get("name") or it.get("title") or f"Option {i+1}",
             "hint": it.get("price", "")}
            for i, it in enumerate(items)
        ]
        return [make_unit("choice_list", unit.get("state", "result"), {"options": options})]

    if kind == "carousel" and d.get("payload") == "media":
        # Media survives as-is (image/video messages handled by the send layer).
        return [unit]

    if kind == "quick_replies":
        reps = d.get("chips", [])[:_WA_BUTTON_MAX]
        return [make_unit("quick_replies", unit.get("state", "result"), {"chips": reps})]

    if kind == "action_buttons":
        btns = d.get("buttons", d.get("actions", []))[:_WA_BUTTON_MAX]
        return [make_unit("action_buttons", unit.get("state", "result"), {"buttons": btns})]

    if kind in ("comparison", "map", "confirmation", "status_rail"):
        # Not natively renderable on WA → prefixed/receipt text (error stays visible).
        return [_as_text_unit(unit)]

    if kind in _CAPS["whatsapp"]:
        return [unit]

    return [_as_text_unit(unit)]


def adapt(units: list[dict[str, Any]], channel: str) -> list[dict[str, Any]]:
    if channel not in CHANNELS:
        channel = "plain"

    if channel == "web":
        return list(units)

    out: list[dict[str, Any]] = []
    for u in units:
        if channel == "plain":
            out.append(_as_text_unit(u))
        else:  # whatsapp
            out.extend(_adapt_whatsapp(u))
    return out
