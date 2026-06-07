# Brand Self-Serve Signup + WhatsApp Onboarding — Design

**Date:** 2026-06-05
**Status:** Draft for review
**Owner:** Sanchay (Solo CPO/PM)

---

## 1. The problem

New brands are waiting to get on the booking bot, but the only way to add one today is to edit `_SEED_BRANDS` in `main.py` and redeploy. That hand-seeding step is now blocking real deals. We want brands to onboard themselves.

"Signup" is hiding two jobs:

- **Account** — a brand creates a login and lands in the admin panel.
- **Provisioning** — the brand gets connected to its properties (RentOk `pg_ids`) and a WhatsApp number, so the bot actually answers. This is the hard 90%.

A signup form that only collects email + password produces a dead bot. The real work is provisioning.

## 2. What's already decided

| Question | Decision |
|---|---|
| Channels live in v1 | **Web + WhatsApp** |
| Where WhatsApp credentials come from | **Embedded Signup** (Meta Tech Provider) |
| Meta standing | **Approved & ready** (must confirm App Review + Business Verification are cleared — see §7) |
| Who owns identity + WhatsApp | **RentOk** is the system of record. The bot consumes it. |
| Incoming brands' RentOk relationship | Mostly **existing RentOk customers** → auto-attach their real `pg_ids`. Net-new = handled, gated. |
| Admin login | **Both** — native email/password (standalone product login) **and** "Login with RentOk" (coupled to RentOk's identity). |
| Build sequence | **Approach A** — thin self-serve spine first; Embedded Signup as a separately-shippable fast-follow. |

## 3. The core architecture decision: bot consumes RentOk

RentOk is a registered Meta Tech Provider and **already has production plumbing** for Embedded Signup, accounts, and the WABA→property mapping. We do **not** rebuild any of that. Rebuilding it would mean two services minting and storing WhatsApp tokens against the same Meta App, and two competing sources of "which number maps to which property" — a data-integrity trap.

So: the **signup UI lives in our admin panel (`eazypg-admin`)**, but underneath it **calls RentOk**. The booking bot's only job is to write its own `brand_config` (brand_hash, pg_ids, phone_number_id, access_token, link token) *from* RentOk's data.

### What RentOk already gives us (reuse map)

| We need | RentOk has | Where |
|---|---|---|
| ES code→token exchange | `registerWabaUser()` | `controllers/others.ts:8669` |
| Meta app creds | `META_CLIENT_ID`, `META_CLIENT_SECRET`, `META_SYSTEM_USER_ACCESS_TOKEN` | env |
| WABA→number→property map | `WabaPhoneMap { pg_id, waba_id, phone_number_id, waba_phone }` | `entities/waba_phone_map.ts` |
| Account → pg_ids (fast path) | `Account.find({ relations:['property'] })` | `entities/account.ts` |
| New-brand creation | `POST /v1/signup/admin` → returns `jwt_token` + `eazypg_id` | `v1/signup/service.ts` |
| Inbound WhatsApp routing | `getAccountIdsFromWabaId(waba_id)` → tenant / lead / **booking-bot** handler | `services/meta/metaWebhookService.ts` |

### How the bot is wired to RentOk today (from the integration-seams findings)

The inbound WhatsApp path already runs through RentOk:

- Meta webhook → RentOk's `metaWebhookService.ts` extracts `waba_id`, calls `getAccountIdsFromWabaId(waba_id)` → returns `{ pg_ids, phone_number_id, whitelabel, default_rent_ok }`.
  - `whitelabel: true` = the brand registered its **own** WABA (row exists in `WabaPhoneMap`). `default_rent_ok: true` = the shared RentOk system WABA. Unregistered + not-default → webhook ignored.
- If `!is_current_tenant` and any `pg_id` is in the bot list, RentOk calls `sendBookingBotData()` → POSTs to the bot at `${baseURL}/langgraph`, passing the message plus an `extra_keys` object: `eazypg_id, pg_ids, is_meta, waba_id, phone_number_id, rentok_phone, pg_id_eazypg_id_map, kyc_enabled, areas, cities, brand_name, min_rent, max_rent, localities`.
- The bot replies **directly to Meta** using its **own** `phone_number_id` + `access_token` — RentOk plays no part in outbound. So provisioning must land that credential triple on the bot side.

### Seams that need work on the RentOk side

Three facts from the seams findings change the build:

1. **Bot routing is a hardcoded `pg_id` list.** RentOk routes a number to the bot only if its `pg_id` is in a hardcoded `booking_bot_pg_ids` array in `metaWebhookService.ts` — no per-property flag. Adding a brand today means a RentOk code change + redeploy. **Self-serve requires replacing this array with a lookup** (a DB flag/table RentOk writes at provisioning time).

2. **RentOk → bot call is unauthenticated, to a hardcoded IP.** Base URL is hardcoded `http://57.155.89.118:8000` (with a per-phone `{phone}_ngrok` Redis override), no auth header. For safe multi-brand provisioning this needs (a) a URL env var and (b) a shared secret. Hardening item, not a v1 blocker for the *web* channel.

3. **`registerWabaUser` / `saveWabaPhoneMap` are public (no auth).** Anyone can register a WABA or rewrite a `waba_id`→`pg_id` mapping. If our wizard calls these, secure them and bind them to the authenticated account context.

## 3b. RentOk-side work package (cross-repo, small)

Everything below is in `rentok-backend`. It's small and additive — no breaking changes. This is the full list of what RentOk must add for self-serve to work; the booking bot can't do it from its own repo.

| # | Change | Why | Size |
|---|---|---|---|
| R1 | **Routing flag in DB.** Add `booking_bot_enabled boolean default false` to the `Property` entity; replace the hardcoded `booking_bot_pg_ids` array check in `metaWebhookService.ts:~210` with that column. | Today enabling the bot for a brand = code edit + redeploy. This makes it a DB write the wizard can do. **This is the real unlock.** | ~5 lines + 1 migration |
| R2 | **Service-to-service auth.** Add an internal `X-Service-Key` (shared secret) middleware. Apply it to the provisioning calls the wizard makes, and **secure the currently-public `registerWabaUser` / `saveWabaPhoneMap`**. | No S2S auth exists today; these endpoints are open to the internet. | 1 middleware + a few route guards |
| R3 | **Env var + secret for the RentOk→bot call.** Replace hardcoded `http://57.155.89.118:8000` with an env var; sign the call with a shared secret the bot verifies. | Hardcoded IP + unauthenticated inbound. Hardening; needed before many brands. | small |
| R4 | *(nice-to-have)* Authenticated `GET /owner/properties` returning the caller's `pg_id`/`eazypg_id`/`pg_name` list. | Lets us re-sync properties after login, not just at login. Not required for v1. | ~20 lines |

What RentOk **already** has and we reuse as-is: `POST /v1/login/property/check-email` (identity + pg_ids), `registerWabaUser()` (ES code→token), `WabaPhoneMap`, `POST /v1/signup/admin` (net-new account creation).

## 4. The flow (Approach A)

Three stages. The first two have **zero Meta dependency** and ship first. WhatsApp is a pluggable slot bolted on after.

### Stage 1 — Sign up + instant demo
- Public **Sign up**: email + password + email verification.
- Backend mints an account **and a starter demo brand on sandbox inventory** immediately.
- Brand lands in the admin panel with a working **web** bot in minutes — no RentOk link, no Meta gate. This is the "aha" before any paperwork.

### Stage 2 — Activate (go live on real data)
- Swap sandbox inventory for the brand's real properties.
- **Existing RentOk customer:** look them up (by account / owner phone / email) and **auto-attach their real `pg_ids`**. Their web link is now live on real stock.
- **Net-new to RentOk:** gated. They need a RentOk account first (assisted by you / sales). The **demo keeps running** so they still see value while that happens.

### Stage 3 — Connect WhatsApp (the pluggable slot)
- Built as a **provider interface**, not hardwired to Meta. The slot has one job: produce a `(phone_number_id, access_token, waba_id)` triple and register the `brand_wa:{phone_number_id}` reverse-lookup.
- **Provider = Embedded Signup** (the target): launches Meta's ES popup with `config_id`, catches the `WA_EMBEDDED_SIGNUP` message event for `waba_id` + `phone_number_id`, exchanges the 30-second `code` server-side (via RentOk's `registerWabaUser`), and flips the brand live on WhatsApp.
- Because it's a slot, an interim **assisted-paste** provider (operator pastes the triple) can ship the same UI before ES is wired — *if* we want WhatsApp live before the ES integration is finished. Decision point in §9.

## 5. Booking-bot side: what we build

The bot already has the brand model (`brand_config:{brand_hash}`, `brand_wa:{phone_number_id}`, `brand_token:{uuid}`). New work:

- **Public signup + provisioning endpoints** that do *not* require an existing brand key (solves the chicken-and-egg in §6). These call RentOk, then write `brand_config`.
- **Account/auth layer** for the admin panel (dual login — see §5a): today admin auth is just "your key has a brand config." Signup needs a real account that *issues* the brand key after provisioning.
- **Activate wizard UI** in `eazypg-admin`: RentOk lookup → pg_id attach → web-link live → WhatsApp slot.
- **Demo seeding**: a sandbox brand config + sample pg_ids minted at signup.

## 5a. Login: two doors, one account

The admin panel offers **both** login methods. They map naturally onto the two kinds of brand.

- **Native email/password** — a standalone product login. No RentOk coupling. This is the path for net-new brands and anyone who wants the bot account separate from RentOk. Identity here tells us nothing about their properties, so `pg_id` attach happens later in the Activate wizard (Stage 2).

- **Login with RentOk** — SSO against RentOk's existing identity (Firebase → RentOk JWT). For an **existing RentOk customer this is the express lane**: the same login that authenticates them also hands us their `pg_ids`, so login + property-attach collapse into one step (Stage 1 and the Stage 2 lookup happen together).

Both doors land on the **same brand account model** underneath (one `brand_config`, one brand key issued after provisioning). The login method is just how the brand authenticates; it does not fork the data model. A brand could start native and link RentOk later, or vice-versa.

**How "Login with RentOk" actually works (verified against the RentOk code):**
- RentOk's login is `POST /v1/login/property/check-email` — the client sends a **Firebase ID token** (`user_id_token`); RentOk verifies it with firebase-admin and returns a **RentOk JWT** (7-day, signed with `JWT_SECRET`, payload `{ pg_id, isOwner }`). For an owner with **multiple** properties it returns a `properties[]` array (`pg_id`, `pg_name`, `eazypg_id`, `logo_url`) plus a short-lived `multi_property_token`.
- So the express lane **reuses that existing endpoint** — no new RentOk identity endpoint required. Our admin panel does Firebase client login (same Firebase project) → calls `check-email` → receives the JWT and the `pg_id`(s). That response *is* both the identity and the property list.
- We **trust the response over HTTPS** (we just called RentOk and it answered). Optionally we can also verify the JWT ourselves, but that would mean sharing `JWT_SECRET` across services — avoid it; trusting the direct call is cleaner and keeps the signing secret inside RentOk.
- The only gap: there's no standalone "list my properties" endpoint for later re-syncs — properties come back **only at login**. For v1 that's fine (we capture `pg_ids` at login). A small authenticated `GET /owner/properties` is a nice-to-have for re-sync, listed in the RentOk work package (§3b).

## 6. Solving the chicken-and-egg auth problem

Today every admin route — including `POST /admin/brand-config` — uses `require_admin_brand_key`, which **rejects any key without an existing brand config** ([core/auth.py:50](../../core/auth.py)). A brand-new brand has no key, so it can't create itself. That's why brands are seeded in code today.

Fix: a **separate signup/provisioning path** that is account-authenticated (email-verified login or RentOk JWT), **not** brand-key-authenticated. It is rate-limited and abuse-guarded because it's reachable pre-brand. It is the *only* path that can create a brand config; once created, the brand gets its key and everything else stays behind `require_admin_brand_key` exactly as now.

## 7. Meta API constraints (verified against live docs, 2026)

These are build rules. RentOk's existing code is stale in places — do not copy blindly.

**Do NOT copy from RentOk:**
- **Graph API v17 / v19 / v20 pins.** Current is **v25.0**. Target v25.0.
- **`debug_token` + `granular_scopes` extraction** of `waba_id`. Superseded — use the **`WA_EMBEDDED_SIGNUP` browser message event** session-info object to get `waba_id` + `phone_number_id`.

**Current correct flow:**
- ES launch requires a **Login Configuration `config_id`**: `FB.login({ config_id, response_type:'code', override_default_response_type:true, extras:{ setup:{} } })`.
- Callback returns an **exchangeable `code` with a 30-second TTL**; exchange server-side for the business token (v25.0).
- Subscribe to the **`account_update` webhook** — it fires on ES completion and carries the onboarded business info.
- **Phone activation unchanged:** register the phone number, subscribe the app to the WABA (Subscribed Apps API).
- **Pricing is per-message** (since Jul 1 2025) — relevant to the cost model, not the build.

**Confirm before coding:**
- Exact **`sessionInfoVersion`** value (v2 vs v3) — not pinned in our research; check the live ES implementation doc.
- That the Meta App has cleared **Business Verification + App Review** for `whatsapp_business_management` and `whatsapp_business_messaging`. "Approved & ready" should mean yes; if not, this is a weeks-long, Meta-controlled gate that blocks Stage 3 only (Stages 1–2 are unaffected).

## 8. Phasing

- **Phase 1 — Self-serve spine + demo + web go-live.** Signup, account/auth, demo seeding, Activate wizard with RentOk lookup + pg_id attach, web link live. Zero Meta dependency. Ships first, delivers value alone.
- **Phase 2 — Connect WhatsApp via Embedded Signup.** The provider slot + ES integration + `account_update` webhook + RentOk routing-table change (replace the hardcoded `booking_bot_pg_ids` array). Separately deployable; a bug here cannot block Phase 1.

## 9. Open items to confirm on review

1. **Assisted-paste interim, yes or no?** If Meta approval is truly done, we can wire Embedded Signup directly and skip the assisted-paste provider. If there's any approval risk, the interim provider lets WhatsApp ship behind the same UI. *(Recommendation: build the slot regardless; only build assisted-paste if ES approval is uncertain.)*
2. ~~Admin-panel account model~~ — **DECIDED: both** (native email/password **and** Login with RentOk). See §5a. Remaining sub-question: does RentOk already expose a "verify token → return account + pg_ids" endpoint, or do we add one?
3. **Net-new gating UX — recommendation: demo + assisted for v1.** Keep net-new brands on the running demo and hand them to assisted RentOk signup. Note: RentOk's `POST /v1/signup/admin` is callable (it creates an account+property from a Firebase token), so a *fully self-serve* net-new path is buildable later — but it adds Firebase-account creation + RentOk onboarding into our wizard. Defer to v2; don't bloat v1.
4. **RentOk-side changes — see §3b.** R1 (DB routing flag) is the unlock for **Phase 2 (WhatsApp)** go-live — *not* needed for Phase 1, which is web-only and never touches RentOk's WhatsApp routing. R2/R3 (S2S auth + secured inbound) are needed before scaling brand count. R4 is optional. Confirm you own landing these in `rentok-backend`.

## 10. Out of scope (YAGNI for v1)

- Billing / paid plans / metering.
- Multi-user teams per brand (one account = one brand admin for now).
- Self-serve for net-new (non-RentOk) brands beyond demo + hand-off.
- Replacing RentOk's Interakt/BSP send path.
