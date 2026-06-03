"""test_whatsapp_egress.py — WhatsApp egress maps degraded units to send dicts (Task 6).

Standalone script (matches the repo's gate convention: `check()`/`section_*()`
+ a `__main__` runner; no pytest dependency, runs under `python test_whatsapp_egress.py`).
Assertions mirror Task 6 verbatim (pytest cases converted to the harness).

`units_to_wa_messages` is a PURE function (runs units through adapt() then maps to
send-layer dicts). The network senders are NOT exercised here.

Run: `python test_whatsapp_egress.py`.
"""
import os
import sys

# channels.whatsapp imports config/db at module load; set the key defensively to
# match the established convention (empty-string env vars trip pydantic-settings).
os.environ["ANTHROPIC_API_KEY"] = os.environ.get("ANTHROPIC_API_KEY") or "test-key-not-used"

from core.contract import make_unit  # noqa: E402
from channels.whatsapp import units_to_wa_messages  # noqa: E402

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


def section_text_unit_becomes_one_text_message():
    msgs = units_to_wa_messages([make_unit("text", "result", {"text": "Hello"})])
    check("text_unit: becomes one text message",
          msgs == [{"type": "text", "text": "Hello"}], repr(msgs))


def section_listing_carousel_becomes_list_message_max_10():
    items = [{"name": f"PG {i}", "id": str(i)} for i in range(12)]
    units = [make_unit("carousel", "result", {"payload": "listing", "items": items})]
    msgs = units_to_wa_messages(units)
    lists = [m for m in msgs if m["type"] == "list"]
    check("listing_carousel: produces a list message", bool(lists), repr(msgs))
    if lists:
        check("listing_carousel: rows capped at 10",
              len(lists[0]["rows"]) <= 10, repr(lists[0]["rows"]))


def section_quick_replies_become_reply_buttons_max_3():
    units = [make_unit("quick_replies", "result", {"chips": ["a", "b", "c", "d"]})]
    msgs = units_to_wa_messages(units)
    btns = [m for m in msgs if m["type"] == "buttons"]
    check("quick_replies: produces a buttons message", bool(btns), repr(msgs))
    if btns:
        check("quick_replies: buttons capped at 3",
              len(btns[0]["buttons"]) <= 3, repr(btns[0]["buttons"]))


def section_map_becomes_text_with_deeplink():
    units = [make_unit("map", "result", {"pins": [{"lat": 19.0, "lng": 72.8, "label": "Maple"}]})]
    msgs = units_to_wa_messages(units)
    check("map: degrades to text with google maps deep-link",
          any(m["type"] == "text" and "google.com/maps" in m["text"] for m in msgs),
          repr(msgs))


def section_error_rail_stays_visible_as_text():
    units = [make_unit("status_rail", "error", {"variant": "err", "title": "Listings down", "retry": True})]
    msgs = units_to_wa_messages(units)
    check("error_rail: title survives as text",
          any(m["type"] == "text" and "Listings down" in m["text"] for m in msgs),
          repr(msgs))


def section_action_buttons_become_reply_buttons_max_3():
    units = [make_unit("action_buttons", "result", {"buttons": [
        {"label": "Book now"}, {"label": "Schedule visit"},
        {"label": "Call"}, {"label": "Cancel"}]})]
    msgs = units_to_wa_messages(units)
    btns = [m for m in msgs if m["type"] == "buttons"]
    check("action_buttons: produces a buttons message", bool(btns), repr(msgs))
    if btns:
        check("action_buttons: buttons capped at 3",
              len(btns[0]["buttons"]) <= 3, repr(btns[0]["buttons"]))
        check("action_buttons: first title from label is 'Book now'",
              btns[0]["buttons"][0]["title"] == "Book now", repr(btns[0]["buttons"]))


def section_media_carousel_becomes_one_media_per_item():
    units = [make_unit("carousel", "result", {"payload": "media", "items": [
        {"url": "https://x/1.jpg", "type": "image"},
        {"url": "https://x/2.jpg", "type": "image"},
        {"url": "https://x/3.mp4", "type": "video"}]})]
    msgs = units_to_wa_messages(units)
    media = [m for m in msgs if m["type"] == "media"]
    check("media_carousel: exactly 3 media messages",
          len(media) == 3, repr(media))
    check("media_carousel: first url preserved",
          bool(media) and media[0]["url"] == "https://x/1.jpg", repr(media))
    vids = [m for m in media if m["url"].endswith(".mp4")]
    check("media_carousel: video item carries media_type 'video'",
          bool(vids) and vids[0]["media_type"] == "video", repr(vids))


def section_interactive_messages_carry_a_meaningful_body():
    # D6: the interactive list/buttons must carry a real prompt body, not the generic
    # "Pick one" / blank default that send_units falls back to.
    ql = units_to_wa_messages([make_unit("quick_replies", "result", {"chips": ["Search PGs", "My visits"]})])
    btns = [m for m in ql if m["type"] == "buttons"]
    check("body: quick_replies buttons message carries a non-blank body",
          bool(btns) and bool((btns[0].get("body") or "").strip()), repr(btns))
    check("body: quick_replies body is not the generic 'Pick one'",
          bool(btns) and btns[0].get("body") != "Pick one", repr(btns))
    cl = units_to_wa_messages([make_unit("choice_list", "result",
                                         {"prompt": "Which area?", "options": [{"label": "Kurla"}, {"label": "Powai"}]})])
    lists = [m for m in cl if m["type"] == "list"]
    check("body: choice_list list message uses its prompt as the body",
          bool(lists) and lists[0].get("body") == "Which area?", repr(lists))


def section_filter_interactive_keeps_only_tappable_kinds():
    # D6: the drain path already sends the body text + property carousel + images, so only
    # the interactive supplements WA lacks may be forwarded — everything else would double-send.
    from channels.whatsapp import filter_interactive
    units = [
        make_unit("text", "result", {"text": "Here are some options"}),
        make_unit("status_rail", "warn", {"variant": "warn", "title": "Heads up", "body": "x"}),
        make_unit("carousel", "result", {"payload": "media", "items": [{"url": "a.jpg"}]}),
        make_unit("comparison", "result", {"items": [{"name": "A"}, {"name": "B"}]}),
        make_unit("quick_replies", "result", {"chips": ["a", "b"]}),
        make_unit("action_buttons", "result", {"buttons": [{"label": "Book"}]}),
        make_unit("choice_list", "result", {"options": [{"label": "Kurla"}]}),
    ]
    kept = filter_interactive(units)
    kinds = sorted(u["kind"] for u in kept)
    check("filter: keeps exactly the 3 interactive kinds",
          kinds == ["action_buttons", "choice_list", "quick_replies"], repr(kinds))
    check("filter: drops text/status_rail/carousel/comparison (already sent → no double-send)",
          not any(u["kind"] in ("text", "status_rail", "carousel", "comparison") for u in kept), repr(kept))
    check("filter: empty input → empty (honesty-branch turns forward nothing)",
          filter_interactive([]) == [], "non-empty")


def main():
    section_text_unit_becomes_one_text_message()
    section_listing_carousel_becomes_list_message_max_10()
    section_interactive_messages_carry_a_meaningful_body()
    section_filter_interactive_keeps_only_tappable_kinds()
    section_quick_replies_become_reply_buttons_max_3()
    section_action_buttons_become_reply_buttons_max_3()
    section_media_carousel_becomes_one_media_per_item()
    section_map_becomes_text_with_deeplink()
    section_error_rail_stays_visible_as_text()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
