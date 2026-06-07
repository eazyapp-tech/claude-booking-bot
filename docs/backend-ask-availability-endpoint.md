# Backend Ask — Booking Bot Availability Endpoint (+ contract hygiene)

**From:** Sanchay (CPO/PM, EazyPG Booking Bot) · **To:** RentOk Backend team · **Date:** 2026-06-04
**Context:** Reconciled against `rentok-backend/docs/booking-bot-property-data-gap.md` and the booking-bot codebase. This is the bot side's concrete ask.

---

## TL;DR

The booking bot can describe a property in full but **cannot answer the single most decisive pre-booking question: "is a bed free, and from when?"** One purpose-built endpoint fixes this and collapses five separate data gaps into a single call. Everything else below is secondary and can ride later changes.

**The ask:** a thin, **live** availability endpoint sourced from the bed-allocation truth — not from microsite config.

---

## 1. PRIMARY ASK — `GET /bookingBot/availability`

### Request
```
GET /bookingBot/availability
{
  "pg_ids": ["<firebase_pg_id>", ...],   // 1..N properties, Firebase UID (p_pg_id), same IDs the bot already uses
  "brand_token": "<uuid>"                 // for brand scoping / auth (see §3)
}
```

### Response
```jsonc
{
  "data": [
    {
      "pg_id": "<firebase_pg_id>",
      "rooms": [
        {
          "room_id": "<id>",
          "room_name": "Room 101",
          "sharing_type": "Double sharing",
          "total_beds": 2,
          "occupied_beds": 1,
          "vacant_beds": 1,
          "is_available": true,
          "next_available_from": null,      // null if a bed is free now; ISO date if currently full but freeing
          "rent_per_bed": 9500              // authoritative per-sharing-type price
        }
      ]
    }
  ]
}
```

### The non-negotiable requirement: **freshness / source of truth**
- Must be derived **live from the bed-allocation / tenant-occupancy table** — the actual truth for who occupies which bed.
- **Must NOT** be read from `propertyMicrosite` config or any manually-maintained field.
- Rationale: this is an honesty-first product (grounded scarcity, never fabricate — already shipped). A *wrong* "available from May 1" is worse than saying nothing. Stale availability silently destroys trust and undoes shipped work.
- A short server-side cache (60–120s TTL) is acceptable; a config-level snapshot is not.

### Why this endpoint specifically
It answers **five** of our gap items in one call, so we don't chase them separately:
| Gap item | Answered by |
|---|---|
| #1 isAvailable per room | `is_available` |
| #2 availableFrom per room | `next_available_from` |
| #3 vacant bed count | `vacant_beds` |
| #4 occupied bed count | `occupied_beds` |
| #7 rent per sharing type | `rent_per_bed` |

Note #3/#4 are **already computed and then discarded** today (`propertyDetails.ts:628-641`). This is not new computation — it's returning what you already know, shaped for the bot.

---

## 2. SEMANTICS WE NEED YOU TO DEFINE (blocks accurate bot copy)

The bot will speak these values to users verbatim, so the meaning must be pinned down:

1. **What marks a bed as `occupied`?** Booking confirmed / tenant onboarded / agreement signed / payment received — which event flips it?
2. **What does `next_available_from` mean?**
   - "hard vacant now" (bed empty today), vs
   - "current tenant gave notice, frees on date X" (notice-period driven).
   If both cases exist, we need a flag to distinguish them (e.g. `availability_kind: "vacant_now" | "freeing_on_notice"`), because the bot says different things ("available now" vs "opens up on the 15th").
3. **Is `rent_per_bed` inclusive or exclusive** of deposit / add-on services? (We surface "true monthly cost" honestly.)

---

## 3. SECONDARY ASKS — bundle only if you're touching these routes anyway

These are not blockers. Do them when convenient.

### 3a. A versioned, public-safe bot contract for `property-details-bots`
- Today the endpoint returns the **full 221-column `Property` entity dump**; the bot projects ~25. If a `Property` column is renamed, the bot breaks silently.
- Ask: expose a **documented, versioned** projection for the bot that (a) **strips** `autopay_*`, `eviction_*`, `wallet_*`, KYC, settlement-cycle fields (operational/financial, never tenant-facing), and (b) **adds** the Phase-2 enrichment fields below.
- Benefit for you: kills the over-fetch bandwidth waste and removes the latent risk of internal fields leaking into a tenant-facing payload. Benefit for us: we stop coupling to your internal schema.

**Phase-2 enrichment fields to include in the projection** (source already exists in `rental-option`, per your own re-verify):
- `addOnServices[]` — name, amount, billing frequency, category
- `includedServices[]` — structured (Wi-Fi, power backup, parking, maintenance), not a blob
- per-room `images[]`

### 3b. Auth on the bot routes
- Bot routes currently trust `pg_id` from the request body with no auth / brand isolation.
- Ask: accept a bot key or brand-scoped signed token (the `brand_token` in §1) and enforce that requested `pg_ids` belong to that brand. The moment richer data flows through these routes, this should land.

---

## 4. FORWARD-LOOKING — design the availability work to be push-capable

Not for now, but please **build the availability source so it can emit change events later.** A future webhook —

```
POST <bot>/webhook/availability   { pg_id, room_id, event: "bed_freed" | "bed_taken", vacant_beds }
```

— lets the bot **proactively re-engage a lead the moment their wanted property opens up** (we already run `schedule_followup` + `/cron/follow-ups`). That's a conversion mechanic a static bot can't match. We don't need the webhook today; we just don't want the availability build to foreclose it.

---

## 5. What we are NOT asking for (verified dead/redundant — saves you time)

From the original gap doc, the bot side + your re-verify confirmed these are **not worth building**:
- **structured deposit** — `rental-option` `deposit` just re-reads `microsite_data.security_deposit` (`propertyDetails.ts:551`); same value the bot already gets.
- **rental terms** — flat strings already fetched.
- **highlights[]** — hardcoded `[]` (`propertyDetails.ts:553`). Dead.
- **marketingDescription** — hardcoded `''` (`propertyDetails.ts:563`). Dead.
- **lat/long, support contacts** — already in the payload; bot-side formatting, no backend work.

---

## 6. Priority for you, in one line

**Build §1 (live availability endpoint) first — it's the only thing gating bot conversion, and the count it returns is data you already compute and throw away.** §2 semantics block our copy, so answer those alongside. §3/§4 are convenience/strategic, do when you touch the routes.
