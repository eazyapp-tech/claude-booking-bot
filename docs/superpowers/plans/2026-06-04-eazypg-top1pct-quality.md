# EazyPG Chat — Top-1% Quality Milestone — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans / subagent-driven-development. Steps use `- [ ]`. This plan is the SINGLE SOURCE OF TRUTH — update checkboxes + the Decision Log as you go. Each phase is independently shippable and MUST be verified (offline gate + live) before the next. **Hard rule: every change must be strictly better and break nothing that works in production today.**

**Goal:** Close the specific, evidence-backed gaps that keep EazyPG Chat below a top-1% bar — presentation polish, reload durability, one unified contract (retire prose-regex parsing), real-renderer tests, and one-command local bring-up — without regressing the live product (D1–D6, already shipped at main `bd1a4a2`).

**Architecture:** Backend (Python/FastAPI) emits native `{kind,state,data,surface}` units validated against `core/contract.json`; the live FE (vanilla-JS SPA `eazypg-chat`) renders them. The cohesion is proven (10/10 kinds 1:1). This milestone removes the two parallel representations (legacy `{type}` + native `{kind}`) and the regex prose-parser, hardens tests with the real renderers, and adds local dev infra.

**Tech Stack:** Python 3.11 (venv standalone test scripts, NOT pytest), Redis, Postgres, Anthropic; FE = Vite vanilla JS (sibling repo `eazypg-chat`); Node for real-renderer tests; docker-compose for local infra.

---

## Resume Protocol (run at the START of every session)

1. Read this plan top-to-bottom. Skim the Decision Log + Invariants.
2. Verify git state:
   ```bash
   cd "/Users/eazypg/CC Booking Bot FInal/claude-booking-bot"
   git branch --show-current && git log --oneline -5 && git status -s
   ```
3. Green baseline (the 22-suite gate + the 3 prod-grounded scripts in docs/superpowers/):
   ```bash
   export ANTHROPIC_API_KEY=test-key-not-used
   for t in test_cross_contract test_signals test_ui_parts_native test_channel_adapter \
     test_web_egress test_whatsapp_egress test_contract test_contract_parity; do
     ./.venv/bin/python "$t.py" >/tmp/g_$t 2>&1 && echo "PASS $t" || echo "FAIL $t"; done
   ```
4. Find the next unchecked phase. Do its **Grounding task FIRST**. Work test-first. Commit per task. Ship + verify live before starting the next phase.

---

## Invariants (do not violate)

- **Production is live and must keep working.** Main `bd1a4a2` is deployed (D1–D6). New FE is live. Verify every change against the live lockstep before declaring done.
- **The live FE is the contract authority** (eazypg-chat @ `fix/spacing-pass`, prod). Match the exact `data` keys each renderer reads.
- **One contract key per concept** (e.g. `quick_replies`→`data.chips`).
- **`generate_ui_parts` is supplements-only on web** — never re-emit the response body (double-render).
- **Tests are standalone scripts** — add every new test to `.github/workflows/ci.yml`.
- **Writes hit OxOtel's real CRM** — never finalize a booking/visit/reserve/payment in a live test.
- **Rollback ready** — each ship is a single revertable commit; deploy is CI-gated via `deploy-render.yml`.

---

## Status Tracker

- [ ] **P1 — Presentation polish (backend, low-risk, high visible value).** Human sharing labels ("2"→"Double sharing"), amenity-string hygiene in compare, i18n the human-handoff strings.
- [ ] **P2 — FE reload durability (frontend).** Persist `parts[]` in chat history so a reload restores rich cards/comparison instead of prose.
- [ ] **P3 — Real-renderer tests (infra; de-risks P4).** Run the ACTUAL FE renderers (node + jsdom or module import) against backend output, replacing the hand-maintained Python mirror as the authority. Grounding-first.
- [ ] **P4 — One contract end-to-end (the big one).** Emit the property carousel as a structured `carousel` unit from the search tool output; retire `message_parser`'s regex prose-parser and the legacy `{type}` parts. Grounding-first; gated behind P3.
- [ ] **P5 — Local bring-up (DX).** docker-compose (Redis+Postgres) + a mock-LLM mode so the full stack runs and smoke-tests in one command. Grounding-first.

---

## P1 — Presentation Polish

**Evidence (live):** the detail sheet's "CHOOSE SHARING" showed a button labeled bare `3`; the comparison amenity row showed `…water-filter 2` (stray token); the D5 handoff string is English-only in an en/hi/mr product.

**Files:**
- Modify: `utils/api.py` — `parse_sharing_types_structured` (map numeric labels → human).
- Modify: `tools/broker/compare.py` — amenity string hygiene (the comparison `amenities` cell).
- Modify: `core/ui_parts.py` — `make_human_handoff_part` (accept locale; localized title/body).
- Modify: `routers/chat.py` — pass `language` to `make_human_handoff_part`.
- Test: `test_presentation_polish.py` (new, standalone) + add to `ci.yml`.

### Task P1.1 — Human sharing labels

- [ ] **Step 1 — Failing test.** Add `test_presentation_polish.py`:
```python
import os; os.environ.setdefault("ANTHROPIC_API_KEY","test-key-not-used")
import sys
from utils.api import parse_sharing_types_structured
_p=_f=0
def ck(n,c,d=""):
    global _p,_f
    print(("  PASS " if c else "  FAIL ")+n+("" if c else " -> "+str(d))); _p+=c; _f+=(not c)
def t_labels():
    raw=[{"sharing_type":"2","is_enabled":True},{"sharing_type":"3","is_enabled":True},{"sharing_type":"1","is_enabled":True}]
    out=parse_sharing_types_structured(raw)
    labels=[o["label"] for o in out]
    ck("sharing: numeric types become human labels", labels==["Double sharing","Triple sharing","Single"], labels)
    raw2=[{"sharing_type":"Double","is_enabled":True,"rent":5000}]
    out2=parse_sharing_types_structured(raw2)
    ck("sharing: non-numeric label preserved + price kept", out2==[{"label":"Double","price":"₹5000/mo"}], out2)
    raw3=[{"sharing_type":"4","is_enabled":True}]
    ck("sharing: unknown numeric falls back to N-sharing", parse_sharing_types_structured(raw3)==[{"label":"4-sharing","price":""}], parse_sharing_types_structured(raw3))
if __name__=="__main__":
    t_labels(); print(f"\n{_p} passed, {_f} failed"); sys.exit(1 if _f else 0)
```
- [ ] **Step 2 — Run; verify FAIL** (`./.venv/bin/python test_presentation_polish.py` → label assert fails: gets "2"/"3").
- [ ] **Step 3 — Implement** in `utils/api.py:parse_sharing_types_structured`, after computing `stype`:
```python
        _SHARING = {"1": "Single", "2": "Double sharing", "3": "Triple sharing"}
        label = _SHARING.get(str(stype).strip()) or (f"{stype}-sharing" if str(stype).strip().isdigit() else str(stype))
        out.append({"label": label, "price": f"₹{rent}/mo" if rent else ""})
```
  (Replace the existing `out.append({"label": str(stype), ...})` line.)
- [ ] **Step 4 — Run; verify PASS.**
- [ ] **Step 5 — Commit** `feat(polish): human sharing labels in the detail sheet (P1.1)`.

### Task P1.2 — Comparison amenity hygiene — ❌ DROPPED (not a code defect)
- [x] **Grounding done.** `compare.py:137` reads `details["common_amenities"]`, which `_fetch_details_raw:77` already produces via `parse_amenities(...)`, joined with `", "`. The stray `water-filter 2` has a SPACE (not `, `) → it is a SINGLE literal amenity name in OxOtel's source data, not a join/parse artifact. Heuristic cleaning would risk mangling legitimate names ("AC 2 Ton", "24x7 Security"). Top-1% call: do NOT paper over a data-source quirk in the presentation layer — fix at the data source if desired. P1.2 dropped.

### Task P1.3 — i18n the human-handoff
- [ ] **Grounding:** read `core/ui_parts.py:make_human_handoff_part` + how `_LABELS`/locale is threaded in `generate_ui_parts`; confirm en/hi/mr label table location. Record in Decision Log.
- [ ] **TDD:** test that `make_human_handoff_part(brand, locale="hi")` yields Hindi title/body; en unchanged. Implement a small locale table; thread `language` from `routers/chat.py` both call sites. Commit `feat(polish): localize human-handoff identity en/hi/mr (P1.3)`.

### Ship P1
- [ ] Full offline gate green. Add `test_presentation_polish.py` to `ci.yml`. PR → CI green → merge → deploy → **live verify**: search → open a Details sheet → confirm "Double sharing"/"Triple sharing" (not "2"/"3"); compare → clean amenity row. Update tracker + Decision Log.

---

## P2 — FE Reload Durability

**Evidence (live):** after a page reload, restored history rendered property/comparison messages as **prose text** — the FE persists only message text, not `parts[]`, so rich units are lost on restore.

**Files (eazypg-chat repo):**
- Modify: `src/chat-history.js` — persist + restore `parts[]` per bot message.
- Modify: `src/message-builder.js` / `src/stream.js` — on restore, re-render via the unit renderer when `parts[]` exists; fall back to text otherwise.
- Test: `tests/*.spec.js` (vitest, if present) or a focused DOM test.

- [ ] **Grounding:** read `src/chat-history.js` (`saveChatHistory`/`loadChatHistory`, the stored shape), `src/message-builder.js` (`addBotMessage`, restore path), `src/stream.js` (how live parts reach `renderUnits`). Confirm where parts[] is dropped on save. Check the test runner (vitest present?). Record in Decision Log.
- [ ] **TDD + implement:** persist `parts` alongside each bot message; on `loadChatHistory`, when `parts` exist, render through the same `renderUnits`/`renderFromServerParts` path as live; else current text fallback. Cap stored size sanely. Add a test (restored message with a carousel unit → `.property-card` present). Commit. **Live verify:** search → reload → cards/comparison still render rich. Ship (separate eazypg-chat PR). Update tracker + Decision Log.

---

## P3 — Real-Renderer Tests (de-risks P4)

**Why first (before P4):** the Python FE-mirror in `test_cross_contract.py` is a hand-maintained port — exactly the fragility that makes the P4 contract refactor risky. Running the REAL renderers makes P4 safe to verify.

- [ ] **Grounding:** determine the cleanest way to execute the real FE renderers headlessly — (a) `node` importing `src/renderers/*` with a jsdom shim for `document`/`escapeHtml`, or (b) a vitest suite in `eazypg-chat` that feeds real backend-JSON fixtures through `renderUnits`. Inventory the renderers' DOM deps. Decide harness. Record in Decision Log.
- [ ] **TDD + implement:** a fixtures file = real backend output (captured from `core.ui_parts`/`make_unit` for every kind×state, via a small Python dumper) → a node/vitest test asserting each renders non-empty using the ACTUAL FE renderer. Wire into CI (both repos or a fixtures artifact). Keep the Python mirror as a fast pre-check but make the node renderer the authority. Commit. Update tracker + Decision Log.

---

## P4 — One Contract End-to-End (the big one)

**Why:** the property carousel is reconstructed by regex-scraping the broker's prose (`core/message_parser.py`), and the FE receives a mix of legacy `{type}` + native `{kind}`. Top-1% = one representation, structured from the tool output.

- [ ] **Grounding (deep):** trace the full carousel path — `tools/broker/search.py` (the data it already has), `core/message_parser.py` (`parse_message_parts` regex + `_build_carousel_parts`), `routers/chat.py` (parts assembly), `eazypg-chat/src/ingress.js` (`fromLegacy`) + `server-parts.js`. Decide: emit a native `carousel` unit (data.items[]) directly from a signal recorded in `search_properties` (same pattern as D2 comparison), so `parse_message_parts` no longer regex-builds it. Map every field the FE card + sheet read. Plan the legacy-removal in slices (carousel first; keep text). Record in Decision Log.
- [ ] **TDD + implement (sliced):** (1) record carousel items on the signal slate in `search_properties`; (2) emit native `carousel` from `generate_ui_parts`; (3) cross-contract + real-renderer (P3) assert parity with today's regex output; (4) remove the regex carousel builder once parity proven; (5) collapse the FE `fromLegacy` carousel branch if no longer needed. Each slice: test-first, gate, live-verify, commit. Ship lockstep. Update tracker + Decision Log.

---

## P5 — Local Bring-Up (DX)

- [ ] **Grounding:** inventory runtime deps (Redis, Postgres, pgvector optional, env vars from `config.py`). Decide docker-compose services + a `MOCK_LLM` mode (env flag → `core/claude` returns canned tool-call/text so the pipeline runs offline). Record in Decision Log.
- [ ] **TDD + implement:** `docker-compose.yml` (redis + postgres), a `.env.example`, a `MOCK_LLM` path in `core/claude` (guarded, never in prod), and a `make smoke` that boots + runs a read-only search assertion. Commit. Update tracker + Decision Log.

---

## Verification & Ship (per phase)

- Offline gate (the 22 suites) green before + after.
- Live lockstep: prod `/health` 200 + a read-only browser/API smoke of the changed surface.
- Keep `git revert <merge>` ready.

---

## Decision Log (append; newest last)

- **2026-06-04 — Milestone created.** Off the back of the D1–D6 ship (`bd1a4a2`) + the honest top-1% gap assessment. Sequenced low-risk→high-risk: P1 polish, P2 reload, then infra-first (P3 real-renderer tests) BEFORE the P4 contract refactor (the mirror's fragility is why P4 is risky), then P5 DX. Grounded P1 on real prod data: OxOtel `sharing` = `[{"label":"2"|"3","price":""}]` (numeric, no per-type rent) → the "3" bug; carousel `amenities` often empty (sheet degrades); images 13–20/property (gallery solid); the stray "water-filter 2" is in the compare path, not the carousel.

- **2026-06-04 — P4 GROUNDING COMPLETE (deep trace; no code written yet).** Traced the full carousel path end-to-end. Findings:
  - **Signal mechanism** (`core/signals.py`): per-turn `ContextVar` slate; `reset_signals()` at turn start (pipeline.py:40 / chat.py:236), `record_signal(**kw)` mutates in place, `current_signals()` read at egress. **D2 precedent to copy exactly:** `compare.py:206 record_signal(comparison_items=build_comparison_items(...))` → `ui_parts.py:974 signals.get("comparison_items")` → `parts.append(make_unit("comparison","result",{"items":cmp_items}))`. The carousel parallel is 1:1.
  - **FE pass-through proven:** `eazypg-chat/src/ingress.js:normalizePart` — any part with a string `kind` is passed through (validated by `isValidUnit`), only `fromLegacy()` converts old `{type}` parts. The legacy `property_carousel` maps to **native `{kind:"carousel", state:"result", data:{payload:"listing", items, map_center}, surface:"inline"}`** (ingress.js `case "property_carousel"`). `server-parts.js:336` native `carousel` renderer delegates `payload:"listing"` → `renderPropertyCarousel({properties:data.items, map_center})`, which builds the lean card view-model (`rent→price`) AND stashes the **full original item** for `composePropertySheet`. ⇒ **Emitting the native unit from the backend renders byte-identically to today; ZERO FE change required.** `make_unit(kind,state,data,surface="inline")` confirmed in `core/contract.py:31`.
  - **WhatsApp is fully decoupled** — `routers/webhooks.py` never calls `parse_message_parts`; its carousel is `property_template` (the `info` dicts) → `channels/whatsapp.py:send_carousel` (Meta carousel templates, reads `property_name/property_location/property_rent/pg_available_for/prop_id`). ⇒ **Retiring the web regex carousel touches ONLY the web channel.** The brief's "keep send_carousel working" invariant is automatically satisfied.
  - **The `info` dict built in `search.py` (the `set_property_info_map` payload) is a strict SUPERSET of today's regex-scraped carousel item.** Native emission is therefore strictly richer than the regex→Redis re-derivation (which itself just looks the same `info` back up via `_find_in_info_map`).

  **AUTHORITATIVE FIELD MAP — native carousel `item` (byte-identical to `message_parser._build_carousel_parts` output; every field a consumer reads):**

  | item key | type | source in `search.py` `info`/locals | consumer(s) | format note |
  |---|---|---|---|---|
  | `name` | str | `info["property_name"]` | card name, sheet title, map pin | exact spelling (never modify) |
  | `location` | str | `info["property_location"]` (address) | card loc, sheet area | |
  | `rent` | str | `info["property_rent"]` (raw e.g. `"9000"`) | card→`price` (strips `/mo`), sheet `price`/`rent` | **MUST format `f"₹{int(rent):,}/mo"`** — today's value is prose-derived (`"₹9,000/mo"`); raw number would visibly regress. Non-numeric → pass through. |
  | `gender` | str | `info["pg_available_for"]` | card gender pill | |
  | `distance` | str | `info["distance"]` | (parity only; card doesn't render) | keep for parity |
  | `image` | str | `info["property_image"]` | card cover, sheet single-image fallback | |
  | `link` | str | `info["property_link"]` (microsite) | (parity only) | keep for parity |
  | `lat` | str | `info["property_lat"]` | carousel map, sheet map | |
  | `lng` | str | `info["property_long"]` | carousel map, sheet map | |
  | `score` | str | `round(float(info["match_score"]))` | card score badge (`%`) | int-string; today rounds the float `_custom_score` |
  | `amenities` | str | `info["amenities"]` (comma-joined) | card pills, sheet amenity list | |
  | `images` | array | `info["images"]` (full gallery) | sheet gallery | `_sheet_enrichment` key — sheet degrades gracefully if absent |
  | `sharing` | array`[{label,price}]` | `info["sharing_types_list"]` | sheet "Choose sharing" | `_sheet_enrichment` key — sheet degrades gracefully if absent |

  `map_center` = `{"lat":float(lat),"lng":float(lng)}` from the geocoded search center already in `search_properties` locals (`lat,lng`), else average of item coords (mirrors legacy). **N = top-5** by score (results already sorted desc; mirrors "show 5 at a time" + `_enrich_with_images(limit=5)`).

  - **SCOPE DECISION (made 2026-06-04, user-delegated to expert call) — "structured SUPERSEDES scraped" (refined Option C).** Today the carousel content is driven by the **broker's prose**, which is turn-stateful: `search_properties` returns up to **20** in one call (no offset; only `radius_flag`), and `search.md:30` instructs "show 5 at a time, then 6-10" — so on plain **"show more / next batch"** the broker paginates props 6-10 **from context WITHOUT re-calling search** → no fresh signal that turn (slate reset each turn). Considered A (tool owns pagination: +offset/cursor param, +2nd Haiku-reliability surface — rejected, biggest diff/risk for a tail flow), B (name-anchored cache emission: still reads prose names + must keep the regex as 0-match fallback ⇒ *two* pagination paths, worse code — rejected), and deck-widening (emit top-8 + FE swipe, delete regex outright — a genuine UX win but a **visible behavior change**; entangling it with a contract refactor is the hard-to-revert big-bang the brief warns against → **deferred to P4b as its own ticket with its own live validation**).
    **DECISION = the disciplined increment: P4 changes the *representation*, not the *behavior*.** Rule: *on any turn where a search ran, the structured carousel (from `signals["carousel_items"]`) SUPERSEDES the scraped one.* Mechanics: `search.py` records the items on the signal; `generate_ui_parts` emits the native `carousel/listing` unit (D2-consistent); `chat.py` strips the parser's `property_carousel` part when the signal is present (one carousel/turn, deterministic). The legacy `_build_carousel_parts` regex is **retained** as the sole source only for same-search "show more" (no fresh signal) — isolated to that tail, teed up for deletion in **P4b**.
    **Why this is the top-1% call:** (1) **correctness no longer depends on the prompt** — broker may keep writing prose; we just discard the scraped carousel in favour of the structured one ⇒ the "intermittent double-render if Haiku misbehaves" risk is *designed out*, deterministically; the prompt nudge becomes a pure quality/cost optimization, not load-bearing. (2) **Fresh-search carousels get strictly richer/reliable** — real `amenities`/`lat,lng`/`images[]`/`sharing[]` from the `info` dict vs today's lossy scrape. (3) **A short broker intro becomes SAFE** — today a non-block-format intro yields zero cards (scraper finds nothing); with supersession the carousel comes from the signal regardless of prose shape, so the nudge can never cost the cards. Tiny diff, zero behavior change, clean `git revert`, honest scope.
    **Build slices (test-first, behavior-identical, pause at offline-green before the live flip):** S1 `search.py` records `carousel_items` (top-5, exact field map above) + `carousel_map_center` on the signal. S2 `generate_ui_parts` emits native `carousel/listing` from the signal. S3 `chat.py` supersession guard (strip `property_carousel` when signal present) as a small testable helper. S4 regenerate P3 fixtures + assert the new carousel renders through the REAL FE renderers (`backend-fixtures.test.js`) + cross-contract parity. ~~S5 nudge to `search.md`~~. New test `test_carousel_contract.py` → add to `ci.yml`. **P4b (separate ticket): widen-the-deck swipe pagination + delete the regex entirely.**

- **2026-06-04 — P4 BUILD COMPLETE (offline-green; pre-live-flip checkpoint).** S1–S4 done, test-first; **S5 DROPPED** (see below). Verified channel-routing facts that gate the design: (1) WhatsApp can't double-carousel — `whatsapp.py:filter_interactive` whitelists only `(quick_replies, action_buttons, choice_list)`, so a `kind:carousel` unit is dropped (WA keeps its `send_carousel` template path); (2) web passes carousel through — `channel_adapter._CAPS["web"]` includes `carousel`. So the ONLY double-render risk is the web `parse_message_parts` carousel, handled by the S3 supersession guard.
  - **S1** `tools/broker/search.py`: pure `build_carousel_items(info_list, search_lat, search_lng, limit=5) -> (items, map_center)` + `_fmt_rent` (→ `₹9,000/mo`); `search_properties` calls `record_signal(carousel_items=…, carousel_map_center=…)` after `set_property_info_map` (same top-5 `property_template` WhatsApp shows). **S2** `core/ui_parts.py:generate_ui_parts`: emits `make_unit("carousel","result",{payload:listing, items, map_center})` when `signals["carousel_items"]` present (D2-consistent). **S3** `core/message_parser.py:drop_scraped_carousel(parts, has_native_carousel)` (pure) wired into BOTH `routers/chat.py` egress sites (capture `sig=current_signals()` once; strip `property_carousel` when `bool(sig["carousel_items"])`). **S4** `dump_contract_fixtures.py` now sources the `carousel/result/listing` unit AND the rich detail-sheet item from the REAL `build_carousel_items` emitter (was hand-typed minimal) → drift guard bites on field-map change; `backend-fixtures.test.js` renders the real output through `renderUnits` + `composePropertySheet` (gallery + sharing chips).
  - **Gates:** backend **25/25** (24 baseline + new `test_carousel_contract.py`, 55 assertions), FE **237/237** (incl. real-renderer fixtures 23/23). Zero regression.
  - **S5 DROPPED — reasoned scope refinement.** A `search.md` "short-intro, don't enumerate" nudge is (a) NOT needed for correctness — `parse_message_parts` *consumes* the property-block prose into the carousel (excluded from pre/post text), so stripping the scraped carousel removes the redundant prose too; and (b) in **direct conflict** with the retained legacy show-more path, which *requires* the broker to keep writing scrapeable blocks (a same-search "show more" has no fresh signal → cards come from scraping the broker's prose). Changing the prompt would break show-more's cards. Net: no prompt change → no Haiku-reliability surface → no show-more tension. Token-waste optimization (broker writes blocks discarded on web) is deferred to **P4b**, which reworks show-more so prose pagination goes away entirely.
  - **Only visible behavior change (call out at live verify):** on fresh search the structured carousel **appends after** the broker's prose (intro + recommendation), vs today's scraped carousel sitting *between* pre/post text. This is **consistent with the already-live D2 comparison unit** (signal-driven units append as supplements after the body) — established contract pattern, not a regression; cards are also richer (real amenities/coords/gallery/sharing vs lossy scrape). If live UX prefers cards-before-recommendation, an in-place splice is a small follow-up.
  - **REMAINING before "P4 done": live flip.** PR → CI green → squash-merge → deploy → poll prod for new code → read-only live smoke (Kurla boys / Powai girls / Bandra→nearby / show-more) checking: exactly one carousel, richer cards, Details sheet composes rich, gender hard-constraint holds, honest empty works, show-more still renders cards (legacy path). `git revert <merge>` staged.
