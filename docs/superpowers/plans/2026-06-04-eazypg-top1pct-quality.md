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
