# AV-§1 — Availability Endpoint Spec

**From:** Sanchay (CPO/PM, EazyPG Booking Bot)  
**To:** RentOk Backend team  
**Date:** 2026-06-07  
**Status:** Ready to build — all product decisions resolved.

---

## The one-line ask

Add `GET /bookingBot/availability` — a live per-room vacancy feed sourced from the bed-allocation table. The bot cannot answer "is a bed free, and from when?" today. This endpoint fixes that and unblocks 6 downstream improvements in one call.

---

## Endpoint contract

### Request

```
GET /bookingBot/availability?pg_ids=<id1>,<id2>,...
X-API-Key: <brand_api_key>
```

- `pg_ids`: comma-separated Firebase property IDs (`p_pg_id`) — the same IDs the bot already uses in every other `/bookingBot/*` call. 1–10 per request.
- Auth: same `X-API-Key` pattern as the other bot endpoints. No new auth mechanism needed for now.

Alternatively, POST with a body if query-string length is a concern:
```json
{ "pg_ids": ["<firebase_pg_id>", ...] }
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
          "availability_kind": "vacant_now",   // see Semantics below
          "next_available_from": null,          // null = free now; ISO 8601 date if occupied but freeing
          "rent_per_bed": 9500                  // monthly rent, excluding deposit and add-ons
        }
      ]
    }
  ]
}
```

---

## Semantics (resolved — all decisions made)

### 1. What marks a bed "occupied"?

A bed flips to occupied when **both** conditions hold:
- Booking is confirmed (payment received or token paid), AND
- The tenant is either onboarded OR the move-in date is within 7 days

**`status=2` (booked, not yet onboarded) → `is_available: true`, `availability_kind: "soft_hold"`**

Rationale: a status-2 booking might not complete (KYC fails, tenant backs out, payment lapses). Hiding the bed from search silently removes real inventory. Showing it with a "soft hold" label lets the bot say "one bed — someone has a booking, move quickly" — honest scarcity that drives conversion. We never inflate availability; we never suppress it either.

### 2. `availability_kind` — three states, three different bot responses

| `availability_kind` | Meaning | What the bot says |
|---|---|---|
| `vacant_now` | Bed is empty today, ready immediately | "Available now" |
| `soft_hold` | status=2: booking placed, not yet onboarded | "1 bed — someone has a booking, act fast" |
| `freeing_on_notice` | Current tenant gave notice, leaving on date X | "Opens up on [date]" |

`next_available_from`:
- `vacant_now` → `null`
- `soft_hold` → expected move-in date (if known) or `null`
- `freeing_on_notice` → the notice-end date (ISO 8601, e.g. `"2026-07-15"`)

### 3. `rent_per_bed` — base rent only

Return the **base monthly rent, excluding deposit and add-on services.** The bot surfaces total move-in cost separately once we have structured add-ons (A5 — future). Mixing them into this field now would require us to reverse it later.

### 4. Freshness requirement

- Source of truth: **live bed-allocation / tenant-occupancy table** — NOT `propertyMicrosite` config or any manually-maintained field.
- Server-side cache: **60–120 second TTL is acceptable.** A config-level snapshot is not.
- Why: the bot is an honesty-first product (fabricated availability destroys trust faster than showing nothing). A stale "1 bed available" that's actually taken is worse than "call us to confirm."

---

## What the bot builds when this lands

This is the bot-side consumption plan, for sizing and alignment:

| Item | What we build | File |
|---|---|---|
| **A1** | `tools/broker/availability.py` — calls this endpoint, formats per-room availability for the broker agent | new file |
| **A1** | Carousel cards show "X beds · from ₹9,500" or "Opens 15 Jul" inline | `tools/broker/search.py` |
| **R2** | `match_score` gets a `vacant_beds` term — properties with available beds score higher | `utils/scoring.py` |
| **R3** | Move-in preference vs `next_available_from` validation — flag properties that open after the user's target date | `tools/broker/search.py` |
| **E2 (full)** | Property details confirm availability before the bot commits to "this place is available" | `tools/broker/property_details.py` |

All bot-side work is parked until the endpoint lands. No dependency on your timeline from our side.

---

## Secondary asks — do when you're touching these routes anyway

These are not blockers for the availability endpoint.

**3a. Versioned bot projection for `property-details-bots`**

Today that endpoint returns the full 221-column `Property` entity. The bot uses ~25 columns. If a column is renamed, the bot breaks silently.

Ask: expose a documented, stable projection that strips `autopay_*`, `eviction_*`, `wallet_*`, KYC, and settlement-cycle fields (internal ops — should never reach a tenant payload). Add these Phase-2 fields from `rental-option` when you do (source already exists per your own re-verify):
- `addOnServices[]` — name, amount, billing, category
- `includedServices[]` — structured (Wi-Fi, power backup, parking, maintenance)
- per-room `images[]`

**3b. Auth / brand isolation on bot routes**

Bot routes today trust `pg_id` from the request body with no brand check. Once richer data flows through these routes, a brand-scoped check (`pg_ids` must belong to the requesting brand) should land alongside. We already carry a `brand_link_token` for web channel scoping — happy to use that or a simpler server-to-server key.

---

## Forward-looking — build the source to be push-capable

Not for now, but please **don't foreclose it**: if the availability data source can emit change events later, we can wire a webhook:

```
POST <bot>/webhook/availability  { pg_id, room_id, event: "bed_freed" | "bed_taken", vacant_beds }
```

This lets the bot proactively re-engage leads the moment their wanted property opens up (we already run a follow-up state machine — `core/followup.py`). That's a conversion loop a static bot can't match. No work needed now; just keep the door open.

---

## What we are NOT asking for

Verified as dead or redundant (saves your time):

- **Structured deposit** — `rental-option.deposit` re-reads `microsite_data.security_deposit` (`propertyDetails.ts:551`); same value the bot already has.
- **Rental terms** — flat strings already fetched.
- **highlights[]** — hardcoded `[]` (`propertyDetails.ts:553`). Dead.
- **marketingDescription** — hardcoded `''` (`propertyDetails.ts:563`). Dead.
- **lat/long, support contacts** — already in the payload; bot-side work, no backend ask.

---

## Priority in one line

**Build the availability endpoint first — it's the only thing gating bot conversion, and `occupied_beds + vacant_beds` is a count you already compute and throw away today (`propertyDetails.ts:628-641`).** §2 semantics are now all resolved above. §3 and §4 ride future changes.
