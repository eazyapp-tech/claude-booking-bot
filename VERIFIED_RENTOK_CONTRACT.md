<!-- AUTHORITATIVE. Source-verified against the live rentok-backend codebase
     (route -> controller -> service), re-verified by 4 parallel agents at ~95%
     accuracy on 2026-05-31. This file SUPERSEDES RENTOK_API.md: where the two
     disagree, THIS doc wins. Do not paraphrase or 'tidy' the body below -- it is
     the verbatim ground-truth contract. Re-verify with `graphify update .` if the
     backend changes. -->

RentOk Backend — Verified API Contract (Ground Truth)



Source-verified against the live rentok-backend codebase (Node/TS/Express), tracing
route → controller → service for all 22 endpoints + cross-cutting concerns.
Every claim below is backed by file:line in the backend. This replaces the
inferred/probed contract in RENTOK_API.md. Where this doc and your inferences
disagree, this doc wins.

Base URL: https://apiv2.rentok.com · Generated 2026-05-31



0. TL;DR — The 12 corrections that matter most







#



Your assumption



Verified reality





1



A5 GET /bookingBot/getAvailableRoomFromEazyPGID exists but 404s for some tenants



That route does not exist at all (live-confirmed real HTTP 404). Drop-in replacement is POST /bookingBot/get-room-details (same /bookingBot namespace, POST, richer fields). 404 only when eazypg_id is unknown. See A5 for the full A-vs-B comparison.





2



sub_XXXX is a Razorpay subscription/payment link



It's an internal 6-char random short code ([A-Za-z0-9]{6}). No gateway call, no paise conversion. "sub" is coincidental. But the link DOES expire — 7 days after creation (DB default ShortLink.expiry_at = now + INTERVAL '7 days'). See C2.





3



Aadhaar OTP via QuickEkyc



Two live flows exist. The single-a paths the bot calls (/checkIn/generateAadharOTP + /verifyAadharOTP) route to Cashfree. The app's main self-check-in (double-a /generateAadhaarOTP + /verifyAadhaarOTPAndPan) uses QuickEkyc — the newer vendor; Cashfree is the legacy path. The bot is currently on the legacy Cashfree path. See §7 — decide whether to stay or migrate.





4



D1 requires firebase_id, lead_source, lead_status



D1 requires only eazypg_id, phone, name, gender. firebase_id you send is overwritten server-side with the new tenant UUID.





5



HTTP status reflects success



Most bot endpoints return HTTP 200 with the real status in the body status field. Read body.status, not the HTTP code.





6



Brand isolation enforced server-side via API key / brand_hash



No auth, no brand isolation on bot routes. pg_id is trusted from the request body. A leaked pg_id = data read/write exposure.





7



Payment webhook is HMAC-verified



Cashfree inbound webhook is unverified (body-trust). No HMAC, no signature header. (Easebuzz HMAC verifier exists but is commented out.)





8



Rentok emits webhooks (payment-confirmed, visit-completed, lead-status-changed)



No outbound webhooks for any bot-relevant event. You must poll.





9



addLead / add-booking ping owners/sales reps



No SMS/WhatsApp/push fires on these paths. Dedup exists anyway.





10



reserveProperty holds a bed with a TTL / decrements inventory



No TTL, no expiry, no inventory touch. It's an append-only marker row. No oversell of beds (it doesn't track beds), but duplicate reservation rows are possible (TOCTOU race).





11



Filter enums like "All Boys"/"Boys"/"Male" are accepted



No enum exists. pg_available_for is free-text matched exactly; unit_types_available is array-overlap. Wrong casing → silent 0 results. Pull live values from A3 first.





12



A "check-only" mode of reserveProperty



Separate endpoint POST /bookingBot/checkPropetyReserved (note the typo "Propety").



0.5 Verification Ledger — all 22 endpoints, confidence-tagged

How to read the Confidence column (this is the trust contract — the method that replaces the earlier guess-as-fact mistakes):





🟢 LIVE — actually called against https://apiv2.rentok.com this session and the response shape was observed first-hand.



🔵 CODE — route → controller → service traced in source with file:line, but not live-called (mutating, or needs a test account). The contract is read off the code, which is authoritative for shape but not for live edge-cases.



⚪ INFERRED — anything not directly backed by code or a live call. There are none left in this doc — every row below is 🟢 or 🔵. If a future claim can't be tagged 🟢/🔵, it does not go in this doc.







#



Bot's documented path



Real backend path & method



Route → service file:line



Confidence



Live-test status





A1



getPropertyDetailsAroundLatLong



POST /property/getPropertyDetailsAroundLatLong



property.ts:1030 → PropertyService.ts:347



🟢 LIVE



Tested read-only. coords + double-wrap confirmed





A2



fetch-all-properties



POST /bookingBot/fetch-all-properties



bookingBot.ts:35 → svc bookingBot.ts:1224



🔵 CODE



⚠️ WRITES a recommendation row — not safely live-testable





A3



property-info



GET /bookingBot/property-info



bookingBot.ts:22 → bookingBot.ts:431



🟢 LIVE



Tested. pg_ids (plural) required





A4



property-details-bots



POST /property/property-details-bots



property.ts:1085 → PropertyService.ts:1359



🟢 LIVE



Tested. Body-status 404, real-500 on throw





A5



getAvailableRoomFromEazyPGID ❌



POST /bookingBot/get-room-details (drop-in)



bookingBot.ts:30 → bookingBot.ts:969



🟢 LIVE



Bot path → real 404; this one → 200. Fields confirmed





A5-alt



—



POST /rooms/getAvailableRoomsByEazyPGID



rooms.ts:185 → controllers/rooms.ts:2745



🟢 LIVE



Equivalent; fewer fields, real-404 on miss





A6



fetchPropertyImages



POST /bookingBot/fetchPropertyImages



bookingBot.ts:19 → bookingBot.ts:349



🔵 CODE



Read-only; not yet live-tested





B1



add-booking



POST /bookingBot/add-booking



bookingBot.ts:10 → bookingBot.ts:28



🔵 CODE



Mutating — needs test account





B2



cancel-booking



POST /bookingBot/cancel-booking



bookingBot.ts:11 → bookingBot.ts:80



🔵 CODE



Mutating — needs test account





B3



update-booking



POST /bookingBot/update-booking



bookingBot.ts:12 → bookingBot.ts:97



🔵 CODE



Mutating — needs test account





B4



reserveProperty



POST /bookingBot/reserveProperty



bookingBot.ts:21 → bookingBot.ts:400



🔵 CODE



Mutating (append marker) — needs test account





B4b



checkPropetyReserved



POST /bookingBot/checkPropetyReserved



bookingBot.ts:20 → svc



🔵 CODE



Read-ish; not yet live-tested





B5



booking/:user_id/events



GET /bookingBot/booking/:user_id/events



bookingBot.ts:8 → bookingBot.ts:132



🔵 CODE



Read-only; not yet live-tested





B6



shortlist-booking-bot-property



POST /bookingBot/shortlist-booking-bot-property



bookingBot.ts:26 → bookingBot.ts:668



🔵 CODE



Mutating — needs test account





C1



get-tenant_uuid



GET /tenant/get-tenant_uuid



tenant.ts:1104 → controllers/tenant.ts:29683



🔵 CODE



Read-only; not yet live-tested





C2



{tenant_uuid}/lead-payment-link



GET /tenant/{tenant_uuid}/lead-payment-link



tenant.ts:1103 → svc tenant.ts:3592



🔵 CODE



Mutating (creates short link, 7-day TTL) — needs test account





C3



addPayment



POST /bookingBot/addPayment



bookingBot.ts:34 → svc bookingBot.ts:1198



🔵 CODE



⚠️ Payment path — never casually live-test





D1



addLeadFromEazyPGID



POST /tenant/addLeadFromEazyPGID



tenant.ts:918 → controllers/tenant.ts:2572



🔵 CODE



Mutating (creates lead) — needs test account





E1



user-kyc/:user_id



GET /bookingBotKyc/user-kyc/:user_id



bookingBotKyc.ts:9 → svc bookingBotKyc.ts:63



🔵 CODE



⚠️ Writes (INSERTs kyc row) despite GET — not pure read





E2



booking/:user_id/kyc-status



GET /bookingBotKyc/booking/:user_id/kyc-status



bookingBotKyc.ts:7 → bookingBotKyc.ts:5



🔵 CODE



Read-only; not yet live-tested





E3



generateAadharOTP



POST /checkIn/generateAadharOTP



checkIn.ts:63 → svc checkIn.ts:3785



🔵 CODE



⚠️ Sends real OTP SMS (Cashfree cost) — never live-test





E4



verifyAadharOTP



POST /checkIn/verifyAadharOTP



checkIn.ts:64 → svc checkIn.ts:3813



🔵 CODE



Mutating — needs test account





E5



update-kyc



POST /bookingBotKyc/update-kyc



bookingBotKyc.ts:8 → bookingBotKyc.ts:23



🔵 CODE



Mutating — needs test account

Ground-truthing summary: 6 endpoints live-confirmed against production (A1, A3, A4, A5, A5-alt, + the bot's dead A5 path proven 404). The remaining 16 are code-verified to file:line and flagged for the reasons they weren't live-called (mutating / payment / real-SMS / writes-on-GET). Nothing in this doc is an unverified guess.



1. Auth, Multi-Tenancy & Transport (Tier 2 + Dimension C)

There is no authentication and no brand isolation on the booking-bot endpoints.





/bookingBot and /bookingBotKyc are mounted with zero middleware (server.ts:210-211). Each route is only wrapped in ErrorHandler (a try/catch, not auth — commonFunctions.ts:318).



/property and /tenant bot routes use HeaderValidator (commonFunctions.ts:1076), which does not enforce auth: it verifies a JWT only if an Authorization: Bearer header is present; no header = passes through (is_authenticated=false, commonFunctions.ts:1204). It rejects only expired/invalid tokens (419/501).



Brand scoping is by pg_id/eazypg_id in the request body — looked up via Property.findOne(...). There is no check that the caller "owns" that property. No brand_hash/brand_id exists server-side.

Implication for brand_hash isolation: your isolation model is a client-side convention only. Treat pg_id as a capability — if one leaks, that brand's data is readable/writable. Keep pg_id↔brand_hash mapping secret on your side; the server won't stop cross-brand access.

Rate limits / timeouts: None on /bookingBot, /bookingBotKyc, /checkIn. A Redis limiter exists but is applied only to specific OTP/password routes (returns 429 when it fires). No per-route server timeout.

Correlation IDs: A per-request UUID (req.id) is generated (commonFunctions.ts:1785) but only written to server logs, never returned in the body or as x-request-id. You cannot retrieve a correlation ID from responses.

Sandbox/staging: None. No src/config/ module, no NODE_ENV base-URL branching, internal re-dispatch URLs hardcoded to apiv2.rentok.com. There is no test pg_id gate. Test against production with real-but-disposable data.



2. The Response Envelope & Error Taxonomy (Tier 3)

Two response patterns coexist:





res.success / res.error (commonFunctions.ts:1881): this.status(status).json({status, message, data}) — HTTP status equals body status. Used on some /property, /tenant routes.



Bare res.json({status, message, data}) (most bot controllers): HTTP is always 200, real status is in body.status. This is the source of "HTTP 200 with inner status:400/404/500".

Always branch on body.status, not the HTTP code, for bot endpoints.

Envelope shape (both): { status: number, message: string, data?: T }.





No centralized errorHandler.ts, no PaymentError/ValidationError/NotFoundError classes. Status codes are hand-written per controller.



Uncaught throw → ErrorHandler → HTTP 500 {status:500, message, data}.



res.error defaults to status 400 when no status passed.



data type is inconsistent across success/error on several endpoints (e.g. [] on success, {} on error, or false). Defensive parsing required — see per-endpoint notes.



3. ID Model (Tier 1)







ID



What it is



Lives on



Used as key by





pg_id



Firebase-style UID (e.g. k0eZk8...MB2); identifies a brand account



Property



A1/A2/A3 brand scope (pg_ids[]), A4, A6, C2, C3





pg_number



int; identifies one physical property within a brand



Property



A4, A6, C2 (with pg_id)





id (a.k.a. property_id, the "p_id" UUID)



Property table PK (UUID)



Property



A4 (property_id), returned everywhere as id





eazypg_id



"RentOk ID", alternate human-facing property id



Property



A5 (sole key), C1, D1





tenant_uuid



Tenant table PK (UUID)



Tenant



C1 output, C2 path param





user_id



Your WhatsApp/customer key (free text)



booking_bot_* tables



B1–B6, C3, E1, E2, E5

Not interchangeable. A1/A2/A3 scope by pg_ids (array of pg_id); A4 by UUID or pg_id+pg_number; A5 by eazypg_id; A6 by pg_id+pg_number. phone is on Tenant; eazypg_id is on Property — C1 joins them.



4. Property Discovery (A1–A6) — Tier 1 + Dimension A



Mount prefixes: /property (property.ts), /bookingBot (bookingBot.ts), /rooms (rooms.ts). No auth on any of these.

A1 — POST /property/getPropertyDetailsAroundLatLong

Geo search. Route property.ts:1030 → PropertyService.ts:347.

Request (body):





coords — [lat,long][] or {lat,long}[] (both accepted). Empty → 400 "At least one coordinate pair is required".



pg_ids — string[], effectively required (used in IN; empty → 500). This is your brand scope.



radius — meters (no default; one hardcoded brand override to 200 km).



rent_starts_from (default 0), rent_ends_to (default 999999999).



property_type — string | CSV | string[].



pg_available_for — CSV, exact IN match (see §8 enums).



unit_types_available — CSV/array, PG array-overlap (&&).



tenant_preferred — exact match. sharing_types_enabled — number[] (<@). property_ids — exclusion list (NOT IN). is_test, images_present — bool.

Always-on filters: is_deleted=false, is_locked=false, disabled_website=false, images_present, and a vacancy filter (SUM(room.sharing_type) > COUNT(active tenants)) — fully-occupied properties are excluded.

Ranking & geo (your Dimension A question): Two-stage. (1) PostGIS st_distancesphere < radius (straight-line sphere, not bounding box). (2) Google Distance Matrix API for road distance from coords[0], keeps distance.value <= radius, sorts ascending by road distance → results are ranked server-side by driving distance. Null-coordinate properties survive stage 1 but are dropped in stage 2 (Google returns non-OK for null,null), so they don't leak into final results.

Response — ⚠️ DOUBLE-WRAPPED (live-confirmed): the service returns its own {status,message,data:{results,total}} envelope (PropertyService.ts:555), and the controller wraps that whole object inside its own data (property.ts:23201 does res.status(200).json({status:200, message:"Success", data: response})). So the real payload is two levels deep:

{ status:200, message:"Success",
  data: { status:200, message:"Success",
          data: { results: Property[], total: number } } }

Read results at body.data.data.results and count at body.data.data.total — NOT body.data.results. Rows are flat aliased (getRawMany): p_id, p_pg_name, p_pg_id, p_eazypg_id, p_lat, p_long, pm_*_amenities, property_image, distance, etc. Empty radius → inner data:{results:[], total:0} at HTTP 200. Two distinct 500s: a geo/query failure surfaces as inner data.data={status:500,...} still at HTTP 200; a thrown exception in the controller surfaces as a real HTTP 500 with {status:500, data:[]} (property.ts catch).

A2 — POST /bookingBot/fetch-all-properties

Text match. Route bookingBot.ts:35 → bookingBot.ts:1224.
Request (body): pg_ids (required), search_query (optional). Matches pg_name ONLY (LOWER(pg_name) LIKE %q%) — not address/locality. No search_query → returns all for pg_ids.
Response: {status:200, data: Property[]}. Each item includes microsite_data:{...} with min_token_amount, security_deposit, notice_period, faqs, reviews, customer_support_*, etc. microsite_link = https://rentok.com/property/${prop_id}. ⚠️ Several top-level fields are absent from the SELECT when search_query is set → come back undefined. data is array on success, {} on error.

A3 — GET /bookingBot/property-info ⭐ call this FIRST for filter values

Brand aggregate. Route bookingBot.ts:22 → bookingBot.ts:431. Query param, not body.
Request: pg_ids (required; pass repeated pg_ids=...&pg_ids=...). Missing → 400.
Computed live, NOT stored: returns arrays of every distinct value across the brand (not min/max ranges).
Response data: { rent:number[], token_amount:number[], property_type:string[], tenants_preferred:string[], services_amenities:[], emergency_stay_rate:500 (hardcoded), unit_types_available:string[], sharing_types_enabled:number[], common_amenities:[], pg_availability:string[], uniqueAmenityNames:string[], address:string[] }. Error → data:false.
Use pg_availability and unit_types_available from here as the exact strings to feed back into A1.

A4 — POST /property/property-details-bots

Full details. Route property.ts:1085 → PropertyService.ts:1359.
Request (discriminated): property_id (UUID) OR both pg_id+pg_number. Else 400.
Response: {status:200, data:{property:<full entity>, propertyMicrosite:{...}}}. Not found → {status:404, data:{}}. ⚠️ The controller returns the service's object via res.status(200).json(response), so service-returned 400/404 come back as HTTP 200 with the real code in body.status — always read body.status, not the HTTP code. But an unexpected thrown exception is caught and returned as a real HTTP 500 (res.status(500).json(...), property.ts:23492). So: validation/not-found → HTTP 200 + body-status; unhandled crash → HTTP 500.

A5 — Room details (your GET /bookingBot/getAvailableRoomFromEazyPGID)

Your inferred path does not exist. Your own doc live-tested it → HTTP 404 "Cannot GET /bookingBot/getAvailableRoomFromEazyPGID" (March 2026, OxOtel) — confirmed: no such route is registered anywhere. It is not a per-tenant deploy gap; the path simply isn't in the router. Also wrong method — there is no GET variant; both real equivalents are POST.

Two working equivalents exist. Prefer the first — it's a drop-in in the namespace you already call:











A (recommended) — POST /bookingBot/get-room-details



B — POST /rooms/getAvailableRoomsByEazyPGID





Route → handler



bookingBot.ts:30 → getAvailableRooms → BookingBotService.getAvailableRoom (bookingBot.ts:969)



rooms.ts:185 → controllers/rooms.ts:2745





Namespace



Same /bookingBot you already use



Different /rooms mount





Validation



none



Zod { eazypg_id } required ("RentOk ID is required")





Body



{ eazypg_id }



{ eazypg_id }





Unknown id



{status:404, data:{}} at HTTP 200 (body-status)



real HTTP 404 {status:404, message:"Property Not Found"}





data.rooms[] fields



id, name, rent, tags, type_tags, sharing_type (richer)



id, name, rent only

Logic (both): Property.findOne({eazypg_id}) → Room.find({property.id, rent_disable:false, unit_type != "BED"}), ordered by floor then name. Wrapper: {status:200, message:"Success", data:{ rooms:[...], pg_name, pg_id, pg_number, brand_color, pg_logo, pg_address }}. Rooms are always at data.rooms (resolves your "rooms or data" branch — it's never data as the array).

⚠️ Semantic correction: your doc (line 329) says this returns "live bed availability per room… real-time counts." It does not. Both endpoints filter unit_type != "BED", so they return room-level configurations (name, rent, sharing type) — not bed counts and not real-time availability. There is no per-bed-count field in the response. If you need live bed availability, that's the reserve/checkPropetyReserved path (§B), not this one.

Microsite variant if needed: POST /rooms/getAvailableRoomsForMicrosite.

A6 — POST /bookingBot/fetchPropertyImages

Route bookingBot.ts:19 → bookingBot.ts:349. No validation. (Distinct from /property/fetchPropertyImages, which is validated — use the bookingBot one.)
Request (body): { pg_id, pg_number } — both required (not eazypg_id). Missing → 400 data:[].
Response: {status:200, message:"Success"|"No Images Found", data: string[]} — always a flat array of URL strings, never objects. Error → {status:500} (no data).



5. Bookings & Scheduling (B1–B6) — Tier 2 + Dimension A



All under /bookingBot, no auth/validation, ErrorHandler-wrapped, HTTP 200 with body status.
No notifications fire on any of these (no SMS/WhatsApp/push/CRM).

B1 — POST /bookingBot/add-booking  (bookingBot.ts:10 → service bookingBot.ts:28)

Request (body): user_id, visit_date, property_id (de-facto required); visit_time, visit_type, property_name (optional). All varchar(255).
visit_type is free-form — NO enum. "visit"/"call"/"video tour" are your conventions; backend stores any string verbatim and never branches on it. No per-type required fields.
Dedup (the "200 with status:400"): rejects if a row exists for {user_id, property_id} ("A visit for this property already exists") or {user_id, visit_date} ("You already have a visit scheduled for this date"). A user can hold only one booking per calendar date. Not idempotent — hard reject.
Response: success {status:200, message:"User data saved successfully", data:<row>}; dup {status:400, data:<existing>}; error {status:500}.

B2 — POST /bookingBot/cancel-booking  (bookingBot.ts:11 → bookingBot.ts:80)

Request: user_id, property_id.
Hard DELETE, not a status change. TypeORM .delete() returns affected:0 for no match without throwing → always returns {status:200, message:"Property cancelled successfully"} even for nonexistent IDs (your observation confirmed). No data.

B3 — POST /bookingBot/update-booking  (bookingBot.ts:12 → bookingBot.ts:97)

Request: user_id+property_id (WHERE keys), and any of visit_date/visit_time/visit_type (only truthy ones applied — can't clear a field).
Response: no fields → {status:400,"No fields to update"}; success → {status:200, success:true, data:<changed fields>} (only endpoint with success:true). Updating a nonexistent booking still returns success (affected:0, no throw).

B4 — POST /bookingBot/reserveProperty ⭐  (bookingBot.ts:21 → bookingBot.ts:400)

Request: user_id, property_id.
Semantics (your critical questions):





No TTL, no expiry, no scheduler. Inserts a row {id, user_id, property_id, created_at} into booking_bot_reserved_properties. Permanent until explicitly deleted.



Does NOT touch inventory (no Room/bed/Property writes). "Reserving" = a per-(user,property) marker, not a bed hold.



No oversell of beds (beds aren't tracked here), but check-then-insert with no transaction and no unique constraint → concurrent reserves for the same (user_id, property_id) can create duplicate rows (TOCTOU). Unlimited users can "reserve" the same property.
Response: already → {status:400,"Property already reserved"}; success → {status:200,"Property reserved successfully"}; error → {status:500}.
"Check-only mode" is a separate endpoint: POST /bookingBot/checkPropetyReserved (typo intentional) → {status:200, message:"Property reserved"|"Property not reserved", data:<bool>}, no write.

B5 — GET /bookingBot/booking/:user_id/events  (bookingBot.ts:8 → bookingBot.ts:132)

Request: user_id as path param.
Response: {status:200, data: [{property_id, visit_date, visit_time, visit_type, property_name}]} — projected (no id/created_at/flags). Returns all the user's bookings across all properties. Empty → []; error → data:{}.

B6 — POST /bookingBot/shortlist-booking-bot-property  (bookingBot.ts:26 → bookingBot.ts:668)

Request: user_id, property_id, property_contact — all explicitly required (only in-scope endpoint with real validation).
Logic: pushes property_id into booking_preferences.shortlisted_properties for {user_id, property_contact} (dedup within array). ⚠️ UPDATE only — if no booking_preferences row exists for that {user_id, property_contact}, the shortlist is silently not persisted (no upsert).
Response: missing field → {status:400}; success → {status:200, data:<UpdateResult>}.

Booking state machine (Dimension A) — there isn't one

No status enum, no transition guards. State is implied by table presence + 3 flags:





"Scheduled" = row in booking_bot_users (created by B1, deleted by B2).



visit_type = free-form varchar (no enum).



bed_reserved int 0→1 (set by /reserve-bed, never reset).



payment_status int →1 (set by /update-payment, never reset).



"Reserved/Token" = row in booking_bot_reserved_properties (B4).
No ordering enforced (nothing requires a visit before reserving, or KYC before payment, at this layer). "Visit Scheduled"/"Token" are not stored strings.



6. Payments & Lead Management (C1–C3, D1) — Tier 1/2 + Dimension A/B



⚠️ Production financial system. All amounts elsewhere in RentOk are paise-integers, but see C2/C3 caveats.

C1 — GET /tenant/get-tenant_uuid  (tenant.ts:1104 → controllers/tenant.ts:29683)

Request (query): phone (required), eazypg_id (required).
Resolution: Tenant.findOne where phone AND lead_source="bookingBot00" AND status=3 (lead) AND property.eazypg_id. Only resolves bookingBot-sourced leads.
Response: {status:200, data:{tenant_uuid:<Tenant.id UUID>}}; not found → 404; DB error → 400.
⚠️ Cross-cutting gotcha: C1 finds only lead_source="bookingBot00". If you create a lead via D1 with lead_source="Token" (or anything else), C1 will not find it. Use "bookingBot00" as lead_source in D1 if you intend to look it up via C1.

C2 — GET /tenant/{tenant_uuid}/lead-payment-link  (tenant.ts:1103 → service tenant.ts:3592)

Request: tenant_uuid (path), and query: pg_id, pg_number, amount — all required (any falsy → 400 "Invalid request"). Tenant must exist with status=3 + matching property.
The sub_XXXX link is internal, NOT Razorpay. createShortLink (tenant.ts:3684) makes a ShortLink row with link = generate6DigitCode() — 6 random [A-Za-z0-9] chars (commonFunctions.ts:338), type='payment_page'. No Razorpay/gateway/axios call in this endpoint. The "sub" prefix is random coincidence.
⚠️ The link EXPIRES after 7 days. The service doesn't set an expiry, but the ShortLink entity has a DB-level default expiry_at = CURRENT_TIMESTAMP + INTERVAL '7 days' (entities/short_link.ts:24). Treat sub_XXXX payment links as valid for 7 days from creation, not permanent — regenerate via C2 if older.
amount is passed through as a string — NOT converted to paise here. Stored verbatim in the ShortLink body (is_booking_bot_payment:true, is_vendor:true, collect_online_payment:true, pending_dues_amount:amount, etc.). Retries up to 3×.
Response: {status:200, data:{link:<6-char code>, pg_name}}. Logical errors → body.status:400 but HTTP 200; thrown → HTTP 500.
The actual payment page is rendered downstream by resolving that ShortLink (out of this endpoint's scope) — that's where the real gateway lives.

C3 — POST /bookingBot/addPayment  (bookingBot.ts:34 → service bookingBot.ts:1198)

No auth, no validation. Real handler is addBookingBotPayement (not the generic addPayment).
Request (body, all optional): user_id, pg_id, pg_number, short_link, amount.
Persists one INSERT into booking_bot_payments: {user_id, pg_id, pg_number, short_link, token_amount=amount, created_at, updated_at}. Note rename amount→token_amount.
Atomicity/verification: single insert (trivially atomic), no DB transaction, and it does NOT verify with any gateway — it blindly records what you send. No status/type enum. Pure bookkeeping.
Response: {status:200, message:"Successfully inserted", data:{}}; error → {status:500}. No receipt/notification.

D1 — POST /tenant/addLeadFromEazyPGID  (tenant.ts:918 → controllers/tenant.ts:2572)

Middleware: HeaderValidator → ValidateRequest(Zod).
Required (Zod, the ONLY 400 source): eazypg_id (min 1), phone (min 10), name (min 1), gender ("Male"/"Female"/"Any").
Optional (read raw, unvalidated): facilities, father_name, father_phone, rent_range, room_type, staff_id, staff_phone, staff_name, lead_status, lead_token_amount, lead_source, remarks, visit_date (YYYY-MM-DD), visit_time, visit_type, created_at, firebase_id.
Correction: firebase_id/lead_source/lead_status are NOT required. And firebase_id you send is overwritten with the new tenant UUID immediately after save (tenant.ts:2643).
Dedup (your "401 = duplicate"): key = phone + property_id (from eazypg_id) + status=3. Existing → HTTP 401 {status:401,"Lead already exists!"}. Create-only — never an upsert. Unknown eazypg_id → 404 "Property not found!".
Enums (free-text, no DB enforcement): observed lead_source: bookingBot00, Booking Bot, Justdial, Magicbricks, housing, 99Acres, olx. Observed lead_status: New Lead, Visit Done, Added as tenant. Any string accepted; no transition rules. Use lead_source="bookingBot00" if you want C1 to find the lead.
Response: 200 "Lead created!"; 401 dup; 404 no property; 500 {message:"Error!"} (no status key in this branch); 400 from Zod.
Side effects: None — no SMS/WhatsApp/Slack/Firebase push/sales-rep notification. The only post-save action is the firebase_id self-update.



7. KYC / Aadhaar (E1–E5) — Tier 1 + Dimension A



All unauthenticated. E1/E2/E5 controllers use bare res.json (HTTP 200, real status in body).
Aadhaar vendor — the bot's single-a paths use Cashfree; the app's newer double-a self-check-in uses QuickEkyc. Both are live (see the two-flow table below).

Two parallel Aadhaar OTP flows live in the same SelfCheckIn class (services/tenant/checkIn.ts):







Path (as the bot calls it)



Controller → service method



Vendor





POST /checkIn/generateAadharOTP (single-a) + POST /checkIn/verifyAadharOTP



generateAadharOTP (L3785) / verifyAadharOTP (L3813)



Cashfree (L3789, L3818) — legacy, but still live; this is what the bot hits today





GET /checkIn/generateAadhaarOTP (double-a) + POST /checkIn/verifyAadhaarOTPAndPan



generateOTP (L46) / verifyAadhaarAndPanUsingOTP (L102)



QuickEkyc (L81, L197) — newer; what the rest of the app uses

The Cashfree→QuickEkyc migration is visible in controllers/teamMember.ts:2646-2651 (Cashfree call commented out, replaced by QuickEkyc.generateAadharOTP). Action item for the bot: the single-a Cashfree path still works, but it is the older vendor and may be deprecated. Confirm with the backend team whether to (a) stay on Cashfree single-a, or (b) migrate to the QuickEkyc double-a flow — note the double-a flow's request/response shape differs (it's a combined Aadhaar+PAN verify, GET for generate, and returns a different envelope), so it is not a drop-in swap.

E1 — GET /bookingBotKyc/user-kyc/:user_id  (bookingBotKyc.ts:9 → service bookingBotKyc.ts:63)

Init KYC row. user_id = path param.
Existing row → {status:400,"User already has KYC entry", data:<row>}. Else inserts {kyc_status:0, kyc_data:''} → {status:200, data:{id, user_id, kyc_status:0, kyc_data:""}}.

E2 — GET /bookingBotKyc/booking/:user_id/kyc-status  (bookingBotKyc.ts:7 → bookingBotKyc.ts:5)

user_id = path param.
Response: {status:200, message:"Success", data:{kyc_status:<value>}}. Values: 0 (created, not done), 1 (verified). undefined/omitted if no row exists. Only 0 and 1 are ever written. Your "1 = verified" is correct.

E3 — POST /checkIn/generateAadharOTP  (checkIn.ts:63 → service checkIn.ts:3785)



Note lowercase Aadhar — distinct from /generateAadhaarOTP at checkIn.ts:34 (different path).
Request (body): aadhar_number (12-digit), user_phone_number.
Vendor: Cashfree — synchronous axios.post https://api.cashfree.com/verification/offline-aadhaar/otp. The returned ref_id is stored server-side in Redis under ${user_phone_number}_aadhaarRefId (24h TTL) — not returned for you to echo.
Response: {status:200, message:"OTP sent successfully", data:{ref_id}}. Failures pass through Cashfree's body (e.g. 400 "Otp already generated", 502 "Error From UIDAI").

E4 — POST /checkIn/verifyAadharOTP  (checkIn.ts:64 → service checkIn.ts:3813)

Request (body): otp, user_phone_number. ref_id is NOT sent by you — fetched from Redis by phone, so E3 and E4 must use the same user_phone_number.
Vendor: Cashfree — offline-aadhaar/verify with {otp, ref_id}.
Verified identity returned (under data): status:"VALID", name, gender ("M"/"F"), dob ("DD-MM-YYYY"), year_of_birth, care_of, address, email (hashed), mobile_hash, photo_link (base64 JPEG), split_address:{country, dist, house, landmark, pincode, postOffice, state, street, subdist, vtc}. ⚠️ The exact nesting of data mirrors Cashfree's envelope (may itself nest under data) — handle defensively. Mock refIds 000000/000001 return a flat identity object.
Failure: vendor !=200 → {status:400,"OTP verification failed"}; expired → "Session expired, please generate a new OTP". E4 persists nothing — persistence is your job via E5.

E5 — POST /bookingBotKyc/update-kyc  (bookingBotKyc.ts:8 → service bookingBotKyc.ts:23)

Request (body): user_id, kyc_data (stored to a text column — no schema enforced, stored verbatim; typically the E4 identity object).
⚠️ Order-of-ops quirk: the service UPDATEs (kyc_status=1, kyc_data=...) then re-reads and checks kyc_status===1, which is now always true → returns {status:400, "KYC status is already updated", data:<row with kyc_status:1>} even on the first successful write. Treat the DB write as succeeded regardless of the 400 body. The status:200 branch is only reachable when no row existed (affected:0).





{status:400,"KYC data is missing"} if kyc_data falsy. {status:500} on exception.
Side effects: flips kyc_status→1 + stores kyc_data. No downstream events/notifications. E2 reads the flag later.

KYC lifecycle: 0 (E1) → 1 (E5). E3/E4 don't touch the KYC table; OTP ref_id lives only in Redis (<phone>_aadhaarRefId, 24h).



8. Filter Enum Values — the silent-zero-results trap (Tier 1)

There is no enum file for pg_available_for or unit_types_available. Both are per-property data:





pg_available_for — free-text string column. A1 matches with exact IN. Stored values are mixed phrases like "Male & Female". Passing "Boys"/"Male"/"All Boys" when the row stores "Male & Female" → silent 0 results. (Other parts of the codebase use .toLowerCase().includes('boys'/'girls'), so there's no single canonical casing.)



unit_types_available — string[] column, A1 uses array-overlap (&&). Stored tokens are uppercase like SINGLESHARING, DOUBLESHARING, TRIPLESHARING.

The reliable pattern: call A3 GET /bookingBot/property-info?pg_ids=... first, then feed the exact strings from pg_availability and unit_types_available back into A1's filters. Never hardcode "All Boys"/"Boys"/"Male".



9. Events Back & Webhooks (Dimension B)





No outbound webhooks for payment-confirmed, visit-completed, or lead-status-changed. The only outbound webhook in the codebase is an unrelated PV-Tool integration. You must poll (e.g. B5 for events, E2 for KYC, your own payment short-link resolution).



Inbound payment webhook POST /payment/cashfreeWebhook (controllers/payment.ts:5983): no signature verification — body is trusted, safety comes from an InitiatedTransactions order-id lookup. No HMAC secret to verify against. (An Easebuzz HMAC verifier exists but is commented out at the route.) If your bot HMAC-verifies inbound payment webhooks, that scheme does not match this backend.



10. Highest-Leverage Next Artifact (your meta-question)

Given the findings, the single most useful thing to build on your side is a typed client wrapper that encodes these realities (not an OpenAPI export — the backend has none and the routes aren't schema-described):





Status interceptor: read body.status, not HTTP status, for all /bookingBot, /bookingBotKyc, A4, C2 calls. Map body.status → success/error.



Filter resolver: always hit A3 before A1; cache pg_availability + unit_types_available per pg_ids and only ever pass values from that set.



Endpoint corrections baked in: A5 → POST /rooms/getAvailableRoomsByEazyPGID; reserve check → /bookingBot/checkPropetyReserved.



Idempotency awareness: B1/D1 reject duplicates (status 400 / HTTP 401); B2/B3 silently succeed on no-match — don't treat 200 as proof of effect.



Lead lookup contract: create D1 leads with lead_source="bookingBot00" so C1 can resolve tenant_uuid.



Defensive data typing: data may be array / object / false / {} across success/error on A2/A3/B5/etc. — parse permissively.



No server-side notifications/inventory/reservation-TTL — your bot must own dedup-before-human-ping, bed-availability truth (re-query A5/A1 vacancy), and any reservation expiry.



All line numbers reference the rentok-backend repo at the time of extraction. Re-verify against graphify update . if the backend changes.
