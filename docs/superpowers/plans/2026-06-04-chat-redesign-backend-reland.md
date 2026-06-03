# Chat Redesign Backend Re-land — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement task-by-task. Steps use checkbox (`- [ ]`) syntax. This plan is the SINGLE SOURCE OF TRUTH across sessions — update the checkboxes and the Decision Log as you go, and commit the plan with the code.

**Goal:** Re-land the reverted backend PR #16 (contract spine + native units + channel egress) so the backend emits the structured payloads the now-LIVE redesigned frontend renders — fixing comparison-as-text, thin property details, and the empty-chips rollback — then ship lockstep-safe.

**Architecture:** Backend produces native `{kind, state, data, surface}` units (`core/ui_parts.py:generate_ui_parts` + `_to_native`), validated against `core/contract.py` (byte-mirrors `eazypg-chat/src/contract.json`). Web egress passes units **verbatim** (`core/channel_adapter.adapt("web")`) to the FE renderer registry; WhatsApp egress degrades them (`channel_adapter` + `channels/whatsapp.units_to_wa_messages`). Request-scoped truth signals (`core/signals.py`) flow from tool seams to egress so structured/honest UI is data-driven, not prose-parsed.

**Tech Stack:** Python 3.11 (FastAPI, asyncio), Redis, PostgreSQL, Anthropic Claude. Frontend (read-only / frozen): Vite vanilla-JS SPA in sibling repo `eazypg-chat`.

---

## ⚠️ Resume Protocol (run at the START of every session on this plan)

1. **Re-orient from disk, not memory.** Read this plan top-to-bottom (it is the source of truth). Skim the Decision Log + Invariants.
2. **Verify git state** (prose notes have been false before — trust `git`):
   ```bash
   cd "/Users/eazypg/CC Booking Bot FInal/claude-booking-bot"
   git branch --show-current        # expect: feat/chat-redesign-backend-reland
   git log --oneline -6
   git status -s
   ```
3. **Establish the green baseline** (python3.11 venv; tests are standalone scripts, NOT pytest):
   ```bash
   ANTHROPIC_API_KEY=test-key-not-used ./.venv/bin/python test_cross_contract.py
   # then the full gate (see Verification section) before AND after your changes
   ```
   If `.venv` is missing: `python3.11 -m venv .venv && ./.venv/bin/pip install -r requirements.txt`.
4. **Find the next unchecked deliverable** in the Status Tracker. Do its **Grounding task FIRST** (re-read the exact FE/BE files — the FE is the contract authority and may have moved).
5. **Work test-first** (the D1 cross-contract pattern: faithful Python mirror of the live FE renderer + adversarial old-shape-renders-blank guard). Commit per task. Update this plan's checkboxes + Decision Log. Do NOT push or deploy until a deliverable is complete and the full gate is green.

---

## Invariants (do not violate — these are why #16 was reverted)

- **The live FE is the contract authority.** Match the exact `data` keys each renderer reads. Sources of truth (eazypg-chat @ `origin/main` `e02982c`, byte-identical to local `fix/spacing-pass`):
  - `src/ingress.js` — `normalizePart`: a native unit (has `kind`) passes through `isValidUnit` verbatim; only legacy `{type}` parts hit `fromLegacy`.
  - `src/renderers/server-parts.js` — `KIND_RENDERERS` (the native data-key reads).
  - `src/renderers/primitives.js` — `renderStatusRail` / `renderChoiceList` / `renderMapUnit` / `renderInputRequest`.
  - `src/renderers/comparison.js` — `renderComparison` / `toComparisonModel`.
  - `src/renderers/property-sheet.js` — `composePropertySheet` (the detail sheet keys).
  - `src/message-builder.js` — `partitionBySurface`: **`surface:"sheet"` auto-opens the detent bottom sheet** (don't push sheet units unless intended).
  - `src/contract.json` — enums; keep `core/contract.json` byte-mirrored (test_contract_parity guards this).
- **One contract key per concept.** `quick_replies` → `data.chips` (NOT `replies`) end-to-end (web verbatim + WhatsApp egress). Never reintroduce a backend-internal alias.
- **`is_valid_unit` checks enums only, never data sub-keys** — so a parity-by-enum test is NOT enough. Every emitted unit MUST be asserted through the FE-mirror cross-contract test (`test_cross_contract.py`).
- **Supplements-only on web.** `generate_ui_parts` never emits the response body — that is owned by `parse_message_parts` (web) / `send_text` (WhatsApp). Re-emitting it double-renders. (Branch fixes already in: `f0c5c91`.)
- **Signals survive `asyncio.gather`** only via in-place dict mutation (`core/signals.py`; branch fix `832aee1`). Record with `record_signal(**kw)`; read at egress with `current_signals()`.
- **Tests are standalone scripts** (`python test_x.py`, exit 0 = pass; no network/Redis/LLM). Add every new test to `.github/workflows/ci.yml`.
- **Live FE has ZERO `state:"partial"` rendering** — only `renderStatusRail` branches on state (error/empty). See D4.

---

## Status Tracker (the durable checklist — keep current)

- [x] **D1 — `_to_native` key alignment + cross-contract test.** Shipped commit `ca363c5`. quick_replies `replies`→`chips` (end-to-end), gallery `property_name`→`property`, status_card→`status_rail`, expandable→inline. `test_cross_contract.py` (35 assertions, proven to bite). Gate 22/22.
- [x] **D2 — Structured comparison emission.** Shipped commit `3ac75ce`. `compare.py:build_comparison_items()` (pure) → `record_signal(comparison_items=...)`; `generate_ui_parts` emits the native `comparison` unit; `compare.md` nudged to short-recommendation-only. Cross-contract D2 section + adversarial no-signal guard. Gate 22/22.
- [x] **D3 — Rich detail sheet (spec §3.2).** Enriched the stashed carousel item with a multi-image gallery + structured sharing options (the two sections with real data today; reviews/rules/honest-bit/commute have no source → deferred, sheet degrades). `parse_sharing_types_structured` (utils/api.py) + `images`/`sharing_types_list` cache keys (search.py) + `_sheet_enrichment` propagation (message_parser.py). Cross-contract `composePropertySheet` mirror + adversarial thin/old-string guards. Gate 22/22 (cross-contract 55 assertions).
- [x] **D4 — Partial-success receipt.** Changed the partial branch in `generate_ui_parts` from `confirmation`/`partial` to `status_rail`/`partial` variant `warn` (live FE has no partial rendering). Body is a SHORT caveat (NOT response_text → no double-render). Signal already fires (`schedule_visit.py:158`). Updated `test_ui_parts_native` + `test_signals` in lockstep; cross-contract guard proves the OLD confirmation/partial shows phantom Confirm/Cancel + drops the caveat. Gate 22/22.
- [x] **D5 — Web human-handoff teammate identity.** Web human-mode turns streamed `''` (dead air). Fixed at the WEB egress (`routers/chat.py` — both `/chat` and `/chat/stream`), NOT `run_pipeline` (shared with WhatsApp, where silence is correct). New `make_human_handoff_part(brand_name)` → `status_rail` ok with the brand identity. Cross-contract test for the generator (+ graceful empty-brand fallback). Gate 22/22.
- [x] **D6 — WhatsApp `send_units` wiring (LAST).** Wired `generate_ui_parts → filter_interactive → send_units` into `routers/webhooks.py:_drain_and_process` (after the body+carousel+images sends). **Interactive-only** (user-chosen scope): forwards just quick_replies/action_buttons/choice_list (the tappable UX WA lacks); text/status_rail/listing-carousel/media excluded so nothing the existing path already sends is duplicated → ZERO double-send, no template-carousel regression. Fixed the generic "Pick one"/blank body (real prompt body in `units_to_wa_messages`). `test_whatsapp_egress` +2 sections (body + filter). Gate 22/22.
- [x] **SHIP — lockstep verification + deploy.** ✅ DONE. PR #18 squash-merged to main (`bd1a4a2`); CI gate green on the merge commit; `deploy-render.yml` triggered Render; new code confirmed live (search supplements switched legacy `{type}`→native `{kind}`). **Post-deploy lockstep smoke on REAL prod: 23/23** (`/tmp/smoke_post_deploy.py` — read-only search/details/compare; every native unit rendered through the byte-faithful FE mirror; D1 native chips w/ `data.chips`, D2 native comparison unit ≥2 cols + no double-render, D3 5/5 carousel items carry gallery/sharing + sheet composes). Prod /health 200. Strictly better than baseline (legacy comparison_table→structured; thin→enriched sheet), nothing broken (5 cards + chips intact). `git revert bd1a4a2` ready if needed.

---

## D2 — Structured Comparison Emission — ✅ DONE (`3ac75ce`)

**Problem:** `tools/broker/compare.py:compare_properties` builds a rich structured list (`comparison[]`, lines 129-147) then flattens it into a PROSE string and returns that to the LLM, which writes its own text breakdown → the FE renders text. The FE `comparison` renderer works (seeded-verified); it is just never fed structured data.

**Approach:** Record the structured comparison on the signal slate in `compare_properties`; emit a native `comparison` unit from `generate_ui_parts` (the same signal→egress pattern as the empty/error/partial branches); nudge the broker (compare.md) to write a SHORT recommendation rather than a full text table.

**FE contract (`comparison.js:toComparisonModel`):** native `data.items[]` = `[{name, score?, badge?, attrs:[{label, value, best?}]}]`. `renderComparison` returns null if `<2` items. The NATIVE path does NOT auto-compute `best` (only the legacy headers/rows path does) — so the backend must set `best:true` on winning cells and `badge` on the recommended item.

**Files:**
- Modify: `tools/broker/compare.py` — build `items[]` from `comparison[]`, `record_signal(comparison_items=items)`.
- Modify: `core/ui_parts.py:generate_ui_parts` — read `signals.get("comparison_items")` → `make_unit("comparison", "result", {"items": items})`.
- Modify: `skills/broker/compare.md` — nudge: emit the structured compare + a 1-2 sentence recommendation, NOT a prose table.
- Test: `test_cross_contract.py` — add a `comparison` mirror (`toComparisonModel` + `renderComparison`'s <2 rule) + section asserting a real `compare_properties`-shaped payload renders ≥2 columns with score/attrs/best.

- [ ] **Step 1 — Grounding (confirm, don't assume):** Re-read `tools/broker/compare.py:129-186`, `core/ui_parts.py:856-973` (the signal branches + call shape), `core/signals.py`, and `eazypg-chat/src/renderers/comparison.js`. Confirm: (a) `generate_ui_parts` receives `signals`; (b) where `record_signal` is safe to call inside `compare_properties` (it runs under `asyncio.gather` in the agent loop — in-place mutation is required, already guaranteed by `signals.py`); (c) the exact `items[]`/`attrs[]` shape. Record findings in the Decision Log.

- [ ] **Step 2 — Write the failing cross-contract test.** Add to `test_cross_contract.py`: a Python mirror of `toComparisonModel` (native items[] precedence; `<2` → null) + `renderComparison`, and a section that builds an `items[]` payload from a representative `compare_properties` result and asserts: unit is `kind:"comparison"`, valid, renders non-empty (≥2 columns), each item's `name`+`score` survive, ≥1 attr `best:true` is honored, and the OLD behavior (prose text, no comparison unit) would render no comparison. (Mirror code written in full at execution time against the then-current `comparison.js`.)

- [ ] **Step 3 — Run it; verify it FAILS** (`comparison` unit not yet emitted). `ANTHROPIC_API_KEY=test-key-not-used ./.venv/bin/python test_cross_contract.py` → FAIL on the new section.

- [ ] **Step 4 — Implement `compare_properties` items[] + `record_signal`.** Map each `comparison[]` entry → `{name, score, badge, attrs:[{label,value,best}]}` (attrs: Rent/Location/Match/For/Distance/Amenities/Token/Beds — omit empty); set `badge:"Best match"` + per-row `best` on the max-score item and lowest-rent cell; `record_signal(comparison_items=items)`. Keep returning the prose string to the LLM (it still needs the data to reason) UNLESS Step 1 shows double-render risk.

- [ ] **Step 5 — Implement the `generate_ui_parts` emission.** After the honesty branches, `if signals.get("comparison_items"): parts.append(make_unit("comparison","result",{"items": signals["comparison_items"]}))`. Ensure it does not duplicate when chips also fire.

- [ ] **Step 6 — Run the new test; verify PASS.** Then run the FULL gate (Verification section) — confirm 0 regressions.

- [ ] **Step 7 — Broker nudge.** Edit `skills/broker/compare.md`: after `compare_properties`, write a SHORT (1-2 sentence) recommendation; the structured comparison card carries the table — do NOT re-type it as prose. (Prompt-only; verify no test depends on the old prose.)

- [ ] **Step 8 — Commit.** `git add tools/broker/compare.py core/ui_parts.py skills/broker/compare.md test_cross_contract.py && git commit` (message: `feat(contract): emit structured comparison unit from the compare path (D2)`). Update this plan's tracker + Decision Log.

---

## D3 — Rich Detail Sheet (spec §3.2) — Grounding-First

**Symptom:** the Details sheet opens but looks thin (no gallery/sharing/reviews/rules/honest-bit/commute).

**Likely shape (confirm in grounding):** the sheet composes CLIENT-SIDE from the stashed carousel property object (`message-builder.wireViewFull` → `composePropertySheet(prop, flags)`), NOT from a backend `surface:"sheet"` unit. So the backend work is to **enrich the carousel `items[]` fields** the FE stashes, so the sheet has data to compose. (Do NOT push `surface:"sheet"` units — that auto-opens the sheet; see Invariants.)

- [x] **Grounding:** Read `eazypg-chat/src/renderers/property-sheet.js` (exact keys read + the `images|media|gallery`, `sharing|rooms|room_types`, `reviews`, `house_rules`, `honest_bit`, `commute|landmarks`, `lat/lng` degradation), `eazypg-chat/src/renderers/property-card.js` (what `getStashedProperty` stores), and backend `tools/broker/search.py` (the carousel item fields) + `property_details.py` / `room_details.py` (richer data available). Decide: which fields the backend can populate today vs which need a new fetch. Record in Decision Log. ✅ DONE — see Decision Log 2026-06-04 D3 grounding.
- [x] **TDD tasks (write at execution time):** add a `composePropertySheet` mirror to `test_cross_contract.py`; assert that an enriched carousel item yields a non-thin sheet (gallery + sharing + whatever real data exists), degrading gracefully when a field is absent. Implement the enrichment in `search.py` (and/or the carousel item builder). Commit. Acceptance: a real OxOtel property's Details sheet renders gallery + sharing + ≥1 rich section; absent fields degrade silently (no empty boxes). ✅ DONE — `section_detail_sheet_enriched_renders_gallery_and_sharing` (10 assertions incl. 2 adversarial: thin item degrades, old display-string sharing renders blank). Implementation: enrich the carousel item dict (the stash source) — sheet composes client-side, no `surface:"sheet"` unit emitted.

---

## D4 — Partial-Success Receipt — Grounding-First

**Critical finding (verified D1):** the live FE has **no `state:"partial"` rendering**. `renderConfirmationCard` ignores state and always draws Confirm/Cancel; only `renderStatusRail` branches on state. So a `confirmation`/`partial` unit renders phantom buttons + drops ok/warn. **Emit the receipt as `status_rail` variant `"warn"`** (title = what succeeded, body = the follow-up caveat; no buttons). The existing partial branch in `generate_ui_parts` (and `test_ui_parts_native.py` / `test_signals.py`) encode the old `confirmation`/`partial` shape — update them in lockstep.

- [x] **Grounding:** Re-read `core/ui_parts.py:888-898` (current partial branch), `test_ui_parts_native.py:77-92`, `test_signals.py:~71`, and the write tools (`tools/booking/reserve.py`, `schedule_visit.py`, `payment.py`) to confirm where a real half-success is known and whether `record_signal(booking_held=..., crm_synced=...)` already fires. Record in Decision Log. ✅ DONE — see Decision Log 2026-06-04 D4.
- [x] **TDD tasks:** add an adversarial guard to `test_cross_contract.py` proving the OLD `confirmation`/`partial` shape renders phantom buttons + drops the warn line, and the NEW `status_rail` warn renders the ok-title + warn-body. Change the partial branch to emit `status_rail` warn. Wire the real signal from the write tool seam (if not already). Update `test_ui_parts_native.py` + `test_signals.py`. Commit. Acceptance: a simulated booking-ok/CRM-failed turn emits a `status_rail` warn that renders title+body, no buttons. ✅ DONE — `section_partial_receipt_is_status_rail_warn_not_confirmation` (cross-contract).

---

## D5 — Web Human-Handoff Teammate Identity — Grounding-First

**Symptom:** on the web channel, human-handoff delivery returns `""` (`core/pipeline.py:~59-63`), so the teammate's identity/message never reaches the SSE stream.

- [x] **Grounding:** Read `core/pipeline.py:32-113` (run_pipeline, human-mode branch, web vs WhatsApp delivery) and `routers/chat.py` (the `/chat/stream` SSE send path). Determine how the teammate identity should be emitted as a unit/text on the stream. Record in Decision Log. ✅ DONE — see Decision Log 2026-06-04 D5.
- [x] **TDD tasks:** add a test proving the web human-mode path yields a non-empty teammate-identified payload (a `text`/`status_rail` unit, per grounding). Fix the `""` return. Commit. Acceptance: a web turn in human mode streams the teammate identity, not `""`. ✅ DONE — `section_web_human_handoff_streams_teammate_identity`.

---

## D6 — WhatsApp `send_units` Wiring (LAST) — Grounding-First

**State:** `channels/whatsapp.units_to_wa_messages` (egress) is built + tested but never called from the live WhatsApp path.

- [x] **Grounding:** Read `routers/webhooks.py:_drain_and_process` (after `run_pipeline`), the current WA send path (`send_text`/`send_carousel`), and the `rentok_interakt` template-carousel usage. Decide coexistence (native units vs template carousels) and the fix for the generic interactive `body` ("Pick one"). Record in Decision Log. ✅ DONE — see Decision Log 2026-06-04 D6.
- [x] **TDD tasks:** extend `test_whatsapp_egress.py` for the wiring + body fix. Wire `units_to_wa_messages` into `_drain_and_process`. Commit. Acceptance: a WA turn sends native units degraded via the egress, with no regression to the existing template carousels. ✅ DONE — `section_interactive_messages_carry_a_meaningful_body` + `section_filter_interactive_keeps_only_tappable_kinds`.

---

## Verification & Ship

**Full offline gate** (run before + after every deliverable; all standalone, no network/Redis/LLM):
```bash
cd "/Users/eazypg/CC Booking Bot FInal/claude-booking-bot"
export ANTHROPIC_API_KEY=test-key-not-used
for t in test_untrusted_content test_tenant_isolation test_webhook_signature test_cost_accounting \
  test_server_stop test_engine_contract test_tool_boundary test_wave_a test_contract_alignment \
  test_gender_filter test_shortlist_contract test_admin_login test_quality_analytics test_listing_leak \
  test_contract test_contract_parity test_cross_contract test_signals test_ui_parts_native \
  test_channel_adapter test_web_egress test_whatsapp_egress; do
  printf "%-30s " "$t"; ./.venv/bin/python "$t.py" >/tmp/g_$t 2>&1 && echo PASS || { echo FAIL; tail -5 /tmp/g_$t; }
done
```
(Keep `.github/workflows/ci.yml`'s list in sync with any new test files.)

**Lockstep smoke (the test the rollback lacked) — run once before ship:** point `eazypg-chat/vite.config.js` `/api/stream` proxy at the new backend; load with NO `?brand=` (uses `FALLBACK_ACCOUNT_VALUES` real OxOtel pg_ids + `BRAND_TOKEN:""` → backend default brand); send **read-only** queries only (search/details/compare — NEVER booking/payment/schedule). Assert DOM `.property-card` / `.qr-chip` / `.comparison-unit` non-empty + no console errors. Revert the proxy edit before committing.

**Deploy:** new-FE is live, so new-BE is safe to deploy ONCE its emitted keys match the live FE (D1 done). Render auto-deploys `main` — so SHIP = open PR from `feat/chat-redesign-backend-reland` → CI gate green → merge → verify with a prod read-only search → keep `git revert` ready.

---

## Decision Log (append as you go — newest last)

- **2026-06-04 — Re-land strategy:** Branched `feat/chat-redesign-backend-reland` off main `5d0283e`; cherry-picked #16's 9 feature commits `77e5938..f0c5c91` (NOT the stale merge tip `acd9a52`). Only conflict: `ci.yml` test list (unioned). Code parity vs original branch = empty diff.
- **2026-06-04 — D1 contract-key fix is end-to-end, not just `_to_native`:** the backend had standardized on `replies` across `channel_adapter` + `whatsapp.units_to_wa_messages`; aligned ALL to the FE's `chips`. Single key per concept.
- **2026-06-04 — status_card → status_rail (not confirmation):** a completed milestone must not render Confirm/Cancel; folded subtitle+details into the rail body; celebration/actions intentionally dropped per redesign §3.
- **2026-06-04 — expandable → inline (not surface:sheet):** `surface:"sheet"` auto-opens the detent sheet (message-builder:289); the sheet is owned by the explicit Details affordance.
- **2026-06-04 — D4 partial receipt CANNOT be `confirmation`/`partial`:** the live FE has no partial rendering; use `status_rail` warn. The handoff prose assumed otherwise — trust the FE code.
- **2026-06-04 — Known live-FE bug (out of scope, FE frozen):** carousel media bridge passes `legacy.property` but `renderImageGallery` reads `part.property_name` → gallery header label always "Property" (thumbnails still render). Flag for a future FE fix.
- **2026-06-04 — D2 data-flow decided:** `compare_properties` will `record_signal(comparison_items=items)`; `generate_ui_parts` emits the native `comparison` unit from the signal slate — same pattern as the empty/error/partial branches (`core/signals.py`).
- **2026-06-04 — D3 grounding (CONFIRMED end-to-end, no assumptions):** The detail sheet composes **CLIENT-SIDE** — NOT from a backend `surface:"sheet"` unit. Chain: `parse_message_parts` (web body owner) builds the legacy `{type:"property_carousel", properties:[...]}` part → FE `ingress.fromLegacy` maps it **wholesale** `data.items = p.properties` (NO key whitelist) → `KIND_RENDERERS.carousel` → `renderPropertyCarousel` stashes `originals[i]` (the full backend item dict) via `property-card.stashProperty` → Details button `data-prop-key` → `wireViewFull` → `composePropertySheet(full, flags)`. So D3 = **enrich the carousel item dict** in `message_parser.py`; any key added there reaches the sheet verbatim. `composePropertySheet` reads (all gated/hidden when absent — no empty shells): name, price|rent, location, walk_to_metro|metro|metro_distance, images|media|gallery (array of {url}|string; falls back to single `image`), sharing|sharing_options|rooms|room_types (**array** of {type|label|sharing|name, price|rent|amount}), amenities (string|array), commute | landmarks|distances, lat/lng (map), house_rules, reviews|resident_reviews, honest_bit|the_honest_bit, token|token_value + free_cancellation* (BOTH flag-gated on PAYMENT_REQUIRED — OFF in prod, so neither renders). **Data decision (today vs new-fetch):** (1) **sharing → AVAILABLE today** but collapsed: search results carry `p_sharing_types_enabled`, cached only as a display STRING (`parse_sharing_types`) which the sheet's `_sharingOptions` rejects (`Array.isArray` fails → []). Fix = cache a STRUCTURED `[{label,price}]` (new `parse_sharing_types_structured` + new cache key `sharing_types_list`) and propagate as carousel `sharing`. (2) **multi-image gallery → AVAILABLE today, near-free:** `fetchPropertyImages` (already called in `_enrich_with_images`) returns the FULL list but `_fetch_first_image` keeps only `[0]`; capture the list (new cache key `images`) and propagate. No new network call. (3) **metro/commute/landmarks → NOT in search data** (would need a per-property landmarks fetch, N calls) → DEFER, sheet degrades. (4) **reviews/house_rules/honest_bit → NO data source anywhere today** → OUT OF SCOPE, sheet hides them. So D3 ships **gallery + sharing**, satisfying acceptance "gallery + sharing + ≥1 rich section; absent fields degrade silently". Consumer-safety verified: `_fetch_first_image` has 1 caller; cached `sharing_types` string read only by `room_details.py` (unchanged — new keys are additive); `is_valid_unit`/channel egress/WA egress ignore extra item keys.
- **2026-06-04 — SHIPPED TO PROD (`bd1a4a2`):** Autonomous ship (user delegated, "better + don't break"). Sequence: re-confirmed offline gate 22/22 → CI green on HEAD `5fa56fa` → PR #18 squash-merged → `deploy-render.yml` (CI-gated) triggered Render → polled prod until native units appeared (legacy `{type}`→native `{kind}`) → **post-deploy lockstep smoke 23/23 on real prod**. Local LLM smoke was impossible (empty `ANTHROPIC_API_KEY` locally + PG/Redis down) so verification used the prod API + the byte-faithful FE mirror (the same mirror the offline cross-contract test uses) — stronger than a single visual, it renders EVERY emitted unit through faithful ports of server-parts.js/primitives.js/comparison.js/property-sheet.js. Baseline (legacy prod) captured first: search=5 cards+4 chips, compare=legacy comparison_table. After: search=5 cards + native chips + D3-enriched items (sheet composes gallery+price+sharing), compare=native comparison unit (no double), details=media carousel. Headless-browser visual was attempted but the agent-browser tool couldn't trigger this SPA's send handler (tool limitation, not a product issue) — NOT faked; the API+mirror lockstep stands. Cleanup: throwaway local PG torn down, local Redis MISCONF config restored to as-found. **Revert path: `git revert bd1a4a2` on main → auto-redeploy legacy.** Residual: D5(web human-handoff) + D6(WhatsApp) not live-exercised (no human-mode/WA trigger) but covered by offline tests + healthy deploy + guarded wiring (D6 in try/except).
- **2026-06-04 — D6 grounding + shipped (interactive-only, user-chosen):** `send_units` (egress) + `units_to_wa_messages` were built+tested but never called from the live drain. The drain (`webhooks.py:116-128`) already sends `send_text(response)` + template `send_carousel(get_property_template)` + `send_images`. `units_to_wa_messages` degrades: quick_replies/action_buttons→buttons, choice_list→list, media-carousel→images, **everything else (status_rail, comparison, LISTING carousel via `adapt`) → text via `to_plain_text`**. So a naive full `send_units(generate_ui_parts(...))` would DOUBLE-SEND the body (rail→text == response), the carousel (listing→list collides with the template carousel) AND images (media→image collides with send_images). **User chose interactive-only scope:** new `filter_interactive(units)` keeps just the 3 interactive kinds (quick_replies/action_buttons/choice_list) — the tappable UX WA genuinely lacks — and the drain forwards only those via `send_units` after the existing sends. Honesty-branch turns emit only a status_rail → `filter_interactive` → [] → nothing forwarded (body already sent by `send_text`). Zero double-send, no template-carousel regression. `generate_ui_parts` is supplements-only so it never emits the listing carousel anyway — the filter is also defensive. Signals: `current_signals()` is valid post-`run_pipeline` in the same drain task (in-place contextvar). Body fix: `units_to_wa_messages` now sets a real `body` (prompt/title, sensible default) on list/buttons messages; `send_units` fallbacks upgraded from "Pick one"/" " to "Tap an option:"/"Quick replies:". **FULLER egress (forwarding rails/comparison too, or replacing the ad-hoc sends entirely) = a deliberate future follow-up**, not done here (avoids live-WA double-send risk). **ALL D1-D6 DONE — only SHIP (lockstep smoke + deploy) remains.**
- **2026-06-04 — D5 grounding + shipped (`93acf28`):** Web human-mode returned dead air via TWO paths, BOTH in `routers/chat.py`: `/chat` (non-stream) `agent_name=="human"` → `parts=[]`; `/chat/stream` own bypass → empty `done` event. `run_pipeline:63-67` returns `("", "human", lang)` but is SHARED with the WhatsApp webhook — and WhatsApp SHOULD stay silent on takeover (the admin's replies push directly via `channels.whatsapp.send_text`; a per-message bot note there would be spam). So the fix belongs at the WEB egress (chat.py), NOT `run_pipeline`. `set_human_mode` stores only `{active, taken_at}` — no teammate name — so the identity is the brand name (`get_brand_config_by_hash(bh)["brand_name"]`, fallback "our team"). New `make_human_handoff_part(brand_name)` → `status_rail`/`result` variant `ok` (calm note, no buttons), wired into both chat.py paths. Emitted per user-message in human mode (each message gets an ack rather than dead air). NOTE (out of scope): web has no server push, so the admin's actual replies reaching a web user is a separate delivery problem — D5 only kills the dead-air and identifies the team. i18n of the handoff string = possible follow-up (English MVP). Next: D6 (WhatsApp `send_units` wiring — LAST).
- **2026-06-04 — D4 grounding + shipped (`39282ba`):** The half-success signal **already fires** — `schedule_visit.py:158` records `record_signal(booking_held=True, crm_synced=False)` when the visit is saved but `_create_external_lead` fails; `reserve.py:96` + `schedule_visit.py:166` fire the full-success variant. So NO write-tool wiring was needed — D4 was emission + 2 tests only. Verified `renderStatusRail` (primitives.js): `variant:"warn"` → `rail--warn`, renders `title`+`body`, retry button ONLY when `state==="error" && data.retry` → a warn rail has **no buttons**; it does NOT special-case `state:"partial"` (falls to the warn branch via variant). Chose `make_unit("status_rail", "partial", {variant:"warn", title, body, retry:False})` — keeps the `partial` state enum meaningful AND renders correctly. **Body = a fixed short caveat, NOT response_text** — the OLD branch's `body: response_text` violated supplements-only (parse_message_parts owns the body on web → double-render). `partial` + `warn` are both valid contract enums; `is_valid_unit` checks them independently (no kind×state matrix). Cross-contract guard proves OLD `confirmation`/`partial` draws phantom Confirm/Cancel + drops `data.warn` (FE never reads it). Next: D5 (web human-handoff identity returns `""`).
- **2026-06-04 — D3 shipped (`3ca233e`):** gallery + structured sharing only (the two sections with a real data source today). Image path: `_fetch_first_image`→`_fetch_images` (returns full list; 1 caller), `_enrich_with_images` sets `p_image`=cover + `_images`=full. Sharing: new `parse_sharing_types_structured` → cache `sharing_types_list` (existing string `sharing_types` UNCHANGED — `room_details.py` still reads it). Both new cache keys + `images` propagated via `_sheet_enrichment` onto the carousel item (the FE stash source). Verified the full FE chain has NO key whitelist (`ingress.fromLegacy` passes `properties` wholesale → `data.items` → stash). Adversarial proof in test: the old display-string sharing renders blank (why structured is required). **Out of scope (no data source):** reviews/house_rules/honest_bit; **deferred (needs per-property fetch):** metro/commute/landmarks. Sheet degrades gracefully for all. Next: D4 (partial-success receipt as `status_rail` warn — live FE has no partial rendering).
- **2026-06-04 — D2 shipped (`3ac75ce`):** wiring confirmed live (`pipeline.py:40` reset_signals; `chat.py:165/278` pass `signals=current_signals()` to both /chat and /chat/stream). `build_comparison_items` sets `best` on lowest rent (only with a real spread) + top match score, badges the top scorer "Best match", omits empty attrs. Kept returning the prose string to the LLM (it reasons over it) — no double-render because `generate_ui_parts` is supplements-only and `compare.md` now forbids re-typing the table. **NOT updated: the legacy monolithic compare prompt in `core/prompts.py`** (DYNAMIC_SKILLS_ENABLED=false fallback, off in prod) — if that flag is ever flipped, mirror the nudge there.
