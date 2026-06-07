# Booking Bot — Property Data Gap: Bot-Side Reconciliation

**Owner:** Sanchay (CPO/PM) · **Reconciled:** 2026-06-04 · **Status:** CONVERGED — backend re-verified its own doc, both sides agree

This is the bot-side answer the backend gap doc (`rentok-backend/docs/booking-bot-property-data-gap.md`) asked for in §0/§7: every candidate data point reconciled against the bot's *full* tool set, plus an ingestion-fitness verdict. All claims here are grounded in the booking-bot code with file:line citations.

**Convergence note (backend re-verification, 2026-06-04):** After this reconciliation, the backend re-verified its own doc field-by-field with `propertyDetails.ts` citations and independently confirmed our corrections. Net result of both passes — four fields are now **dead/dropped** (deposit, rental terms, highlights, marketingDescription), lat/long + support contacts are **zero-backend bot-side formatting fixes** (already in the `property-details-bots` payload), and vacant bed count is **sharpened to net-new backend work** (computed internally at `propertyDetails.ts:628-641` but discarded, never returned). §2/§6 below reflect this converged final state.

---

## 1. Verdict in one paragraph

The backend doc is **accepted** as the input to a build. Its central finding is correct and verified: **live availability is a genuine, conversion-blocking gap** with no reusable source in any existing bot tool. Five secondary claims are stale/overstated and are corrected below. **Ingestion fitness is GREEN** — the bot can carry every proposed field end-to-end *today* with formatter-only changes, because the `contract.json` native-unit pipeline (D1–D6) and the schema-agnostic property cache are already in place. The one thing the doc under-weights is the **honesty gate**: availability is the highest-value *and* highest-risk field, so it must not be surfaced as a hard date until a verified real-time bed-state source is confirmed (backend Open Question 3).

---

## 2. Reconciliation of the §5 gap table

Legend: **FETCHED** = already in a bot tool today · **GAP** = genuinely missing · **PARTIAL** = some data, not the full shape · **CORRECTED** = doc claim was wrong/stale.

| # | Data point | Doc said | Reconciled verdict | Evidence |
|---|---|---|---|---|
| 1 | `isAvailable` per room | N, P0 | **GAP (real, P0)** | `room_details.py:27` "no beds_available / live bed count"; no key in any response |
| 2 | `availableFrom` per room | N, P0 | **GAP (real, P0)** — honesty-gated | No key in `get-room-details` or `property-details-bots` |
| 3 | `vacantBedCount` per room | N, P0 | **GAP — NET-NEW BACKEND WORK** | computed internally but discarded, never returned (`propertyDetails.ts:628-641`). The only item needing the backend to surface something new |
| 4 | `occupiedBedCount` | N, P2 | **GAP — net-new backend** | derived from #3; same source |
| 5 | structured numeric `deposit` | Partial | **DROPPED** | backend re-verify: rental-option `deposit` just re-reads `microsite_data.security_deposit` (`propertyDetails.ts:551`) — same value bot already gets. No richer source |
| 6 | `rentalTerms` | Partial | **DROPPED** | flat strings already fetched; no enrichment value |
| 7 | `rentPerMonth {min,max}` per sharing | N, P0 | **GAP — source in rental-option** | only `rent_starts_from` single value today (`search.py:510`); range exists in rental-option |
| 8 | `addOnServices[]` | N, P1 | **GAP — source in rental-option** | not fetched today; exists in rental-option (DueTypes) |
| 9 | `includedServices[]` | Partial | **GAP — source in rental-option** | `services_amenities` string today; structured array in rental-option |
| 10 | per-room `images[]` | N, P1 | **GAP — source in rental-option** | only property gallery today (`search.py:287`) |
| 11 | per-room `amenities[]` | N, P2 | **PARTIAL** | `property_details.py:144` reads `room_amenities` but not per-room-keyed |
| 12 | `occupancyName`/`sharingType` | Partial | **PARTIAL** | `sharing_type` fetched per room |
| 13 | `locationCoordinates {lat,long}` | ⚠️, **P0** | **NOT A GAP — free bot-side** | already in `property-details-bots` entity dump (`property.ts:352-356`) AND fetched/geocoded in search (`search.py:519-524`). Zero-backend formatting fix |
| 14 | `locationObject` structured | Partial | **PARTIAL** | concatenated address string today |
| 15 | `highlights[]` | N, P1 | **DROPPED — dead** | hardcoded `[]` (`propertyDetails.ts:553`) |
| 16 | `tags[]` / `type_tags` | N, P2 | **CHEAP WIN (bot-side)** | `get-room-details` already returns them (`room_details.py:26`) but bot ignores — parse-only, no new API |
| 17 | `policiesAndRules` | Y | **FETCHED** | via microsite `property_rules` (`property_details.py:86,155`) |
| 18 | `marketingDescription` | N, P3, ⚠️ empty | **DROPPED — dead** | confirmed hardcoded `''` (`propertyDetails.ts:563`) |
| 19 | `verifiedStatus`/`total_tenants_count` | N, P2 | **GAP** | trust signals; not fetched |
| 20 | support contacts | ⚠️ | **NOT A GAP — free bot-side** | already in the 21 microsite fields; formatting fix, not a gap |

---

## 3. §7 reconciliation checklist — answered

- **Already fetched by another tool?** Only #13 (lat/long, FETCHED), #16 (tags, arrives-but-ignored), #5/#6/#9/#11/#12 (PARTIAL). Everything else is a true GAP. No tool calls `rental-option`.
- **Can the response formatter carry new fields without blowing token budget?** **YES.** `property_details.py` emits a prose string to the model *and* caches a structured dict in parallel (`property_details.py:166-172`); `ui_parts.py:generate_ui_parts` renders structured units from that cache (`expandable_sections`, `image_gallery`, `status_card`). New fields are formatter additions, not architecture.
- **Does `core/contract.py` / tool schema need updating?** `contract.py` validates `{kind,state,data,surface}` against `contract.json` (shared byte-for-byte with `eazypg-chat/src/contract.json`). New *fields inside existing kinds* need no vocab change; a new **availability badge** is cleanest as a `status_card` variant or a field on the carousel/detail-sheet unit — minor `contract.json` addition, both repos.
- **Redis cache fallback?** Cache is **schema-agnostic** (`db/redis/property.py:22-27`) — new fields auto-persist via the `details.update(...)` merge. No migration.
- **Availability — reuse booking/reserve bed state?** **NO.** `checkPropetyReserved` (`reserve.py:62`) returns binary user-hold, not vacancy. `VERIFIED_RENTOK_CONTRACT.md` A5 states the bot must *own* bed-availability truth. New source required.
- **Coordinates — already geocoded?** **YES** (`search.py:519-524`). Item 13 already removes a geocoding step for properties that ship lat/long; geocode is fallback only.

---

## 4. §8 known-issues — corrected

| Doc issue | Reconciled status |
|---|---|
| **8.1 over-fetch (221 cols), "PII/leak risk on model"** | **Over-fetch CONFIRMED; leak claim OVERSTATED.** Only ~25 fields are projected into the prose the model sees (`property_details.py:139-186`); the ~197 others never reach the model. It's *network waste* + a latent risk if raw dict is ever dumped — not an active model-facing leak. Curated projection still worth doing, riding the enrichment change. |
| **8.2 `query_properties` `pg_name` zero-match** | **STALE — already fixed.** `query_properties.py:58` maps `pg_name → property_name`. No bug. |
| **8.3 no-auth, `pg_id` trusted** | **PARTIALLY MITIGATED.** Web brand derives from a verified link token, WhatsApp from `phone_number_id`; client `brand_hash` never trusted (security waves shipped). Residual: no per-request assertion that `pg_ids ⊆ brand's pg_ids` (`search.py:325`). Low risk (no public write path to `{uid}:pg_ids`); audit-worthy, not blocking. |
| **8.4 typo `checkPropetyReserved`** | **LEAVE AS-IS.** The live RentOk API endpoint is itself misspelled and the bot matches it (proven working in reconcile work). "Fixing" the spelling would 404. Flag as matches-upstream; do not rename. |

---

## 5. Ingestion-fitness verdict: GREEN

The bot just shipped the "ONE CONTRACT" native-unit pipeline (D1–D6: native carousel, structured comparison, enriched detail-sheet). Consequences for this enrichment:
- New property fields flow **model context** (prose add) and **UI** (native unit field) through the same path already exercised by the detail-sheet.
- Cache is schema-agnostic → zero migration.
- Broker runs on Haiku (cost-sensitive) but has no hard token ceiling; new fields add ~tens of tokens of prose. Split prompt caching absorbs the static part.
- An **availability badge** is the only item needing a `contract.json` vocab touch (both repos, byte-faithful) — everything else is field-level.

**There is no architectural blocker.** The gate is data authority (availability real-time source), not bot capability.

---

## 6. How we reuse this — phased plan (converged cost buckets)

**Phase 0 — Honesty gate (backend answer, blocks the availability UI).**
Is `rental-option`'s `isAvailable`/`availableFrom` *real-time/authoritative*, or config-level? Existence is confirmed; freshness is not. **No hard availability date ships until verified** — config-level data degrades to "let me check that for you," never a fabricated date. This is the trust-critical question.

**Phase 1 — Free, bot-side only, no backend.**
- lat/long (#13) → feed commute/map context aggressively (already in payload).
- support contacts (#20) → surface from the microsite fields we already receive.
- `tags`/`type_tags` (#16) → parse what `get-room-details` already returns.
- *Ships this week, independent of backend.*

**Phase 2 — Real gaps whose source already exists in `rental-option`.**
Enrich `property-details-bots` with a **curated public-safe projection** (drop the ~197 unused columns in the same change) carrying: per-room `isAvailable` + `availableFrom` (#1/#2, gated by Phase 0), `rentPerMonth {min,max}` (#7), `addOnServices` (#8), `includedServices` (#9), per-room `images` (#10). Concur with backend: enrich the one bot contract, don't have the bot call `rental-option` directly. Carries through the existing detail-sheet / native-unit pipeline.

**Phase 3 — The one net-new backend item: vacant bed count.**
`vacantBedCount`/`occupiedBedCount` (#3/#4) — backend *computes* this (`propertyDetails.ts:628-641`) but discards it. Needs the backend to surface it. Then render as a native availability badge on the carousel/detail-sheet (`contract.json` addition, both repos). This is the "how many beds free" answer that closes the booking loop.

**Dropped — confirmed not worth it:** structured deposit (#5), rental terms (#6), highlights (#15, dead), marketingDescription (#18, dead).

---

## 7. Backend asks (hand back)

1. **Phase 0 gate (only true blocker):** is `rental-option` `isAvailable`/`availableFrom` authoritative + real-time per bed? Existence confirmed; freshness is the open question. (gates #1/#2)
2. **Phase 3:** expose the vacant-bed count you already compute at `propertyDetails.ts:628-641` in the bot contract (#3/#4).
3. Confirm the curated projection set for the enriched `property-details-bots` (we'll supply the exact Phase-2 field list).
4. Brand-isolation assertion — fine to defer; we'll add the bot-side `pg_ids ⊆ brand` check as cheap audit hardening.

*Resolved by backend re-verify (no longer open): deposit, rental terms, highlights, marketingDescription all dropped; lat/long + support contacts confirmed bot-side-only.*

---

## 8. Sources (bot-side, verified)
- `tools/broker/property_details.py` · `tools/broker/search.py` · `tools/broker/room_details.py` · `tools/broker/compare.py` · `tools/broker/query_properties.py`
- `tools/booking/reserve.py` · `tools/registry.py`
- `core/contract.py` · `core/ui_parts.py` · `db/redis/property.py` · `agents/broker_agent.py`
- `VERIFIED_RENTOK_CONTRACT.md` (A5)
