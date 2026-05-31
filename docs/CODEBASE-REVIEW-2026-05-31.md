# Thermonuclear Codebase Review — EazyPG AI Booking Bot

**Date:** 2026-05-31
**Method:** 17-agent multi-agent workflow — 8 dimensions reviewed independently, each adversarially calibrated by a second agent, then synthesized into a single percentile verdict (~1.45M tokens).

---

## Verdict

| | |
|---|---|
| **Overall score** | **59 / 100** |
| **Percentile band** | **Top 15–25%** |
| **Top 1%?** | **No** — and the gap is not close |

This is genuinely strong solo-built, revenue-generating product engineering — clearly above the median professional codebase. But it is held back by three structural gaps that all sit in the heaviest-weighted dimensions: **security/multi-tenant isolation, event-loop-blocking IO, and the absence of a real automated test net.** These are exactly the gaps that separate "very good" from FAANG-caliber.

### Dimension scorecard (calibrated)

| Dimension | Score | Verdict |
|---|---|---|
| Performance & LLM Cost Efficiency | **71** | Strongest |
| Architecture & Modularity | **70** | Strong |
| Frontend Quality & Safety | **70** | Strong |
| Error Handling & Resilience | **66** | Solid |
| Code Quality & Maintainability | **64** | Solid |
| Concurrency & State Correctness | **58** | Real debt |
| Testing Quality & Coverage | **45** | Weak |
| Security & Multi-Tenant Isolation | **44** | **Anchor / weakest link** |

---

## Top strengths

- **Clean, intentional architecture:** declarative tool registry, pure-function 3-phase router, file-based dynamic skill system with two-block prompt caching, thin lifespan-based app factory, layered 3-tier KB injection chain with explicit fallback contracts.
- **Deliberate LLM cost engineering:** Haiku on all high-frequency paths (routing, summarization, broker agent), ephemeral prompt caching split into cached-base/uncached-skill blocks, hierarchical summarization, fire-and-forget cost tracking off the latency path.
- **Mature error handling for two-phase operations:** honest partial-failure surfacing on booking+CRM flows rather than false success; retry utility with correct exception taxonomy; ToolExecutor that falls back to cached property data and never propagates uncaught exceptions to Claude.
- **Several genuinely hard distributed-concurrency problems solved correctly:** per-user SET NX drain lock, wamid-based dedup with correct TTL, Phase C cancellation signal, textbook atomic pipelined sliding-window rate limiter.
- **Working multi-brand isolation *design*** (SHA-256 brand_hash namespacing, brand-scoped Redis keys, property ownership checks, parameterized SQL throughout) and a thoughtful SSE interrupt pattern (requestCounter + AbortController) on the frontend.

## Top gaps

- **Security/multi-tenant isolation is the weakest link:** no Meta HMAC webhook verification, auth disabled by default (`API_KEY=None` no-op), hardcoded admin key in source, and client-controlled/offline-derivable tenant identity on the `/chat` web channel that defeats the brand isolation the product is built on.
- **Systemic synchronous Redis client on the asyncio event loop**, unwrapped on the hot path (conversation load/save, user-memory RMW, per-property scoring N+1) — blocks the loop on every turn and undermines the parallel-execution claims.
- **No automated test safety net:** zero offline/deterministic mode, suites hit and mutate production CRM/analytics, keyword-presence assertions, no CI gate, and the multi-tenant isolation surface has no dedicated test.
- **Real correctness debt under concurrency:** non-atomic wamid check-then-set, non-atomic queue drain, read-modify-write on user memory and brand flags without CAS, non-atomic conversation-history append — lost updates and duplicate processing at realistic load.
- **Duplication/maintainability debt that compounds:** `run_pipeline` vs `_route_agent` and `run_agent` vs `run_agent_stream` are verbatim duplicates (already diverged); the property-listing regex is triplicated across parser/UI (with confirmed silent drift); `format_prompt` builds 4 workflow branches by hand-concatenated, order-dependent `string.replace()`.

---

## Prioritized improvement roadmap

1. **[High impact / M] Enforce real auth boundaries.** Add Meta `X-Hub-Signature-256` HMAC verification as non-optional middleware on all webhook routes; make `API_KEY` a required (non-Optional) config; remove the hardcoded `OxOtel1234` seed key; stop trusting client-supplied `brand_hash`/`pg_ids` on `/chat` — derive tenant identity server-side from a signed token.
2. **[High / M] Fix event-loop-blocking IO.** Migrate the Redis layer to `redis.asyncio` (or wrap all hot-path calls in `asyncio.to_thread`) and pipeline the per-property scoring reads — eliminates blocking on every turn and fixes the N+1.
3. **[High / M] Make critical shared-state writes atomic.** Single `SET NX EX` for wamid dedup; `MULTI/EXEC` or `LMPOP` for queue drain; Lua/WATCH CAS for user-memory and brand-flag updates; namespace followups per-user to kill the O(N) global scan.
4. **[High / L] Build an offline deterministic test suite.** docker-compose Redis/Postgres, mocked HTTP (respx/recording), a mocked LLM client, tool-call-assertion routing tests, and a dedicated two-brand isolation fixture asserting no cross-tenant leakage — gate on CI before deploy.
5. **[Medium / M] Pay down structural duplication.** Extract a shared core for `run_pipeline`/`_route_agent` and `run_agent`/`run_agent_stream`; centralize the property-listing regex into one parser module; rewrite `format_prompt`'s 4 workflow branches with a composable template.
6. **[Medium / M] Harden remaining resilience + frontend gaps.** Add `raise_for_status` + retry on the KYC OTP path; route cancel/reschedule through the retry helper; add a circuit-breaker/aggregate per-turn timeout for the Rentok dependency; escape the Leaflet popup property-name XSS sink; set an explicit DOMPurify `ALLOWED_TAGS` allowlist; add a CSP.

---

## Per-dimension detail
<!-- Generated from the 8 calibrated reviewer reports. Each score is post-calibration. -->

### Architecture & Modularity — 70/100 (reviewer 66)

The codebase is above-average and clearly authored by someone who understands the domain. It has genuine strengths: a clean pipeline spine, consistent agent/tool separation, a working multi-brand isolation design, and a skill-based dynamic prompt architecture that shows real product thinking. The gaps that prevent a higher score are not cosmetic — they are structural decisions that compound under load and scale: a shared mutable singleton used as a concurrency switch, a fully duplicated routing pipeline, a monolithic prompt registry, and business logic leaking into the HTTP layer.

**Critical/High findings:**
- **Shared mutable singleton swapped per-request is a concurrency hazard** (high) — `broker_agent.py:206-208`, `booking_agent.py:47-48`, `profile_agent.py:46-48`, `default_agent.py:46-48`: all four agents do `engine.tool_executor = cfg['executor']` then `finally: engine.tool_executor = original_executor`. The engine is a process-level singleton — concurrent requests race on this mutation.
- **run_pipeline and _route_agent are verbatim duplicates** (high) — `core/pipeline.py:36-176` (run_pipeline) and `179-218` (_route_agent) implement the same supervisor→safety-net→skill-detection→analytics sequence twice; the code comment even admits it.

### Code Quality & Maintainability — 64/100 (reviewer 62)

Above-average, functional Python with clear architectural intentions and real care for developer experience (the CLAUDE.md file map, named constants, module docstrings). Falls short of elite caliber on three structural axes: verbatim-duplicated regex logic, 150–200 line functions doing 5–7 jobs, and a 170-line hardcoded multi-branch prompt factory built via raw `string.replace()`.

**Critical/High findings:**
- **Property-name regex duplicated verbatim across three files** (high) — the bold pattern appears in `message_parser.py:39`, `ui_parts.py:97`, and `ui_parts.py:157-158`; the H3 pattern in `message_parser.py:55`. Already drifting silently.
- **search_properties() is a ~280-line function doing 6 distinct jobs** (high) — `search.py:206-484`: payload build, progressive relaxation + dedup, image/geocode enrichment, scoring/sort, result building, memory write — all in one function.
- **format_prompt() assembles booking workflows via raw string concat in a 170-line if/elif block** (high) — `prompts.py:634-740`: four nearly-identical workflow strings, order-dependent, with copy-pasted shared steps.

### Security & Multi-Tenant Isolation — 44/100 (reviewer 52)

Thoughtful design (SHA-256 brand_hash isolation, brand-scoped keys, ownership checks, fully parameterized SQL) undermined by a cluster of real production risks.

**Critical/High findings:**
- **No Meta webhook signature verification — any attacker can forge WhatsApp messages** (critical) — `routers/webhooks.py:150-162` (GET) and `:169-287` (POST): no `X-Hub-Signature-256` HMAC validation; the only guard is `verify_api_key`, which is a no-op by default.
- **API_KEY defaults to None — verify_api_key is a no-op; webhook + cron endpoints unprotected by default** (high) — `config.py:43-44` (`API_KEY: Optional[str] = None`), `core/auth.py:24-26` (`if not expected: return`).
- **Hardcoded API key 'OxOtel1234' in main.py source** (high) — `main.py:37` in `_SEED_BRANDS`; trivially guessable per the documented `{BrandName}1234` convention; controls all OxOtel admin access.
- **_require_ownership has a 'lenient' bypass for untagged users** (high) — `admin.py:96-105`: raises only when `user_brand` is set AND differs; `None` (legacy/untagged) users are world-accessible to any brand admin.
- **WhatsApp access tokens stored plaintext in Redis brand_config** (high) — `db/redis/brand.py:34-45` serializes the full config including `whatsapp_access_token`; exposed on any Redis compromise.

### Error Handling & Resilience — 66/100 (reviewer 68)

Deliberate, above-average, progressively improved (documented bug fixes across sprints). Solid retry infra, well-reasoned partial-failure surfacing, good ToolExecutor fallback-to-cache. Held below 75 by silent status discards, bare `except Exception`, no circuit-breaker/timeout budget for Rentok, and inconsistent retry application.

**Critical/High findings:**
- **verify_kyc silently ignores HTTP status before reading the OTP body** (high) — `kyc.py:117-124`: `resp.json()` with no `raise_for_status()`; a 400/422/500 from the OTP endpoint is silently treated as a parseable response.

### Concurrency & State Correctness — 58/100 (reviewer 62)

Several hard distributed problems solved correctly (SET NX drain lock, wamid dedup, Phase C cancellation, atomic sliding-window limiter), but a cluster of real races and TTL gaps a top-1% codebase would not ship.

**Critical/High findings:**
- **Non-atomic wamid dedup: check-then-set race allows duplicate processing** (high) — `db/redis/conversation.py:128-130`: `is_wamid_seen` (EXISTS) and `set_wamid_seen` (SET EX) are separate; Meta's rapid-retry can slip a duplicate through.
- **wa_queue_drain is not atomic** (high) — `db/redis/conversation.py:154-159`: serial `LPOP` loop instead of `LMPOP`/`GETDEL`; vulnerable if two drainers overlap (2-min lock TTL + crash).
- **update_user_memory uses read-modify-write without optimistic locking** (high) — `db/redis/user.py:232-257`: GET → mutate → SET; parallel `asyncio.gather` tool calls (`claude.py:94`) cause lost updates.

### Testing Quality & Coverage — 45/100 (reviewer 42)

Entirely live-integration + LLM-behavioural. No unit tests, no mocks for booking/payment/brand-isolation, no pytest harness, no CI, no coverage. Assertions are systematically weak ("non-empty + contains one keyword" — a server error can pass). Flakiness accepted as a norm. No deterministic offline run is possible.

**Critical/High findings:**
- **Zero unit/isolated integration tests for all critical revenue paths** (critical) — no `conftest.py`/`pytest.ini`; payment, reserve, KYC, brand isolation, rate limiter, summarizer all untested in isolation.
- **Assertions are keyword presence, not correctness** (high) — `test_comprehensive.py:59-63` (13/16 checks), `e2e.spec.js:141` (`|| response.length > 20`): a 500-error message passes.
- **No offline/deterministic test mode** (high) — every run hits production and costs real money (`stress_test_broker_prod.py:33`, `test_semantic_kb.py:11` hardcode the Render prod URL).
- **LLM non-determinism treated as acceptable debt** (high) — MEMORY.md: "S04/S08 are pure Haiku stochasticity… PASS on retry."
- **The entire multi-brand isolation system has no dedicated test** (high) — no coverage of brand_hash scoping, ownership checks, cross-brand leak prevention, dual-write analytics, or the backfill migration.

### Performance & LLM Cost Efficiency — 71/100 (reviewer 72)

Strongest dimension — deliberate, architected-in cost optimization (Haiku hot paths, ephemeral prompt caching, parallel tool exec, dynamic skill loading, Redis caching with sensible TTLs). Held below top-10% by event-loop-blocking IO and a few waste points.

**Critical/High findings:**
- **Synchronous Redis calls block the async event loop on every turn** (high) — `db/redis/_base.py:43-44` returns a synchronous `redis.Redis`; every `_json_get/_json_set/get/set/hgetall` across all 9 modules blocks inside async handlers. Plus an N+1 in the `search_properties` scoring loop and a fresh Anthropic client per summarization call.

### Frontend Quality & Safety — 70/100 (reviewer 72)

Solidly built, well above average for its complexity. Correct SSE interrupt/ownership pattern, consistent XSS sanitization, clean extensible component registry.

**Critical/High findings:**
- **Streaming content_delta path** (high) — `stream.js:197` injects `safeParse(fullText)` (DOMPurify-wrapped, so sanitized) per incremental chunk into a live DOM element; partial-HTML accumulation is the risk to watch.
- **JSON embedded in HTML attributes via naive single-quote escape** (high) — `server-parts.js:57-59` and `:224`: `JSON.stringify(...).replace(/'/g,'&#39;')` into `data-*` attributes instead of a proper serialization strategy.
