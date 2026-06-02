# EazyPG Booking Bot — Roadmap to Top-1% Quality

> Produced by an 11-agent research workflow: 5 external-research agents (GitHub/Reddit/X/Substack/Context7 + Anthropic/Notion eng practice) + 1 codebase-grounding agent → adversarial cross-check of the 4-point proposal → synthesize → critique → finalize. Every claim below is grounded in either the codebase or a named research source. ~900k subagent tokens.

## Current-State → Top-1% Scorecard

| Dimension | Now | Target | One-line justification (grounded) |
|---|---|---|---|
| **Agent architecture & tool design** | 4/5 | 5/5 | Clean supervisor→specialist routing with 3-tier resolution ([pipeline.py:86](../core/pipeline.py#L86)), per-turn skill/tool filtering + graceful expansion ([tool_executor.py:80](../core/tool_executor.py#L80)). One real hole: **no uniform tool-result envelope** — every tool returns freeform English, success indistinguishable from failure ([reserve.py:101](../tools/booking/reserve.py#L101)). Plus two confusable raw IDs (`pg_id`/`p_id`) handed to Haiku. |
| **Evals & testing** | 2/5 | 4/5 | 13-test hermetic CI gate locks *past* bugs well ([ci.yml](../.github/workflows/ci.yml)), but every test is backward-looking. No golden set, no graded eval, no tool-selection eval; `stress_test_broker.py` is out-of-CI and flaps on Haiku variance. The next novel 100%-breaking drift ships exactly like the last three. |
| **Observability & reliability** | 3/5 | 5/5 | Real per-tool success/latency dual-write surfaced in `/admin/analytics` ([analytics.py:305](../db/redis/analytics.py#L305)) — but `_track(success=True)` fires on any non-raising return ([tool_executor.py:133](../core/tool_executor.py#L133)), so it shows `0%` failure during a 100%-broken flow. Zero alerting; `/health` is liveness-only. |
| **Conversational UX / latency / routing** | 3/5 | 4/5 | Cost-tiered routing (Haiku broker / Sonnet others) + true SSE streaming + Phase B/C WhatsApp handling. But Haiku on the primary agent is the documented flake source with **no escalate-to-Sonnet on high-stakes turns**, and a blocking supervisor `classify()` Haiku round-trip of dead air precedes every turn. |
| **Prompt & context engineering** | 4/5 | 5/5 | Most mature dimension: split caching ([claude.py:418](../core/claude.py#L418)), real injection fence ([untrusted.py](../core/untrusted.py)), modular skills, 3-tier KB. Gaps: honesty enforced by prose only (no provenance assertion), legacy monolith + deferred KYC scaffolding rotting behind flags, summarizer is an untested prompt that can drop a hard constraint after turn 30. |

**Composite ~3.2/5.** Architecture and prompts are strong (4s); the weak links are evals (2) and a reliability stack that is structurally blind to your #1 incident class. **The single highest-leverage move is not four parallel workstreams — it is one keystone (a central contract-validator that computes a typed `{ok}` from the RentOk payload) built first, then everything stacks on it.**

---

## The dependency chain (read this before the ranked list)

The 4-point proposal frames reliability/observability/eval/code as parallel. **They are mostly a stack, not a menu** — with one important exception called out below.

The central validator that computes `ok` from "did RentOk actually confirm" is *one mechanism with four payoffs*: it gates the merge (contract test), computes the runtime envelope (typed success), feeds the honest dashboard (real success-rate), and is the ground truth the eval asserts against. So the spine is: **validator → contract-tests → observability → eval.** In the ranked list below this is **Initiative 1 → 2 → 3 → 4**.

**The one control that does NOT stack on the validator: reconciliation.** The bot-said-booked-vs-Postgres-lead-row check (part of Initiative 3) is independent of the tool's self-report by design — that is its entire value. It can ship *before* the validator and catch the fire-and-forget class standalone. It is the cheapest pre-launch silent-success detector and should not be sequenced behind the keystone. Build the reconciliation delta first, in parallel with the validator.

*(Numbering note: throughout this document, "Initiative N" always refers to the ranked list below — never to the original 4-point proposal. There is one numbering.)*

---

## Ranked Initiatives

### 1. The tool-result envelope + central contract-validator (the keystone)
**Why.** Confirmed in code: `_track(True)` fires whenever a handler returns a string without raising ([tool_executor.py:133](../core/tool_executor.py#L133)); `tool_boundary.py` is input-only (no output contract exists). Every tool returns freeform English ([reserve.py:101](../tools/booking/reserve.py#L101)). This is the soil that grew all three 100%-breaking bugs (shortlist wrong-key, fire-and-forget CRM, empty-contact field). Research Block 1 §4 / Block 3 §2 / Block 5 §8 all name typed, outcome-derived success as *the* top-1% discriminator: "a tool that lies corrupts every downstream decision."

**The critical design point the proposal missed:** `ok` must be **computed centrally from the RentOk payload by a shared validator**, never self-declared by each tool. The shortlist tool *would have* returned `ok=true` — it thought it succeeded. So the envelope's `ok` is derived from "did the expected confirmation key appear with the expected value," using the same logic that pins the contract tests (Initiative 2).

**The money-safety field that matters most — `committed`.** The envelope carries `committed: bool|None` and this is the single most important field for a money flow, not a footnote. The dangerous state is `ok=false ∧ committed=true`: RentOk *took the booking/payment* but the response parse failed. A naïve retry on `ok=false` then **double-books or double-charges**. So:
- The validator must distinguish "the write definitely did not happen" (`committed=false`, safe to retry) from "we cannot prove the write didn't happen" (`committed=None`, **must not** blind-retry).
- Wire `committed` into the existing burst-dedup in [`db/redis/idempotency.py`](../db/redis/idempotency.py): the idempotency key/result-cache must key off the *operation identity* (uid + property + slot/amount), so a retry of an already-`committed` write returns the cached prior result instead of issuing a second RentOk write. `ok=false ∧ committed∈{true,None}` routes to human reconciliation, never to an automatic retry.

**How.**
- Define `ToolResult = {ok: bool, data: dict, user_message: str, error: str|None, committed: bool|None}`.
- Harden [`utils/api.py:check_rentok_response`](../utils/api.py#L14) into a per-endpoint contract validator keyed off `VERIFIED_RENTOK_CONTRACT.md` — asserts the *exact* success field exists and has the expected value; on a missing key it increments `schema_mismatch:{tool}:{brand_hash}` and returns `ok=false`.
- Extend [`core/tool_boundary.py`](../core/tool_boundary.py) from input-validation to **output-contract**: every write-path tool's return passes through the validator at the single `ToolExecutor.execute` seam.
- Rewire [`tool_executor.py:133`](../core/tool_executor.py#L133): `_track(success=result.ok)` instead of "didn't raise."

**Effort: L — and it is on the critical path days before launch.** This is not M. It touches all 8 write-path tools' return shapes, rewrites `_track`, hardens `check_rentok_response` into a per-endpoint validator (a contract-doc-to-code mapping that does not exist yet), extends `tool_boundary.py` to output validation, and wires `committed` into idempotency — roughly 3–4 distinct mechanical refactors across ~12 files. **Schedule risk is real; treat this as the one pre-launch item that can slip and plan the launch date around it, not the reverse.** De-risk by landing the validator + `_track` rewire for the 3 money-path tools first (reserve, payment, schedule_visit), then fan out to the rest.

**Success bar:** every write-path tool (search, shortlist, schedule_visit, schedule_call, reserve, payment, cancel, kyc) returns the envelope; `_track` derives from `ok`; a forced wrong-success-key in any tool flips that tool's dashboard `failure_rate` to ~100% AND increments `schema_mismatch`; a simulated `ok=false ∧ committed=true` does **not** trigger a second RentOk write and instead routes to reconciliation.

---

### 1b. Redis-shim `__all__` hardening (split out of the keystone — ships independently)
**Why.** This is a real fix but it is an *unrelated concern* that should not ride on the time-critical keystone. The hand-maintained import list in [`db/redis_store.py`](../db/redis_store.py) silently no-op'd `idem_clear` twice — "be careful with the list" does not hold. But it neither blocks nor depends on the envelope, so it ships on its own track.

**How.** Replace the hand-maintained import list with `from db.redis import *` driven by an explicit `__all__` in each `db/redis/*` module, **plus** a one-line CI test asserting `set(public symbols across db/redis/*) ⊆ dir(db.redis_store)`.

**Effort:** S. **Success bar:** the shim CI test fails if any domain symbol is dropped from the re-export surface.

---

### 2. Fixture-pinned contract tests in the CI gate (close the 100%-broke-undetected class)
**Why.** The hermetic CI uses `FakeEngine` — it *structurally cannot* catch a real RentOk contract change (Block 3, Block 5 §8). The fixture suite catches *your parsing* of a *stable* response, which is exactly the bug shape that hit you three times. Highest-ROI, lowest-effort move named independently by Blocks 2, 5, and 6.

**How.**
- Record real RentOk responses (search, details, rooms, shortlist, reserve, schedule, payment-verify) as JSON fixtures under `tests/fixtures/rentok/`.
- For each write-path tool, a test replays the fixture through the validator from Initiative 1 and asserts `result.ok == true ⟺ the fixture actually confirmed`. Seed permanent regression cases for all three shipped bugs (shortlist inner-`status` not `success`; `p_personal_contact` not `p_phone_number`; fire-and-forget CRM partial-failure).
- Add these to [`ci.yml`](../.github/workflows/ci.yml) — gate count goes from 7→8 hermetic test files.

**The drift gap you must name honestly, not paper over.** Fixtures are frozen at record-time, so they catch *your parsing* but **not future RentOk drift**. The usual answer — a post-deploy canary against a sandbox — does not cleanly apply here (see Initiative 5 and the box below): you are a black-box client with no stated sandbox tenant, so the canary cannot safely exercise writes. **Conclusion to state plainly: you currently have no safe automated mechanism that catches a RentOk *write-contract* change before a real user hits it.** The mitigations that *do* apply are (a) the runtime `schema_mismatch` counter + alert from Initiative 3 (catches drift the moment the first real call mis-parses, in production, within minutes) and (b) a read-only canary (search/details). Accept this as a known, named gap rather than implying the canary closes it.

**Effort:** S–M. **Success bar:** reintroducing any of the three historical bugs turns CI red; a deliberate "rename the success key in the fixture" turns CI red.

---

> ### ⚠️ Black-box-client-of-RentOk reality (applies to Initiatives 2, 3, 5)
> You are a **black-box client** of `apiv2.rentok.com`. There is **no stated sandbox/staging tenant**, and hammering production RentOk on every deploy creates **real bookings/leads/payments** or gets you rate-limited or blocked.
> **Resolution adopted in this roadmap:** until a sandbox is confirmed to exist, every automated live-API probe is **read-only** (`search` / `fetch_property_details` only — never reserve/schedule/payment). The consequence, stated honestly: a read-only canary **cannot verify the write path it would exist to protect.** Write-contract drift is therefore caught only *reactively* in production by the `schema_mismatch` counter + alert (Initiative 3), not *proactively* before a user hits it.
> **Action item:** confirm with the RentOk team whether a sandbox brand/tenant exists. If one does, promote the canary to exercise the full write path against it and this gap closes. Treat "is there a sandbox?" as a launch-blocking question to ask, not an assumption.

---

### 3. Push alerting + reconciliation + schema-mismatch counter (make observability catch silent success)
**Why.** You *already have* the dashboard tile (Block 6 scored this 3/5, not 1/5) — building it again is partly redundant and dangerous: without Initiative 1, the tile you'd build would have rendered `0%` failure during the shortlist outage. The gap is **the success predicate (fixed by Init 1) + push, not pull**. A dashboard is invisible until someone opens the tab; at launch a 100%-broken money flow must page you.

**Reconciliation ships first and standalone (it does not wait on Initiative 1).**
- **Reconciliation metric** (Block 3's "single best silent-success detector"): bot-said-booked vs Postgres-actually-has-a-lead-row for `schedule_visit`/`reserve`. Independent of the tool's self-report — catches the fire-and-forget class *even if the envelope is wrong or not yet built*. Because it has no dependency on the validator, build it **before** the keystone as the cheapest pre-launch silent-success win. Compute the delta on a schedule, alert on breach.

**The rest layers on Initiative 1:**
- **Schema-mismatch counter** from Initiative 1 surfaced in `/admin/analytics` — "this single counter would have screamed on day one for both shipped bugs," and (per Initiative 2) it is your *only* automated detector of live RentOk write-contract drift, so it is load-bearing, not nice-to-have.
- **One Slack webhook** (there is *zero* alerting today — Block 6 confirms no Sentry/Slack/PagerDuty) on: `failure_rate` spike (from `ok`), `schema_mismatch > 0`, reconciliation delta > 0.
- **Deep `/health`:** upgrade [public.py:18](../routers/public.py#L18) from liveness to a Redis + Postgres probe, plus a **read-only** RentOk-reachability ping (a `search`-class call, never a write — per the black-box box above).
- **Post-deploy delta alert (the top-1% alarm):** "page when any write-tool `ok`-rate drops more than **20 points** within 15 min of a deploy." A relative delta fires minutes after a bad deploy where a static threshold on a slowly-degrading flow never trips.

**Effort:** M (reconciliation slice is S and ships first). **Success bar:** a deliberately broken write tool pushes a Slack alert within minutes (not "when someone opens the dashboard"); the reconciliation alert fires on a forced bot-said-booked-but-no-lead-row case *with the validator disabled*; `/health` returns unhealthy when Redis is down.

---

### 4. Error-analysis flywheel + golden set (eval as operating discipline, not just a gate)
**Why.** This is the gap the proposal misses most structurally (Gaps Rank 1). A regression suite is a backward-looking *lock*. Hamel (30+ companies): "products that fail share one root cause — no robust eval system," and the system *starts with error analysis, not metrics*. The proposal has no mechanism that produces the *next* eval case. Every 100%-flow bug would have been caught by a human reading 50 real shortlist traces.

**One corpus, not three.** This roadmap uses a **single bootstrap synthetic corpus** and promotes a **golden subset** from it — there is no separate hand-picked sample size per initiative.
- **Bootstrap corpus (~50 synthetic transcripts)** on a dimension grid (gender × budget × area × channel × language × intent + adversarial/injection). 90 minutes reading them, open-code one note per failure, cluster into a taxonomy; **each recurring category becomes one assertion.** That category count is your roadmap.
- **Golden subset (~40 cases promoted from the corpus)**, stratified by channel × brand × language × intent, seeded with every shipped bug from MEMORY.md (shortlist key, fire-and-forget, empty-contact, S14 empathy, S19 vague-destination).
- The **same corpus** is reused by Initiative 5's tool-selection eval (labeled for correct tool/skill) — do not invent a new sample.

**Cadence stated as an honest minimum, not process theater.** A "weekly calendar block" will be the first thing dropped during launch firefighting. The real rule: **run error analysis whenever ≥30 new real traces have accumulated since the last pass** (volume-triggered, not calendar-triggered). Pre-launch that trigger fires once on the synthetic corpus; post-launch it fires as traffic dictates. Every prod incident becomes a permanent golden case immediately, independent of the trace-volume trigger.

**Effort:** M (one afternoon to stand up; volume-triggered thereafter). **Success bar:** a written failure taxonomy with per-category counts; every recurring category has ≥1 assertion; every prod incident becomes a permanent golden case.

---

### 5. Split the stress suite now; Haiku tool-selection eval later
**Why.** Two corrections the research forces. **(a)** Your stress suite flaps on S04/S08 *because it's a quality eval run as a binary gate* — Block 2 (Notion's Sarah Sachs) and Block 5 hammer that regression evals ≠ frontier evals, and "auto-retrying a blocking test until it passes is how you re-introduce the exact shipped-undetected failure." **(b)** Block 1's strongest finding: tool-selection accuracy is fragile, position-biased, and worst on *cheap models + growing catalogs* — **both your conditions** (Haiku + ~24 tools). "Good descriptions" is not a defense; you must measure.

This initiative is **split across two phases** because the full version is an L that cannot realistically land in the two weeks after a launch you are firefighting:

**5a — Split the suite + quarantine flappers (S, post-launch week 1–2).**
- **Split by determinism.** Deterministic slice (right tool/skill selected? price cited exists? gender filter held? no fabricated number?) → assertion evals, **hard-gate** in CI. Graded slice (empathy phrasing, CTA persuasion, objection handling) → **post to the PR as a comment, do not gate.** Quarantine S04/S08-class flappers; never retry-to-green.
- Turn the deterministic slice of `stress_test_broker.py` / `test_dynamic_skills.py` (which already does the analytics-delta skill-verification primitive) into the gate.

**5b — Haiku tool-selection eval + Prototype→Eval→Collaborate (L, "Then").**
- Label the **same bootstrap corpus** (Initiative 4) for correct tool/skill and measure tool-selection pass-rate **on Haiku specifically** (the cheap-model + growing-catalog danger zone). Establish a baseline to regress against.
- Then run Anthropic's **Prototype→Eval→Collaborate** loop — let Claude read the transcripts and rewrite the tool schemas/descriptions against the eval (Block 1 §2 #7). This is the multiplier the proposal leaves on the table, but it is *not* a launch-week task.
- Optional graded-judge machinery (binary + critique, sampled N=5, gate on ≥4/5 with a bootstrap-CI-vs-baseline test) also lives here, not in 5a — the LLM-judge/bootstrap-CI build alone is ~a week.

**Black-box canary:** a **read-only** search→details probe against the live API on every deploy (per the black-box box — it cannot exercise writes, and that limitation is named in Initiative 2).

**Effort:** 5a = S; 5b = L. **Success bar:** 5a — CI gate is 100%-green-deterministic + a separate non-blocking graded PR comment; S04/S08 no longer block. 5b — a measured Haiku tool-selection pass-rate exists with a baseline to regress against.

---

### 6. Provenance assertion — honesty as code, not prose
**Why.** Your honesty wins (Wave A, Phase 0: no fabricated scarcity/savings, grounded "beds left") are enforced **only by prose** in `selling.md`/`_base.md` with no eval. One prompt regression and the guarantee silently evaporates — and a naïve LLM-judge *cannot* catch it (Block 2's "Style Outweighs Substance," arXiv 2409.15268: judges *reward* fluent confident fabrication, exactly a broker bot's failure mode). This is your biggest *liability* surface — a hallucinated rent is a broken promise at the door (Block 4 §2).

**How.** A post-generation provenance check on write/advisory turns: extract every number the bot emits (price, deposit %, distance, bed count, scarcity), assert each appears in that turn's tool outputs / `[KNOWLEDGE BASE]` block. This is your DEFERRED "Phase 1 provenance guard" — the one honesty control that survives a prompt regression.

**Pre-launch needs a minimal golden harness, so pull one in.** The check is implemented as an **offline assertion over a small provenance golden subset** — a ~10-case slice carved from Initiative 4's bootstrap corpus, specifically the turns that emit numbers. To keep Initiative 6 genuinely pre-launchable, **that minimal harness (corpus slice + assertion runner) is pulled into the pre-launch bucket** rather than waiting for the full Initiative 4 flywheel. The full flywheel still lands post-launch; only the thin number-emitting slice is needed before launch. Later, run the same check online on sampled live traffic.

**Effort:** M (including the thin pre-launch harness slice). **Success bar:** a prompt edit that lets the bot state an unsourced ₹ figure turns the offline eval red; sampled live turns flag <1% ungrounded-number rate.

---

### 7. Per-turn Sonnet escalation on the known-bad turn types (ship narrow now, tune later)
**Why.** Block 4 §6 independently arrives at the tiering recommendation. But the S08 CTA-flap on commit turns is a **known, documented, money-losing failure happening now on Haiku** — waiting for a measured per-turn delta before escalating the obvious commit/objection turns is over-engineering a fix you already know you need. You already have the plumbing: `supervisor.route` returns `skills`.

**Ship the narrow escalation pre-launch; tune the line with data later.**
- **Pre-launch (S):** map a *small, explicit* set of known-high-stakes skills `{selling, compare, booking-commit}` → Sonnet override **for that turn only.** This directly targets the documented S08 flap on the revenue path. Keep the list deliberately tiny — Block 4 documents "router collapse → always-expensive" as the failure mode.
- **Later (depends on Initiative 5b):** use the measured per-turn-type Haiku↔Sonnet quality delta to *tune* the line — add or remove turn types only where Haiku's drop crosses tolerance AND the turn is high-value. Measurement refines the list; it does not gate the initial known-good escalation.

**Effort:** S (plumbing) pre-launch; tuning depends on Init 5b. **Success bar:** S08-class CTA flap disappears on commit turns; cost-per-conversation rises **<10%** over the pre-escalation baseline (you have the per-agent cost counters to measure this — set the baseline before shipping).

---

### 8. Resolve raw `pg_id`/`p_id` to a 0-indexed handle + brand-ownership check (a known-active footgun, not future polish)
**Why.** Block 1 #2/#8: Anthropic says resolving UUIDs to 0-indexed IDs "significantly improves precision and reduces hallucinations," and weak models (Haiku) are worst at exactly this. You hand Haiku **two confusable opaque IDs** — `p_pg_id` (Firebase UID; KB/images/shortlist) vs `p_id` (UUIDv4; booking) — which MEMORY.md flags as a **live footgun that already caused a KB-retrieval miss.** A swap doesn't throw; it silently retrieves nothing or books the wrong unit — the same silent-failure shape as the shortlist bug, but inside the model's reasoning. This is a *known-active* defect, not a hypothetical-future protection, so it is **not** "scale & polish."

**Split by urgency:**
- **Pre-launch (S, security):** the **brand-pg_id ownership check** — any `pg_id` in a booking/shortlist tool call must be in the *calling brand's* `pg_ids` list. This is the same ownership check your admin routes already do (Block 5 §6), so reuse is cheap, and it closes a cross-brand-access path on the largest recent surface (multi-brand isolation). Treat as a pre-launch security item.
- **Post-launch (M):** the `property[0..N]` handle remap — map properties to `property[0..N]` at the tool boundary, resolve back to the real ID internally, never surface raw IDs in tool args or results.

**Effort:** ownership check S (pre-launch); handle remap M (post-launch). **Success bar:** a cross-brand `pg_id` in a tool call is rejected (pre-launch); no raw `pg_id`/`p_id` appears in any tool arg or result the model sees (post-launch).

---

### 9. Latency: skip the supervisor LLM call on clear refinements + instant ack (pre-launch — TTFT is the felt metric)
**Why.** Every turn pays a blocking `classify()` Haiku round-trip of pure dead air before streaming starts (Block 6 dim 4; Block 4 §1). TTFT is the metric users feel (<2s; sub-300ms ack = conversational). For an imminent launch this is an **S-effort pure-latency win on existing plumbing** (last-agent stickiness + keyword safety net already exist) — so the refinement-skip belongs **pre-launch**, not week 2.

**How.** On a clear refinement ("cheaper", "show more") after a broker turn, **skip the supervisor LLM call entirely** and route by keyword. On WhatsApp (no streaming), send an instant ack ("Searching Kurla now…") *before* the tool round-trip — ensure the Phase-B 2s debounce delays only the heavy pipeline, not the ack. Add a TTFT metric.

**Effort:** S–M. **Success bar:** refinement turns skip `classify()`; WhatsApp ack lands <1s; TTFT p95 tracked.

---

### 10. Summarizer coherence test (pre-launch) + eval-gate the legacy fallback (keep it through launch)
**Why.** The summarizer runs at 30 msgs and rewrites history — an **untested prompt** that can silently drop a hard constraint (gender/budget) after turn 30, invisible to every single-turn test (Block 5 §4, "highest-leverage untested surface"). Separately, you carry the legacy monolith + deferred KYC scaffolding behind flags.

**Summarizer test — pre-launch.** One golden multi-turn case that runs past the 30-msg compaction boundary and asserts gender + budget + chosen property survived.

**Do NOT delete the legacy monolith the week you launch the thing it rolls back.** `DYNAMIC_SKILLS_ENABLED=true` is the live path, and the dynamic-skill system is itself the *riskiest, least-traffic-tested* pre-launch surface. The legacy monolith is its documented instant-rollback. "Git is your rollback" is glib when the new system has never run under real traffic — deleting your rollback the week you ship its replacement is the opposite of pre-launch discipline. **Keep the flag through launch + stabilization; eval-gate the fallback** so it is not itself broken when reached (the lesson of the `idem_clear` silent no-op). Wire the `DYNAMIC_SKILLS_ENABLED=false` legacy path into an eval that runs every CI pass. **Revisit deletion only after the dynamic system has proven itself under real traffic for a defined stabilization window.** The deferred-KYC prompt scaffolding (no real path, two blocking deps) is a separate case — *that* can be deleted now, since it has never been live and git holds it.

**Effort:** S. **Success bar:** the compaction-survival case is green and in CI; the legacy monolith path runs under a CI eval every pass; deferred-KYC scaffolding is removed.

---

### 11. Daily-cost circuit breaker (pre-launch — solo-operator blast-radius cap)
**Why.** You track per-agent and daily cost ([db/redis/analytics.py](../db/redis/analytics.py): `increment_daily_cost`/`get_daily_cost`) but there is **no kill-switch** if a tool loop or a bad deploy spikes spend. For a solo operator launching, a runaway Haiku tool-loop or a `web_search` storm overnight is a real, unbounded failure mode — and the existing 120s LLM ceiling does not cap *aggregate* daily spend. This is the cost analogue of the silent-success risk: invisible until the bill arrives.

**How.** On the existing `daily_cost:{brand_hash}:{day}` counter, add a configurable ceiling; when breached, degrade gracefully (disable `web_search`, cap tool-loop iterations, or fall back to a static "we'll get back to you" path) and fire the same Slack alert from Initiative 3. Cheap because the counter already exists.

**Effort:** S. **Success bar:** a simulated spend spike past the ceiling trips the breaker and pages Slack; normal traffic never trips it.

---

### 12. Brand-config seeding correctness as a launch gate (pre-launch — largest recent surface)
**Why.** Multi-brand isolation is the **largest recent surface** and the place a silent error cross-contaminates data: a mis-seeded `brand_hash`, a `brand_token` collision, or overlapping `pg_ids` where they should be disjoint silently leaks one brand's data into another's — exactly the failure class the whole isolation effort exists to prevent. There is currently no assertion that the seed (`main.py` lifespan `_SEED_BRANDS`) is internally consistent.

**How.** One pre-launch assertion test over the seeded brand configs: every seeded brand's `brand_hash` is unique; `pg_ids` are disjoint across brands where expected; each `brand_token:{uuid}` resolves back to exactly its brand's hash; `get_brand_config_by_hash` round-trips. Cheap, and it runs on the data that gates every brand-scoped read/write.

**Effort:** S. **Success bar:** the seeding-consistency test is green in CI; a deliberately duplicated brand_hash or colliding token turns it red.

---

## Phasing (respecting an imminent launch)

### Before launch (must-haves to not embarrass yourself)
The bar here: **do not ship a money/booking flow that can lie about success — or double-charge on retry — with no alarm.**

1. **Initiative 3 (reconciliation slice only)** — bot-said-booked vs Postgres-lead-row delta + alert. Ships **first**, standalone, *before* the keystone (it does not depend on the validator) — your cheapest pre-launch silent-success win.
2. **Initiative 1** — envelope + central validator + `committed`/idempotency money-safety wiring (keystone). **L, on the critical path — plan the date around it.** Land the 3 money-path tools first.
3. **Initiative 1b** — redis-shim `__all__` hardening (independent track, S).
4. **Initiative 2** — fixture-pinned contract tests in CI for all write-path tools.
5. **Initiative 3 (remainder)** — schema-mismatch counter + ONE Slack alert + post-deploy delta alert + deep `/health` (read-only RentOk ping).
6. **Initiative 6** — provenance assertion as an offline check, with the thin number-emitting golden slice pulled into pre-launch.
7. **Initiative 7 (narrow)** — Sonnet escalation on `{selling, compare, booking-commit}` only.
8. **Initiative 8 (ownership check only)** — brand-pg_id membership check on booking/shortlist tools (security).
9. **Initiative 9** — supervisor-skip on refinements + instant WhatsApp ack + TTFT metric.
10. **Initiative 10 (summarizer test + eval-gate fallback)** — compaction-survival case; eval-gate (do **not** delete) the legacy monolith; delete only the never-live KYC scaffolding.
11. **Initiative 11** — daily-cost circuit breaker.
12. **Initiative 12** — brand-config seeding-consistency assertion.

Most of these are S-effort on existing Redis/Postgres/pipeline; **Initiative 1 is the one L and the schedule pivot.** Also resolve the launch-blocking question in the black-box box: **does a RentOk sandbox exist?**

### First 2 weeks post-launch (the measurement/eval flywheel)
- **Initiative 4** — error-analysis flywheel on now-real traces + golden set; volume-triggered (≥30 new traces), not calendar-ritual.
- **Initiative 5a** — split the stress suite (deterministic gate / graded PR comment) + quarantine flappers. **S only** — the L machinery is deferred to "Then."
- **Initiative 8 (handle remap)** — `property[0..N]` boundary remap.

### Then (scale & polish)
- **Initiative 5b** — Haiku tool-selection eval on the shared corpus + Prototype→Eval→Collaborate loop + optional graded-judge/bootstrap-CI machinery (the L).
- **Initiative 7 (tuning)** — refine the escalation list from Initiative 5b's measured per-turn delta.
- **Initiative 10 (revisit deletion)** — after a defined stabilization window of the dynamic-skill system under real traffic, reconsider deleting the legacy monolith.
- Read-only post-deploy canary hardened; promote to full write-path canary **iff** a RentOk sandbox is confirmed.
- North-star outcome metric: lead → visit → **show-up** (brand-scoped), not "conversations."
- Online LLM-judge on sampled live traffic for grounding/hallucination.

---

## What NOT to do (explicit non-goals)

- **Don't add tools.** Block 1 §1: tool-count → accuracy cliff (7%–85% drop in LongFuncEval), worst on Haiku. Adding a tool is negative-EV right now. *Corollary:* consolidating two confusable tools (e.g. `check_reserve_bed`/`reserve_bed`) into one workflow-shaped tool is **not** "adding tools" — don't let this guardrail block that consolidation.
- **Don't polish the ~19 off-path tools** (landmarks, nearby_places, compare, events, brand_info, web_search). Just cap their latency. They are not on the revenue path.
- **Don't build a second observability dashboard.** You already have the tile ([analytics.py:305](../db/redis/analytics.py#L305)) — fixing the success *predicate* (Init 1) makes the existing one trustworthy. Building a new tile on the old predicate ships a dashboard that confidently lies.
- **Don't gate CI on a graded/judge score or a single stochastic run.** That re-introduces the retry-to-green path that hides real regressions. Gate the deterministic slice; post the graded slice to the PR.
- **Don't delete the legacy broker monolith the week you launch its replacement.** Git is your rollback for *proven* code; the dynamic-skill system is unproven under real traffic and the monolith is its only fallback. Eval-gate it through launch + stabilization, then revisit. (Deferred-KYC scaffolding, never live, *can* go now.)
- **Don't blind-retry a write on `ok=false`.** If `committed` is `true` or `None`, a retry double-books/double-charges. Route to human reconciliation, not an automatic retry.
- **Don't assume the post-deploy canary protects the write path.** As a black-box client with no confirmed sandbox, the canary is read-only and cannot exercise writes; write-contract drift is caught only reactively by the `schema_mismatch` alert. Don't let the canary's existence create false confidence.
- **Don't adopt a multi-agent framework / planner / more MCP servers.** Block 1 §7: the "90% better multi-agent" number is for parallel read-heavy research, not transactional bots. Your routing + linear tool loop is the correct altitude.
- **Don't build the KYC flow now.** It's DEFERRED with two real blocking deps (real tenant firebase_id, per-property credits). Delete the scaffolding; don't half-build it.
- **Don't chase vendor "40–60% WhatsApp conversion" numbers.** Block 4: those count a reply as a conversion. Measure honest show-up rate.

---

## The one thing if you do nothing else

Build the **central contract-validator that computes a typed `{ok, committed}` from the RentOk payload (Initiative 1)** — but ship the **reconciliation delta (Initiative 3's first slice) in parallel and ahead of it**, because that one control needs no validator and is the cheapest standalone catch for the fire-and-forget class.

The validator is one mechanism with four payoffs (gates the merge, computes the envelope, feeds the honest dashboard, grounds the eval), and its `committed` field is the money-safety lever that stops a parse-failure retry from double-charging. The code proves why it must come first: `_track(True)` fires on any non-raising return ([tool_executor.py:133](../core/tool_executor.py#L133)), so the success tile you'd otherwise trust rendered `0%` failure during a 100%-broken flow. Everything else is correct prioritization — this is the sequencing correction, plus the honest caveat that as a black-box RentOk client with no confirmed sandbox, no automated check catches a *write-contract* drift before a real user does; only the runtime `schema_mismatch` alert catches it, in minutes, after the fact.
