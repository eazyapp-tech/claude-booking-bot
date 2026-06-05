# Top-1% Bot — Master Tracker

**The single source of truth for the EazyPG booking-bot quality engagement.**
Owner: Sanchay (PO) + Claude (driver). Started 2026-06-05.

> **READ THIS FIRST every session.** Before any new work: open this doc, find the
> current focus, check the Done Ledger so we never redo shipped work, pick the next
> open item in order. Update it the moment anything ships. This is how we stay on
> one track across sessions and never drift, duplicate, or lose the thread.

---

## What "done" means — the 5-truth bar

A top-1% booking agent is one where all five hold. Every item below is tagged with
the truth(s) it serves.

| Tag | Truth | Plain meaning |
|---|---|---|
| **H** | Honest | Every outcome it claims is actually true. No silent success on failure. |
| **Rl** | Real | Every booking/lead/token it captures reaches the human who fulfills it. |
| **Rf** | Right-first | It surfaces the best-fit option first, on real data. |
| **In** | Instant | It streams, never dead-airs. 4s of silence = dead. |
| **Gr** | Graceful | It recovers from errors and never fabricates to cover a gap. |

---

## How we sequence

1. **Phase 1 — Bot-only.** Ship purely in the bot repo. Zero backend dependency.
   Low blast radius. **We are here.**
2. **Phase 2 — Paired (bot half).** Build the bot's half of a paired change ahead
   of the backend, behind the consumption point. Activate when the backend lands.
3. **Phase 3 — Backend / live prod.** `rentok-backend` is live production for every
   manager. Bigger blast radius. We touch it last, with specs you approve.

**Cadence:** one PR per item; **you merge** (you hold the prod trigger). Branch
protection + the CI gate guard `main`.

**Status legend:** ⬜ open · ⏳ in PR / in review · ✅ shipped to main · ⏸ parked · 🔵 needs your decision

---

## 🎯 Current focus

- **Last shipped:** **NAME-1** — name capture + personalization ✅ ([#45](https://github.com/5s10r2/claude-booking-bot/pull/45), `8921fb5`). Live-verified: bot captures a volunteered name, uses it by first name across turns, persists it. Preceded by **C1** — OSRM circuit breaker + honest straight-line fallback ✅ ([#43](https://github.com/5s10r2/claude-booking-bot/pull/43)). **Live-verified 4/4** (R1 intact via osrm_get; estimate_commute 30s dead-air eliminated — 0 new timeouts across 3 commute Qs). Preceded by **R1 commute ranking** (marquee) ✅ #37/#38/#39 + eazypg-chat#8, live 10/10.
- **Doing next:** your call — **G-20** (build the per-brand support line — you supply numbers) → **R8** (intent-tuned ranking, needs a design pass first). G-13 verified done; NAME-1 shipped + live-verified.
- **Blocked on you:** **OSRM EC2 restart** (backend/AWS — bot is correct either way now, but a restart auto-upgrades R1 to precise "X min" labels). 2 product decisions (E3, AV-§2) when convenient.

> **Live finding (2026-06-05):** `maps.rentok.com` OSRM is **down at the network level** on prod (EC2 stopped/terminated — confirmed with backend; the bot is the live consumer, backend's own OSRM refs are commented out). **C1 makes the bot correct regardless:** ranks by honest straight-line proximity, skips the dead host instantly via the breaker, self-heals when the EC2 returns. On restore, confirm the bot's Render `OSRM_API_KEY` + the OSRM param name (`api_key` vs `key`) — one live commute search flips labels "~X km" → "X min".

---

## Phase 1 — Bot-only (do these first)

| ID | Item | Truth | Status | PR |
|---|---|---|---|---|
| **B1** | Lead carries rich intent (`remarks` + `room_type`) so manager sees the *why* | Rl Gr | ✅ | [#32](https://github.com/5s10r2/claude-booking-bot/pull/32) |
| **LAT-1** | Run image-enrich + geocode concurrently via `_enrich_top_results` (search.py). *NB: the "geocode+pg_ids" idea was invalid — pg_ids is synchronous.* | In | ✅ | [#34](https://github.com/5s10r2/claude-booking-bot/pull/34) |
| **R1** | Rank by the user's real commute destination, not the search pin. **Highest lift. ✅ LIVE-VERIFIED 10/10.** Reuses `commute_from`; ranks top-10 by office proximity (haversine always + OSRM drive-time upgrade); card shows "X min to <dest>" or honest "~X km from <dest>"; graceful + self-heals. Absent destination → unchanged. 26/26 hermetic. | Rf Rl | ✅ | [#37](https://github.com/5s10r2/claude-booking-bot/pull/37) [#38](https://github.com/5s10r2/claude-booking-bot/pull/38) [#39](https://github.com/5s10r2/claude-booking-bot/pull/39) |
| **C1** | OSRM circuit breaker + honest fallback. **✅ LIVE-VERIFIED 4/4.** `core/osrm.py` skips the dead host for a 10-min cooldown (no per-call timeout tax), probes for recovery, self-heals. estimate_commute/fetch_landmarks now return honest "~X km straight-line" (broker stops fabricating); R1 routed through it too. 18/18 hermetic. | H In Gr | ✅ | [#43](https://github.com/5s10r2/claude-booking-bot/pull/43) |
| **R5** | Outcome-signal load degrades visibly (logs a warning) instead of silently blind | Rf | ✅ | [#35](https://github.com/5s10r2/claude-booking-bot/pull/35) |
| **NAME-1** | Capture + use the user's name (personalization). **✅ LIVE-VERIFIED.** `save_name` tool (web analog of WhatsApp profile name, in ALWAYS_TOOLS) + every conversational agent appends an uncached name directive → addresses the user by first name; absent name = byte-clean prompt. 31/31 hermetic. Prod: "I'm Sanchay" → "Got it, Sanchay! 🙌" → used again next turn → persisted (admin API). | Rl Gr | ✅ | [#45](https://github.com/5s10r2/claude-booking-bot/pull/45) |
| **G-13** | Surface property lat/long. **✅ ALREADY DONE — verify-first found the "formatting only" label was stale.** Coords reach the user three ways: carousel `lat`/`lng` + `map_center` → Leaflet Map View; `google_map` deep link built in search.py:757 + property_details.py:161. No PR needed. | Rl | ✅ | — |
| **G-20** | Surface support contacts. **🔵 NOT formatting-only — product-gated.** Property owner phone is *deliberately* hidden (prompts.py:234 — privacy + lead funnel); no brand support line exists in config. **Decision (2026-06-05): add a per-brand `support_contact` field** (admin-editable). Next bot+config PR; you supply numbers to seed. | Rl | ⬜ | — |
| **P1.7** | KYC generate-failure no longer reported as false success | H | ⏳ | [#30](https://github.com/5s10r2/claude-booking-bot/pull/30) |
| **R8** | Intent-tuned ranking weight profiles per user. **Do after R1 + R5.** | Rf | ⬜ | — |

---

## Phase 2 — Paired (build the bot half ahead, activate on backend)

| ID | Item | Truth | Status | Notes |
|---|---|---|---|---|
| **A1/G-1/G-2/G-7** | Availability + per-sharing price contract: formatter, cache fallback, tool schema carrying `is_available` / `available_from` / `vacant_beds` / `rent_per_bed` | Rl Rf | ⬜ | Consume the moment the availability endpoint lands |
| **E2** | Image guarantee — filter to `images_present=true` now; cover-first ordering + CDN sizing waits on backend | Rl | ⬜ | Bot half shippable now |
| **E4** | Honest empty-vs-error message branch (bot half largely done; backend must signal the two distinctly) | H Gr | ⬜ | |
| **E6** | Quote one authoritative price field once backend names it | H | ⬜ | |
| **R2/R3** | Vacancy-count scoring + move-in-date vs `next_available_from` validation | Rf | ⬜ | Consumes availability endpoint |
| **R6** | Quality/trust term in score (photos, completeness, rating, verified) | Rf | ⬜ | Rides backend trust projection |
| **A2** | Feed `PropertyNearby` JSONB into the commute tool | Rf | ⬜ | |
| **A3** | Food-menu tool + formatting against proposed endpoint | Rl | ⬜ | |
| **A4** | Surface trust signals (`is_verified`, certificates, tenant count) | Rl | ⬜ | |
| **A5** | True move-in cost (deposit + add-ons + included) quoting logic | Rl H | ⬜ | |
| **A7/R4** | Amenity scoring free-text → structured enum mapping | Rf | ⬜ | |
| **P0.1** | Stop optimistic `addPayment`; consume only a verified result | H Rl | ⬜ | Bot consumption path; backend verifies |
| **P0.2** | Drop raw Aadhaar PII transit through the bot | H | ⬜ | Ready to drop when backend stops sending |
| **P0.3** | Inject brand-scoped token on every RentOk call | Rl | ⬜ | Backend enforces later |
| **P1.5** | Call explicit KYC `POST .../init` (read/write split) | H | ⬜ | |
| **P2.1** | Surface reservation "expires at X" | Gr | ⬜ | |
| **P2.3** | Bot-side shortlist dedup-on-append | Rf | ⬜ | |
| **P2.5** | limit/offset params + page-through in bot client | In | ⬜ | |
| **B2** | Reason-aware KYC guidance once backend returns a reason | Gr | ⬜ | |

---

## Phase 3 — Backend / live prod (last; specs you approve)

| ID | Item | Truth | Status | PR |
|---|---|---|---|---|
| **SILO** | **THE true P0.** Bot bookings/leads/token reach the manager + notifications fire. A booking nobody's told about = broken first impression. | Rl | ⬜ | — |
| **P0.1** | Payment gateway verification before recording token | H Rl | ⬜ | — |
| **P0.2** | Stop returning raw Aadhaar PII to the bot | H | ⬜ | — |
| **P0.3** | Auth / brand isolation on `/bookingBot/*` | Rl | ⬜ | — |
| **P0.4** | Gate test-Aadhaar numbers behind non-prod | H | ⬜ | — |
| **P0.5** | KYC gateway Cashfree → QuickEkyc | Gr | ⏳ | backend [#5868](https://github.com/eazyapp-tech/rentok-backend/pull/5868) |
| **P1.1** | `cancel-booking` honest status (not always 200) | H | ⬜ | — |
| **P1.2** | `update-booking` existence check + honest record | H | ⬜ | — |
| **P1.4** | `update-kyc` honest status | H | ⬜ | — |
| **P1.6** | Lead race fix + 409 on duplicate | Rl | ⬜ | — |
| **AV-§1** | **Availability endpoint** — per-room vacancy + `next_available_from` (unblocks A1/R2/R3) | Rl Rf | ⬜ | — |
| **AV-§3a** | Public-safe `property-details-bots` projection (strip PII/internal cols) | H | ⬜ | — |
| **T1** | Token lifecycle (real advance, refund/adjust path) | Rl | ⬜ | — |
| **T2** | Identity dedup (lead → tenant by phone) | Rl | ⬜ | — |
| **T3** | Consent + audit before KYC (DPDP) | H | ⬜ | — |
| **T4** | Reservation atomic hold (no double-booking) | Rl | ⬜ | — |
| **A6** | Numeric ratings + aggregate | Rf | ⬜ | — |
| **A8** | Per-property offers | Rl | ⏸ | parked |
| **R7** | Pagination + precomputed distance (latency) | In | ⬜ | — |

---

## 🔵 Decisions needed from you (not blocking now)

| ID | Decision | Why it matters |
|---|---|---|
| **E3** | Translate property content **on-the-fly** (bot-only) vs **stored translations** (backend)? | Bot speaks Hindi/Marathi; property content is English. Fork determines ownership. |
| **AV-§2** | Does a `status=2` (booked-not-onboarded) bed count as **available**? | Inventory-policy product call; shapes the availability endpoint + honest scarcity. |

---

## ✅ Done Ledger — do NOT redo (shipped to `main`)

Evidence so we never re-litigate or duplicate finished work.

| Area | What | PR |
|---|---|---|
| Honesty | Success-on-failure across write tools (booking/visit/call/reserve) | [#21](https://github.com/5s10r2/claude-booking-bot/pull/21) |
| Honesty | Wave A "stop lying" — `user_error()`, no fabricated outcomes | [#5](https://github.com/5s10r2/claude-booking-bot/pull/5) |
| Honesty | Phase 0 trust — AI disclosure, killed fabricated facts, grounded scarcity | [#8](https://github.com/5s10r2/claude-booking-bot/pull/8) |
| Honesty | Empty-vs-error honesty in search (None=error vs []=empty) | baseline |
| Relevance | Gender hard-constraint filter (post-score exclude) | [#10](https://github.com/5s10r2/claude-booking-bot/pull/10) |
| Relevance | Gender NAME-vs-TAG fix (girls-only-named on boys search) | [#27](https://github.com/5s10r2/claude-booking-bot/pull/27) |
| Relevance | Broker search-first (stop re-asking amenities) | [#11](https://github.com/5s10r2/claude-booking-bot/pull/11) |
| Contract | Shortlist contract (inner status:200) | [#12](https://github.com/5s10r2/claude-booking-bot/pull/12) |
| Contract | Rentok API contract alignment (room-details POST, lead_source) | [#6](https://github.com/5s10r2/claude-booking-bot/pull/6) |
| Rendering | Native property carousel supersedes scraper (P4) | [#23](https://github.com/5s10r2/claude-booking-bot/pull/23) |
| Rendering | Native show-more pagination | [#26](https://github.com/5s10r2/claude-booking-bot/pull/26) |
| Rendering | Comparison scraper deleted (native only) | [#29](https://github.com/5s10r2/claude-booking-bot/pull/29) |
| Language | Script-mirroring (reply in user's script) | [#22](https://github.com/5s10r2/claude-booking-bot/pull/22) |
| Routing | Classify robustness + last_agent TTL | [#24](https://github.com/5s10r2/claude-booking-bot/pull/24) |
| KB | pgvector codec fix | [#25](https://github.com/5s10r2/claude-booking-bot/pull/25) |
| UX | Chat redesign backend re-land D1–D6 (native units) | main `bd1a4a2` |
| Security | Waves 1–3 (tenant isolation, HMAC, untrusted-content fencing, tool boundary) | [#3](https://github.com/5s10r2/claude-booking-bot/pull/3), [#4](https://github.com/5s10r2/claude-booking-bot/pull/4) |
| Leads | B1 warm-lead handoff — rich `remarks` + `room_type` to the manager | [#32](https://github.com/5s10r2/claude-booking-bot/pull/32) |
| Latency | LAT-1 concurrent image + geocode enrichment | [#34](https://github.com/5s10r2/claude-booking-bot/pull/34) |
| Ranking | R5 outcome-signal load degrades visibly, not blind-silent | [#35](https://github.com/5s10r2/claude-booking-bot/pull/35) |
| Ranking | **R1 commute ranking** (marquee) — rank by proximity to the user's daily destination; honest "X min"/"~X km" labels; graceful + self-heals. Live-verified 10/10. | [#37](https://github.com/5s10r2/claude-booking-bot/pull/37) [#38](https://github.com/5s10r2/claude-booking-bot/pull/38) [#39](https://github.com/5s10r2/claude-booking-bot/pull/39) [chat#8](https://github.com/5s10r2/eazypg-chat/pull/8) |
| Resilience | **C1 OSRM circuit breaker** — skip the dead routing host instantly (no 30s dead-air), honest straight-line fallback (no fabricated distances), self-heals on restore. Live-verified 4/4. | [#43](https://github.com/5s10r2/claude-booking-bot/pull/43) |
| Personalization | **NAME-1** — `save_name` capture (web analog of WhatsApp profile name) + name directive threaded into all 4 agents; uses the user's first name naturally, persists it. Live-verified. | [#45](https://github.com/5s10r2/claude-booking-bot/pull/45) |

---

## Session log (append-only)

- **2026-06-05 (NAME-1 + G-13/G-20 verify)** — PO flagged: the bot never asks the
  lead's name → zero personalization. Verify-first confirmed it and root-caused two
  breaks: (1) **capture** — `set_user_name` fires ONLY on the WhatsApp webhook (Meta
  profile name); web users are never asked. (2) **usage** — `get_user_name` was read
  in exactly ONE place (`default_agent`), and `DEFAULT_AGENT_PROMPT` has no
  `{user_name}` placeholder, so even that was dead. The broker/booking/profile agents
  never received the name. **NAME-1 fix** ([#45](https://github.com/5s10r2/claude-booking-bot/pull/45)): `save_name` tool (web analog,
  in ALWAYS_TOOLS so capturable on any broker turn) + `build_name_directive()` appended
  (UNCACHED — keeps `_base.md` cacheable) to all four agents' prompts; qualify skills
  ask once after first results (never stacked with the commute ask) + greet returning
  users by name. 31/31 hermetic, gate 39/39. Same verify-first pass settled **G-13**
  (already done — coords reach the user via Leaflet map + `google_map` link; the
  "formatting only" label was stale) and **G-20** (NOT formatting — property phone is
  deliberately hidden for privacy/funnel; no brand support line in config → PO decided
  to add a per-brand `support_contact` field, next PR).
- **2026-06-05** — Engagement kickoff. Aligned on 5-truth bar + bot-first sequencing.
  Ran cross-repo audit (reconciled roadmap vs real code; caught two wrong agent
  assumptions: pg_ids is sync; `remarks` is not a no-op). Created this tracker.
  **Shipped to main this session:** B1 (#32), LAT-1 (#34), R5 (#35), tracker (#33) —
  4 merges, gate 34/34 green. R1 design locked (capture = ask-once-optional);
  held for a dedicated focused pass (ranking core — don't cram, per the P4 lesson).
- **2026-06-05 (R1 pass)** — Verify-first read of the ranking core caught that
  `commute_from` ALREADY exists as a captured-but-unused pref (RENTOK_API.md:1016
  "❌ Not sent") — so R1 REUSES it, no new `commute_to` field. Built one cohesive
  backend change: capture nudge (qualify_new.md post-results, ask-once optional,
  search-first preserved) → compute (`_compute_commute_minutes`) → blend
  (scoring.py commute term REPLACES distance only when known; budget/amenity keep
  weight = complement) → surface (card label). Companion FE PR (eazypg-chat #8):
  accented commute pill. **#37 shipped** (18/18 hermetic, gate 35/35).
- **2026-06-05 (R1 LIVE-VERIFY → 2 fast-follows)** — Live prod smoke caught two
  things offline tests can't: (1) the broker followed the OLD "swap location to
  the commute point" workaround instead of saving `commute_from` (read prefs back
  via admin API to confirm — it was absent). Fixed prompt steering in **#38**
  (commute.md + prompts.py: save `commute_from`, KEEP location, backend ranks).
  (2) `maps.rentok.com` OSRM is timing out 30s on prod (15+/16 tool failures),
  so the precise-drive-time path produced nothing. **#39** added an honest
  straight-line (haversine) fallback: ALWAYS rank by office proximity (instant,
  infra-free), UPGRADE to OSRM minutes when the service responds; label "X min"
  or honest "~X km from <dest>"; never a faked time; self-heals. Re-smoke after
  each deploy via the admin-prefs read + carousel-label check. **Final live
  smoke: 10/10** — area-only unchanged (no commute_from, no labels); commute
  search re-ranks by proximity (Ghatkopar 4km #1 vs area-top Mass Metropolis),
  honest "~X km from Powai" labels, sorted ascending. R1 ✅ shipped + verified.
  Surfaced **C1** (estimate_commute 30s dead-air + Haiku fabrication) for next.
  Hermetic suite 26/26 on test_commute_ranking.py; gate 35/35.
- **2026-06-05 (C1)** — Backend confirmed `maps.rentok.com` OSRM is down at the
  network level (EC2 unreachable; the booking bot is the live consumer). Shipped
  `core/osrm.py` circuit breaker: trip-on-failure → skip for a 10-min cooldown →
  half-open probe → self-heal; fail-open on Redis error. estimate_commute +
  fetch_landmarks now return honest "~X km straight-line (live route timing
  unavailable)" — gives the broker a real number so it stops fabricating "~8-10
  km". R1 + both landmark tools routed through the one breaker; shared
  `haversine_km` moved to utils/geo.py. **#43 shipped, live-verified 4/4**: R1
  labels intact via osrm_get; 0 new estimate_commute 30s-timeouts across 3
  commute Qs (was 90s of dead-air); slowest turn 16.5s (residual = Overpass
  transit, not OSRM). test_osrm_circuit.py 18/18; test_commute_ranking 26/26.
  Open coordination: backend restarts the OSRM EC2 → bot auto-upgrades to "X min".

---

## Source inputs (not trackers — context only)

- Roadmap umbrella: `rentok-backend/docs/booking-bot-upgrade-roadmap.md` (+ per-workstream detail docs)
- Narrative session memory: `~/.claude/projects/-Users-eazypg-CC-Booking-Bot-FInal/memory/MEMORY.md`
- Codebase map: `claude-booking-bot/CLAUDE.md`
