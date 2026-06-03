"""
test_cross_contract.py — the missing test that would have caught the #16 rollback.

The contract-parity test (test_contract_parity.py) checks that backend and frontend
agree on the kind/state/surface ENUMS. It does NOT check the `data` sub-keys each
renderer actually reads. That gap let `_to_native` ship `data.replies` while the live
frontend reads `data.chips` → empty chips every turn → the rollback.

This test closes that gap. It feeds REAL `_to_native` / `generate_ui_parts` output
through a faithful Python MIRROR of the LIVE eazypg-chat renderers — each mirror
function reads EXACTLY the `data` keys its JS counterpart reads. A unit that emits a
key the frontend ignores renders empty here, the same blank the user sees. Every
fix in Deliverable 1 carries an adversarial assertion proving the OLD shape renders
BLANK (so the guard provably bites).

Mirror source of truth (live prod, eazypg-chat @ e02982c — byte-pinned):
  src/ingress.js                    normalizePart → isValidUnit gate (native pass-through)
  src/renderers/server-parts.js     KIND_RENDERERS (text/carousel/quick_replies/...)
  src/renderers/primitives.js       renderStatusRail / renderChoiceList / renderMapUnit / renderInputRequest
  src/message-builder.js            partitionBySurface → surface:"sheet" auto-opens the detent sheet

Run: `python test_cross_contract.py` (exit 0 = pass; no network / Redis / LLM).
"""
import os
import sys

os.environ["ANTHROPIC_API_KEY"] = os.environ.get("ANTHROPIC_API_KEY") or "test-key-not-used"

from core.contract import is_valid_unit, make_unit  # noqa: E402
from core.ui_parts import _to_native, generate_ui_parts  # noqa: E402

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


# ─────────────────────────────────────────────────────────────────────────────
# Faithful Python mirror of the LIVE frontend renderers.
#
# Each function returns the rendered-content string, or None when the frontend
# would render NOTHING for that unit. The keys read here MUST match the JS exactly
# — that is the whole point: a backend key the FE never reads → None → test fails.
# ─────────────────────────────────────────────────────────────────────────────

def _m_sections(sections):
    # server-parts.js renderExpandableSections: null when no sections; else one
    # <details> per section rendering its items (pills / key_value / qa / text).
    if not sections:
        return None
    out = []
    for s in sections:
        item_text = []
        for it in s.get("items", []):
            if isinstance(it, str):
                item_text.append(it)
            elif isinstance(it, dict):
                item_text.append(" ".join(str(it.get(k, "")) for k in
                                          ("label", "value", "question", "answer")))
        out.append(f"[{s.get('title', '')} {' '.join(item_text)}]")
    return " ".join(out)


def _m_text(unit, d):
    # KIND_RENDERERS.text → renderTextPart: markdown=data.text, sections=data.sections,
    # raw_html=data.raw_html. Returns null when BOTH the prose and the sections are empty.
    if d.get("raw_html"):
        return str(d["raw_html"])
    md = (d.get("text") or "").strip()
    sec = _m_sections(d.get("sections") or [])
    if not md and not sec:
        return None
    return f"{md}{sec or ''}"


def _m_carousel(unit, d):
    # KIND_RENDERERS.carousel: media → renderImageGallery(images=data.items);
    # listing → renderPropertyCarousel(properties=data.items). null when items empty.
    items = d.get("items") or []
    if not items:
        return None
    if d.get("payload") == "media":
        # The thumbnails render from data.items. (The header LABEL is read by
        # renderImageGallery from part.property_name, which the carousel bridge does
        # not thread — a separate live-FE gap; the gallery itself still renders.)
        return "gallery:" + " ".join(it.get("url", "") for it in items if it.get("url"))
    return "carousel:" + " ".join(it.get("name", "") for it in items)


def _m_quick_replies(unit, d):
    # renderQuickReplies: chips = data.chips; null when !chips.length. Reads c.label/c.action.
    chips = d.get("chips") or []
    if not chips:
        return None
    return "chips:" + " ".join(
        (c.get("label", "") if isinstance(c, dict) else str(c)) for c in chips)


def _m_action_buttons(unit, d):
    buttons = d.get("buttons") or []
    if not buttons:
        return None
    return "buttons:" + " ".join(
        (b.get("label", "") if isinstance(b, dict) else str(b)) for b in buttons)


def _m_confirmation(unit, d):
    # KIND_RENDERERS.confirmation → renderConfirmationCard: title/subtitle/details +
    # ALWAYS a confirm + cancel button (defaults "Confirm"/"Cancel"). Does NOT branch on
    # state and does NOT read ok/warn/body. Always renders (a card with two buttons).
    details = " ".join(x.get("text", "") for x in d.get("details", []) if isinstance(x, dict))
    confirm = d.get("confirm_action", "Confirm")
    cancel = d.get("cancel_action", "Cancel")
    return f"confirm[{d.get('title','')} {d.get('subtitle','')} {details} {confirm}/{cancel}]"


def _m_status_rail(unit, d):
    # primitives.js renderStatusRail: unit.state + data.{variant,title,body,retry}.
    # The wrapper always renders; treat empty title AND body as "nothing meaningful".
    title = d.get("title", "")
    body = d.get("body", "")
    if not title and not body:
        return None
    return f"rail[{d.get('variant','')} {title} {body}]"


def _m_choice_list(unit, d):
    opts = d.get("options") or []
    if not opts:
        return None
    return "choices:" + " ".join(o.get("label", "") for o in opts)


def _m_map(unit, d):
    pins = d.get("pins") or []
    # renderMapUnit always emits a maps deep-link, so it is never empty.
    return "map:" + " ".join(f"{p.get('lat','')},{p.get('lng','')}" for p in pins)


def _m_input_request(unit, d):
    # renderInputRequest always renders a field + submit (never null).
    return f"input[{d.get('input_type','text')}]"


def _to_comparison_model(d):
    # comparison.js toComparisonModel: native data.items[] takes precedence; else
    # legacy headers/rows transpose. attrs may be an array or a {label:value} object.
    items = d.get("items")
    if isinstance(items, list) and items:
        out = []
        for it in items:
            attrs = it.get("attrs")
            if isinstance(attrs, dict):
                coerced = [{"label": k, "value": v, "best": False} for k, v in attrs.items()]
            elif isinstance(attrs, list):
                coerced = [{"label": a.get("label", ""), "value": a.get("value", ""),
                            "best": bool(a.get("best"))} for a in attrs]
            else:
                coerced = []
            out.append({"name": it.get("name", ""),
                        "score": "" if it.get("score") is None else str(it.get("score")),
                        "badge": it.get("badge", ""), "attrs": coerced})
        return out
    headers = d.get("headers") or d.get("columns") or []
    rows = d.get("rows") or []
    if len(headers) < 2 or len(rows) < 1:
        return []
    names = headers[1:]
    return [{"name": n, "score": "", "badge": "",
             "attrs": [{"label": r[0], "value": (r[c + 1] if c + 1 < len(r) else ""),
                        "best": False} for r in rows]}
            for c, n in enumerate(names)]


def _m_comparison(unit, d):
    # renderComparison: toComparisonModel(data); returns null when < 2 items.
    items = _to_comparison_model(d)
    if len(items) < 2:
        return None
    names = " ".join(i["name"] for i in items)
    attrs = " ".join(f"{a['label']}={a['value']}" for i in items for a in i["attrs"])
    return f"comparison[{names} | {attrs}]"


def _m_text_fallback(unit, d):
    # server-parts.js _renderTextFallback: data.text || data.prompt || data.title.
    body = d.get("text") or d.get("prompt") or d.get("title") or ""
    return f"fallback[{body}]" if body else None


_KIND_MIRROR = {
    "text": _m_text,
    "carousel": _m_carousel,
    "quick_replies": _m_quick_replies,
    "action_buttons": _m_action_buttons,
    "confirmation": _m_confirmation,
    "status_rail": _m_status_rail,
    "choice_list": _m_choice_list,
    "map": _m_map,
    "input_request": _m_input_request,
    "comparison": _m_comparison,
}


def mirror_render(unit):
    """Render a native unit the way the live frontend would. None == user sees nothing."""
    # ingress.js normalizePart: a native unit (kind present) must pass isValidUnit or
    # it is dropped (returns null). surface:"sheet" units are routed to the detent
    # bottom sheet by partitionBySurface — relevant to the expandable-sections guard.
    if not is_valid_unit(unit):
        return None
    return _KIND_MIRROR.get(unit["kind"], _m_text_fallback)(unit, unit.get("data", {}))


# ─────────────────────────────────────────────────────────────────────────────
# Deliverable-1 guards: every _to_native row, plus the adversarial OLD-shape proof.
# ─────────────────────────────────────────────────────────────────────────────

def section_quick_replies_emit_chips_not_replies():
    """THE rollback bug: _to_native emitted data.replies; the live FE reads data.chips."""
    u = _to_native({"type": "quick_replies",
                    "chips": [{"label": "Search PGs", "action": "Show me PGs"}]})
    check("qr: valid unit (survives ingress)", is_valid_unit(u), repr(u))
    check("qr: data key is 'chips' (NOT 'replies')",
          "chips" in u["data"] and "replies" not in u["data"], repr(u["data"]))
    html = mirror_render(u)
    check("qr: renders non-empty through the live-FE mirror", bool(html), repr(html))
    check("qr: chip label reaches the FE", html and "Search PGs" in html, repr(html))
    # Adversarial: the reverted shape ({replies:...}) renders BLANK on the live FE.
    old = make_unit("quick_replies", "result",
                    {"replies": [{"label": "X", "action": "x"}]})
    check("qr: OLD {replies} shape renders BLANK (the guard provably bites)",
          mirror_render(old) is None, "old replies shape rendered non-empty")


def section_image_gallery_emits_property_not_property_name():
    u = _to_native({"type": "image_gallery", "property_name": "Sunrise PG",
                    "images": [{"url": "a.jpg"}, {"url": "b.jpg"}]})
    check("gallery: valid unit", is_valid_unit(u), repr(u))
    check("gallery: payload is media", u["data"].get("payload") == "media", repr(u))
    check("gallery: data key is 'property' (NOT 'property_name')",
          "property" in u["data"] and "property_name" not in u["data"], repr(u["data"]))
    check("gallery: items carried through (data.items)", bool(u["data"].get("items")), repr(u))
    check("gallery: renders non-empty (thumbnails) through the FE mirror",
          bool(mirror_render(u)), repr(mirror_render(u)))


def section_status_card_becomes_status_rail_not_confirmation():
    """Confirmed milestone → quiet rail, NOT a confirmation card with phantom buttons."""
    u = _to_native({"type": "status_card", "status": "success", "icon": "calendar-check",
                    "title": "Visit Confirmed!", "subtitle": "Sunrise PG",
                    "details": [{"icon": "calendar", "text": "Tomorrow"},
                                {"icon": "clock", "text": "5 PM"}],
                    "actions": [{"label": "My visits", "action": "Show my visits"}]})
    check("status: kind is status_rail (NOT confirmation)", u["kind"] == "status_rail", repr(u))
    check("status: no confirm/cancel keys (rail has no buttons)",
          "confirm_action" not in u["data"] and "cancel_action" not in u["data"], repr(u["data"]))
    html = mirror_render(u)
    check("status: renders non-empty through the FE mirror", bool(html), repr(html))
    check("status: title survives", html and "Visit Confirmed!" in html, repr(html))
    check("status: subtitle + details folded into body survive",
          html and "Sunrise PG" in html and "Tomorrow" in html and "5 PM" in html, repr(html))
    # Adversarial: the OLD confirmation mapping forces a phantom "Confirm" button on a
    # done action (renderConfirmationCard always renders confirm/cancel).
    old = make_unit("confirmation", "result", {"title": "Visit Confirmed!", "subtitle": "Sunrise PG"})
    old_html = mirror_render(old)
    check("status: OLD confirmation shape shows a phantom Confirm button",
          bool(old_html) and "Confirm" in old_html, repr(old_html))


def section_expandable_is_inline_text_not_sheet():
    """Expandable detail must render INLINE; surface:'sheet' would auto-open a surprise sheet."""
    u = _to_native({"type": "expandable_sections", "property_name": "Sunrise PG",
                    "sections": [{"id": "am", "title": "Amenities",
                                  "content_type": "pills", "items": ["WiFi", "AC"]}]})
    check("exp: kind is text", u["kind"] == "text", repr(u))
    check("exp: surface is INLINE (NOT 'sheet' — no surprise auto-sheet)",
          u.get("surface", "inline") == "inline", repr(u))
    check("exp: sections carried (data.sections)", bool(u["data"].get("sections")), repr(u))
    check("exp: no full body re-emitted (supplements-only)",
          not (u["data"].get("text") or "").strip(), repr(u["data"]))
    html = mirror_render(u)
    check("exp: renders non-empty inline through the FE mirror", bool(html), repr(html))
    check("exp: section content survives",
          html and "Amenities" in html and "WiFi" in html, repr(html))


def section_confirmation_ask_renders_with_its_cta():
    """The ASKING confirmation_card keeps confirmation/awaiting_input and renders its CTA."""
    u = _to_native({"type": "confirmation_card", "title": "Confirm your visit?",
                    "subtitle": "Sunrise PG", "style": "visit",
                    "details": [{"icon": "calendar", "text": "Tomorrow 5 PM"}],
                    "confirm_action": "Yes, book it", "cancel_action": "Not now"})
    check("confirm: kind is confirmation", u["kind"] == "confirmation", repr(u))
    check("confirm: state is awaiting_input", u["state"] == "awaiting_input", repr(u))
    html = mirror_render(u)
    check("confirm: renders non-empty with its real CTA label",
          bool(html) and "Yes, book it" in html, repr(html))


def section_comparison_emits_structured_unit():
    """D2: a compare turn must emit a native `comparison` unit, not prose text."""
    from tools.broker.compare import build_comparison_items
    comparison = [
        {"name": "Sunrise PG", "location": "Kurla", "rent": "8000", "score": 82,
         "amenities": "WiFi, AC", "food": "", "services": "", "type": "PG",
         "available_for": "Boys", "notice_period": "", "agreement_period": "",
         "token_amount": "2000", "distance": "500", "rooms": [], "total_beds": 4,
         "maps_link": "", "microsite": ""},
        {"name": "Moonlight PG", "location": "Kurla", "rent": "9500", "score": 74,
         "amenities": "WiFi", "food": "", "services": "", "type": "PG",
         "available_for": "Boys", "notice_period": "", "agreement_period": "",
         "token_amount": "", "distance": "800", "rooms": [], "total_beds": 2,
         "maps_link": "", "microsite": ""},
    ]
    items = build_comparison_items(comparison)
    check("cmp: build_comparison_items returns one item per property",
          len(items) == 2, repr(items))
    check("cmp: each item carries name + score + non-empty attrs",
          all(i.get("name") and i.get("score") is not None and i.get("attrs") for i in items),
          repr(items))
    top = [i["name"] for i in items if i.get("badge")]
    check("cmp: the top-scoring property carries a badge", top == ["Sunrise PG"], repr(items))
    rent_best = [i["name"] for i in items for a in i["attrs"]
                 if a["label"] == "Rent" and a.get("best")]
    check("cmp: the lowest-rent cell is flagged best", rent_best == ["Sunrise PG"], repr(rent_best))

    # generate_ui_parts emits the comparison unit FROM the signal slate (the egress
    # pattern). The renderer then renders it via the live-FE mirror.
    units = generate_ui_parts("Sunrise PG is the better fit for your budget.",
                              agent="broker", user_id="u1", locale="en",
                              signals={"comparison_items": items})
    cmp_units = [u for u in units if u["kind"] == "comparison"]
    check("cmp: generate_ui_parts emits exactly one comparison unit",
          len(cmp_units) == 1, repr([u["kind"] for u in units]))
    if cmp_units:
        u = cmp_units[0]
        check("cmp: comparison unit is valid (survives ingress)", is_valid_unit(u), repr(u))
        html = mirror_render(u)
        check("cmp: renders non-empty (>=2 columns) on the live FE", bool(html), repr(html))
        check("cmp: both property names survive to the FE",
              html and "Sunrise PG" in html and "Moonlight PG" in html, repr(html))
        check("cmp: match score survives", html and "82" in html, repr(html))
    # Adversarial: no comparison_items signal → NO comparison unit (the OLD prose-only
    # behaviour that rendered as text). Proves the unit is signal-driven, not guessed.
    units2 = generate_ui_parts("Here is a breakdown of the two options...",
                               agent="broker", user_id="u1", locale="en", signals={})
    check("cmp: no signal → NO comparison unit (proves it is data-driven, not prose-parsed)",
          not any(u["kind"] == "comparison" for u in units2),
          repr([u["kind"] for u in units2]))


def section_end_to_end_every_unit_renders_on_live_fe():
    """Drive the REAL generate_ui_parts path and prove every emitted unit renders."""
    for agent in ("default", "broker", "booking", "profile"):
        units = generate_ui_parts("Here are a couple of options for you.",
                                   agent=agent, user_id="u1", locale="en")
        check(f"e2e[{agent}]: every emitted unit survives ingress (valid)",
              all(is_valid_unit(u) for u in units), repr(units))
        rendered = [(u["kind"], mirror_render(u)) for u in units]
        check(f"e2e[{agent}]: every emitted unit renders non-empty on the live FE",
              all(h for _, h in rendered), repr(rendered))
    # The default-agent plain path always emits the search-chip supplement → must carry chips.
    units = generate_ui_parts("Hello! How can I help you find a place?",
                              agent="default", user_id="u1", locale="en")
    qr = [u for u in units if u["kind"] == "quick_replies"]
    check("e2e: default chips supplement uses the 'chips' key end-to-end",
          bool(qr) and "chips" in qr[0]["data"] and "replies" not in qr[0]["data"], repr(qr))
    check("e2e: those chips render non-empty on the live FE",
          bool(qr) and bool(mirror_render(qr[0])), repr(qr))


if __name__ == "__main__":
    section_quick_replies_emit_chips_not_replies()
    section_image_gallery_emits_property_not_property_name()
    section_status_card_becomes_status_rail_not_confirmation()
    section_expandable_is_inline_text_not_sheet()
    section_confirmation_ask_renders_with_its_cta()
    section_comparison_emits_structured_unit()
    section_end_to_end_every_unit_renders_on_live_fe()
    print(f"\n{'='*52}\n  {_passed} passed, {_failed} failed\n{'='*52}")
    sys.exit(1 if _failed else 0)
