# SILO — Get the manager told when a bot lead/visit/token lands

**Status:** spec for sign-off · **Truth:** Real (Rl) · **Owner:** Claude (driver) + Sanchay (PO)
**Date:** 2026-06-06

> This is the **true P0** on the tracker. A bot can capture a perfect lead, schedule a
> visit, even take a token — and today **no manager is ever told**. The record sits in a
> table until the manager happens to open the app. First impression: broken.

---

## The finding (verified read-only on live backend)

Three bot write-paths all **persist the record and return `status:200` with zero notification**:

| Bot action | Backend endpoint | Handler (evidence) | Notifies manager? |
|---|---|---|---|
| Schedule visit / call | `POST /bookingBot/add-booking` | `services/bookingBot/bookingBot.ts:28` `saveLeadData` → `insert` → return | ⚠️ **No** |
| Create lead | `POST /tenant/addLeadFromEazyPGID` | `controllers/tenant.ts:2572` → save → `{status:200,"Lead created!"}` | ⚠️ **No** |
| Record token | `POST /bookingBot/addPayment` | `bookingBot.ts:1199` `addBookingBotPayement` → `insert` → return | ⚠️ **No** |

I confirmed each by reading the handler bodies myself: no FCM, no WhatsApp, no email, no socket. The whole `bookingBot` service has **no notification import at all**.

**The mechanism already exists** — the bot path just never calls it:
`POST /others/sendNotificationOnCall` (`controllers/others.ts:3363`) sends a real **FCM push to the property owner** when `receiver_type: 101`: it looks up `FcmTokens` where `account.pg_id = pg_id, user_type = ACCOUNT` and dispatches via `NotificationsService.sendNotification` (others.ts:3498). `receiver_type: 103` does the same for team members.

Two facts that make this cheap to close:
1. **The endpoint is unauthenticated** (`routes/others.ts:268`, no middleware) — and so are all `/bookingBot/*` routes. The bot can call it directly.
2. **The bot already holds the key it needs.** Every search result carries `p_pg_id` + `p_pg_number` (the bot already uses them for image fetch). `sendNotificationOnCall` keys owner FCM tokens off exactly `pg_id`.

**One honest caveat:** the *normal* in-app `addLead()` also doesn't push the manager on save (it only WhatsApps the tenant). So managers today rely on opening the app for organic leads too. Adding a push for **bot** actions is therefore a *new* behaviour, not just "match the organic path." That's a feature, and a good one — bot leads are high-intent — but it's a product choice, not a pure bug-parity fix. Hence sign-off.

---

## When the bot creates these records (so notifications are naturally high-intent)

The bot does **not** create a lead on every search. `_create_external_lead` fires only at **visit** and **call** scheduling (`tools/booking/schedule_visit.py`, reused by `schedule_call.py`). Token is its own explicit action. So the notify moments are: **visit scheduled · call scheduled · token recorded** — all high-intent. No "pushed on every browse" noise risk.

---

## Two ways to close it

### Option A — Bot-side (recommended; I can own + ship + live-verify)
After each **successful** write (inner `status:200` confirmed), the bot fires one fire-and-forget call:

```
POST {RENTOK_API_BASE_URL}/others/sendNotificationOnCall
{ "receiver_type": 101,
  "pg_id": <p_pg_id>, "pg_number": <p_pg_number>,
  "notification_title": "New visit booked 🏠",
  "notification_body": "<name> booked a visit at <property> for <date/time>",
  "notification_name": "booking_bot_visit" }
```

- New `tools/.../notify_manager.py` helper; called from `schedule_visit`, `schedule_call`, `payment` **only after** the existing success check passes.
- **Graceful + non-blocking:** wrapped in try/except, short timeout, never blocks or fails the user reply (mirror C1/Wave-A fail-open). A push failure must never turn a real booking into a user-visible error.
- **Honest:** only fires on a *confirmed* success (we already parse inner `status:200`), so we never push "booked!" for a save that didn't take.
- **Pros:** zero live-backend write-path change; low blast radius; I own it end-to-end; live-verifiable (book a visit → watch for the push / check `sendNotificationOnCall` logs). **Cons:** logic lives in the bot, not at the record's home; a future non-bot caller of those endpoints wouldn't get it.

### Option B — Backend-side (the "correct home", but live prod + your approval)
Add the same `sendNotificationOnCall(101)` call inside `saveLeadData` / `addLeadFromEazyPGID` / `addBookingBotPayement`. Every consumer benefits; lives where the record is born. **Cons:** modifies live-prod handlers; bigger blast radius; needs a backend PR you approve + deploy.

**Recommendation:** **Option A now** (close the P0 fast, safely, this engagement), and file Option B as a backend follow-up so the notification eventually lives at the record's home. A is reversible (`git revert`) and can't corrupt data — it only adds an outbound push.

---

## Decisions I need from you

1. **Events to notify on:** visit + call + token (recommended) — or a subset?
2. **Audience:** owner only (`101`, recommended) — or owner + team members (`101` + `103`)?
3. **Option A (bot-side, I ship now) vs B (backend, you approve a PR)** — recommend A now, B as follow-up.

## Build outline once signed off (Option A)
1. **Verify-first:** confirm `p_pg_id` is populated on live search results (read a real search) and that `sendNotificationOnCall` accepts the bot's payload (one careful live probe to a test pg_id).
2. **TDD:** `test_manager_notify.py` — helper builds the right payload from a booking result; fires only on confirmed success; swallows errors (never raises); not called on failure/empty. Register in `ci.yml` (gate 41→42).
3. Wire into `schedule_visit` / `schedule_call` / `payment` after the success check.
4. **Live-verify on prod:** schedule a visit for a test pg_id, confirm the push fires (backend `sendNotificationOnCall` log line / a manager device), confirm a forced push-failure still returns the normal user reply.
5. PR-per-item; CI green; squash-merge; update tracker.
