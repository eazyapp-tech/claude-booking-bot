# RentOK API Reference

> ⚠️ **SUPERSEDED (2026-05-31) by [`VERIFIED_RENTOK_CONTRACT.md`](VERIFIED_RENTOK_CONTRACT.md).**
> That document is source-verified against the live rentok-backend codebase
> (route → controller → service, re-verified by 4 agents at ~95% accuracy) and
> **wins wherever the two disagree.** This file is *inferred/probed* and is kept
> only for its historical test-result detail. Before trusting any endpoint shape,
> ID model, or filter enum here, cross-check the verified contract — several claims
> below were corrected there (notably A5 `getAvailableRoomFromEazyPGID`, which is
> not a real route, and the `lead_source="bookingBot00"` requirement for C1).
>
> **Reverse-engineered from source code + live integration testing (March 2026)**
> Base URL: `https://apiv2.rentok.com`
> Purpose: Complete reference for developers working on the EazyPG booking bot integration. Produced independently of the RentOK team.

---

## Table of Contents

1. [Overview & Conventions](#1-overview--conventions)
2. [ID Types Quick Reference](#2-id-types-quick-reference)
3. [Response Shape Gotchas](#3-response-shape-gotchas-critical)
4. [Endpoint Reference](#4-endpoint-reference)
   - [A. Property Discovery](#a-property-discovery)
   - [B. Bookings & Scheduling](#b-bookings--scheduling)
   - [C. Payments](#c-payments)
   - [D. Lead Management](#d-lead-management)
   - [E. KYC / Identity Verification](#e-kyc--identity-verification)
5. [Lead Enrichment Opportunity](#5-lead-enrichment-opportunity)
6. [Known Behaviours & Limitations](#6-known-behaviours--limitations)
7. [Production Bug Report](#7-production-bug-report)

---

## 1. Overview & Conventions

### Authentication
No authentication headers are used in any observed requests. All endpoints are called with plain HTTP `POST`/`GET` and JSON bodies. The API appears to rely on `pg_ids` (whitelabel property group IDs) and `user_id` fields for tenant isolation rather than header-based auth.

### HTTP Client
- All requests use `httpx.AsyncClient` with a **15-second timeout** (30s for property search)
- `Content-Type: application/json` for POST requests
- Several tools wrap calls in `utils/retry.py:http_post` which adds **2 retries with exponential backoff**

### Date Format
All `visit_date` fields use **`DD/MM/YYYY`** format (e.g., `"15/03/2026"`).
The bot's `utils/date.py:transcribe_date()` converts natural language ("tomorrow", "15 March") into this format before calling any API.

### Visit/Call Types
The `visit_type` field accepts exactly these values:
- `"Physical visit"` — in-person visit
- `"Phone Call"` — phone call with property
- `"Video Tour"` — video call tour

### Time Format
`visit_time` is passed as a string exactly as the user states it. Observed formats: `"10:00 AM"`, `"2:30 PM"`. No normalisation is applied.

---

## 2. ID Types Quick Reference

Understanding the different IDs is critical — they are not interchangeable.

| ID | Field Name(s) | Format | Description |
|---|---|---|---|
| **pg_id** | `pg_id`, `p_pg_id` | Firebase UID string (e.g., `l5zf3ckOnRQV9OHdv5YTTXkvLHp1`) | Whitelabel property owner / property group ID. Used to scope API responses to a brand's portfolio. Multiple per brand. |
| **eazypg_id** | `eazypg_id`, `p_eazypg_id` | Alphanumeric string (e.g., `4000053335H`, `4000043334AC`) | Room-level identifier assigned by RentOK. One per bed/room configuration. Returned by the search API (`p_eazypg_id` field). Required for lead creation and payment flows. |
| **pg_number** | `pg_number`, `p_pg_number` | Integer | Property sub-number within a pg_id group. Used alongside `pg_id` in payment and image calls. |
| **property_id** | `property_id`, `p_id` | UUID v4 string (e.g., `6087ef52-8755-44cc-9551-b80fd43958cb`) | Internal booking system UUID used for `property-details-bots`, scheduling, cancellation, and rescheduling. Returned by the search API as the `p_id` field. **Must be this UUID — passing the Firebase `pg_id` causes HTTP 500 from the property-details endpoint.** |
| **tenant_uuid** | `tenant_uuid` | UUID v4 | RentOK CRM tenant identifier. Looked up by `(phone, eazypg_id)` pair. Required for payment link generation. |
| **firebase_id** | `firebase_id` | String | Client-generated deduplication key for leads. Convention: `cust_YYYY_MM_DD_HH_MM_SS`. |

---

## 3. Response Shape Gotchas (CRITICAL)

The RentOK API is **inconsistent** about how it signals success. Do **not** assume a `success: true` field will be present. Use the table below:

| Endpoint | HTTP Success | `success` Field | How to Check Success |
|---|---|---|---|
| `POST /bookingBot/add-booking` | 200 | ❌ Not present | Check `resp.status_code == 200` + `raise_for_status()`. **Duplicate = HTTP 200 with inner `{"status": 400, "message": "A visit for this property already exists..."}` — NOT an outer HTTP 400.** Check `data.get("status") == 400` after parsing. |
| `POST /bookingBot/cancel-booking` | Always 200* | ❌ Not present | `raise_for_status()` only. Even fake booking IDs return HTTP 200. |
| `POST /bookingBot/update-booking` | 200 | ✅ `success: true` | `data.get("success")` is reliable here. |
| `POST /bookingBot/reserveProperty` | 200 | ✅ `success: true` OR `reserved: true` | Check `data.get("success") or data.get("reserved")` for check-only mode. |
| `POST /tenant/addLeadFromEazyPGID` | 200 | ❌ Not present | `message: "Lead created!"`. Use `raise_for_status()`. |
| `POST /bookingBot/addPayment` | 200 | ❌ Not present | `raise_for_status()` only. |
| `POST /property/getPropertyDetailsAroundLatLong` | 200 | ❌ — but inner status! | Check `data["data"]["status"] != 500`. HTTP 200 can hide inner errors. |
| `POST /checkIn/verifyAadharOTP` | 200 | — | Check `resp_data.get("status") == 400` for OTP failure (not HTTP 400). |
| `GET /tenant/get-tenant_uuid` | 200 | — | Use `check_rentok_response()` + `data["data"]["tenant_uuid"]`. |
| `GET /tenant/{uuid}/lead-payment-link` | 200 | — | Use `check_rentok_response()` + `data["data"]["link"]`. |

*`cancel-booking` returns HTTP 200 even for non-existent booking IDs. The API does not distinguish between "cancelled a real booking" and "nothing to cancel".

---

## 4. Endpoint Reference

---

### A. Property Discovery

---

#### A1. Property Search by Location

```
POST /property/getPropertyDetailsAroundLatLong
```

The primary discovery endpoint. Searches properties within a radius of given coordinates, filtered by the brand's `pg_ids`.

**Request Body**

| Field | Type | Required | Example | Notes |
|---|---|---|---|---|
| `coords` | `[[float, float]]` | ✅ | `[[19.1136, 72.8697]]` | Array of `[lat, lng]` pairs. Pass one pair. |
| `radius` | `int` | ✅ | `20000` | Search radius in **metres**. Default: 20,000. Max observed: 35,000. |
| `pg_ids` | `string[]` | ✅ | `["l5zf3ckOnRQV...", "egu5Hm..."]` | Brand's whitelabel pg_ids. Must be non-empty or API returns nothing. |
| `rent_ends_to` | `int` | ✅ | `25000` | Maximum rent filter (₹). Send `10000000` for no upper limit. |
| `rent_starts_from` | `int` | ❌ | `8000` | Minimum rent filter (₹). Omit if no lower bound. |
| `unit_types_available` | `string` | ❌ | `"Private Room"` | Filter by room type. ⚠️ **Returns 0 results for all tested values** — do not pass unless confirmed server-side values for the RentOK tenant are known. |
| `pg_available_for` | `string` | ❌ | `"All Boys"` | Values: `"All Boys"`, `"All Girls"`. Omit for co-living/any. ⚠️ **Only matches explicitly gender-labeled properties** — returns 0 for properties tagged "Any". |
| `sharing_type_enabled` | `string` | ❌ | `"Single"` | Filter by sharing preference. |

**Response Shape**

```json
{
  "data": {
    "status": 200,
    "data": {
      "results": [
        {
          "p_id": "6087ef52-8755-44cc-9551-b80fd43958cb",
          "p_pg_id": "l5zf3ckOnRQV9OHdv5YTTXkvLHp1",
          "p_pg_number": 3,
          "p_eazypg_id": "4000053335H",
          "p_pg_name": "OXO ZEPHYR RABALE",
          "p_address_line_1": "Plot 7, TTC Industrial Area",
          "p_city": "Navi Mumbai",
          "p_personal_contact": "02222334455",
          "p_rent_starts_from": 5000,
          "p_image": "https://rentok-storage-cdn.azureedge.net/rentok-marketplace/RoomImages/...",
          "p_type": "PG",
          "p_available_for": "All Boys",
          "p_amenities": "WiFi, AC, Meals",
          "p_sharing_types": ["Single", "Double"],
          "p_lat": 19.1136,
          "p_long": 72.8697
        }
      ]
    }
  }
}
```

> **⚠️ Live-tested field name corrections (March 2026):**
> - Property name is `p_pg_name` — **not** `p_name`
> - Location is split into `p_address_line_1` + `p_city` — **not** `p_location`
> - `p_id` is a **UUID v4 string** — **not** an integer
> - Property contact phone: `p_personal_contact`
> - Images hosted at `https://rentok-storage-cdn.azureedge.net/rentok-marketplace/RoomImages/...`

**Gotchas**
- HTTP 200 does **not** guarantee success. Always check `data["data"]["status"] != 500` before processing results.
- `p_eazypg_id` is a **room-level** ID — there can be multiple results with the same property name but different `p_eazypg_id` values (different room configurations).
- Results are not paginated — all matching properties are returned in one response.
- The bot caches results per search payload to reduce API load.
- Use `p_id` (UUID) when calling `property-details-bots`. Do **not** use `p_pg_id` (Firebase UID) as the `property_id` for that endpoint.

---

#### A2. Fetch All Brand Properties

```
POST /bookingBot/fetch-all-properties
```

Returns a simplified list of all properties belonging to the brand's `pg_ids`. Used for text-query matching (e.g., "find Purva Sugandha").

**Request Body**

| Field | Type | Required | Example |
|---|---|---|---|
| `pg_ids` | `string[]` | ✅ | `["l5zf3ckOnRQV...", "egu5Hm..."]` |

**Response Shape**

```json
{
  "data": [
    {
      "id": "6087ef52-8755-44cc-9551-b80fd43958cb",
      "pg_id": "l5zf3ckOnRQV9OHdv5YTTXkvLHp1",
      "pg_name": "OXO ZEPHYR RABALE",
      "microsite_link": "https://www.oxotel.in/property/oxo-zephyr-rabale",
      "microsite_data": {
        "about": "Modern co-living near Rabale IT park...",
        "notice_period": "30 days",
        "security_deposit": "₹5,000",
        "property_amenities": ["WiFi", "AC", "CCTV", "Gym"],
        "room_amenities": ["Attached Bath", "AC"],
        "customer_support_number": "9876543210",
        "customer_support_whatsapp": "9876543210",
        "faqs": [...],
        "reviews": [...]
      }
    }
  ]
}
```

> **⚠️ Live-tested field name corrections (March 2026):**
> - Property name is `pg_name` — **not** `property_name` or `name`
> - UUID is at `id` — **not** `p_id`
> - Firebase UID is at `pg_id`
> - No top-level `location`, `rent_starts_from`, or `address` fields
> - Rich metadata is in the nested `microsite_data` object
> - Top-level response key is `data` (array), not `properties`

**Gotchas**
- Use `data.get("data", [])` to access the array — there is no `properties` key at top level.
- Query by `pg_name` for text matching — **not** `property_name` or `name`.
- The `id` field (UUID) in this response corresponds to `p_id` from the search API and is the correct `property_id` to pass to `property-details-bots`.
- `microsite_data` may be `null` for properties without a configured microsite.

---

#### A3. Brand & Property Aggregate Info

```
GET /bookingBot/property-info
```

Returns aggregate information across all properties for a brand — rent ranges, amenity lists, property types. Used by the default agent's `brand_info` tool. Cached in Redis for 24 hours.

**Query Parameters**

| Param | Type | Required | Example | Notes |
|---|---|---|---|---|
| `pg_ids` | `string` | ✅ | `"l5zf3ckOnRQV,egu5HmrY"` | **Comma-separated string**, not an array. |

**Response Shape**

```json
{
  "data": {
    "rent": "₹5,000 - ₹18,000",
    "token_amount": "₹1,000",
    "property_type": "PG, Co-living",
    "tenants_preferred": "Working Professionals, Students",
    "unit_types_available": "Private Room, Double Sharing",
    "sharing_types_enabled": "Single, Double, Triple",
    "pg_availability": "All Boys",
    "common_amenities": "WiFi, Security, Laundry",
    "uniqueAmenityNames": "Rooftop, Gaming Zone",
    "services_amenities": "Housekeeping, Meals",
    "emergency_stay_rate": "₹500/night",
    "address": "Andheri, Mumbai"
  }
}
```

---

#### A4. Full Property Details

```
POST /property/property-details-bots
```

Returns comprehensive property details: amenities, rules, FAQs, notice period, agreement terms, room types, reviews. Falls back to the Redis search cache if response is sparse.

**Request Body**

| Field | Type | Required | Example | Notes |
|---|---|---|---|---|
| `property_id` | UUID v4 string | ✅ | `"6087ef52-8755-44cc-9551-b80fd43958cb"` | Must be the **UUID** (`p_id` from search / `id` from fetch-all). **Do NOT pass `pg_id` (Firebase UID)** — the API returns HTTP 500 "invalid input syntax for type uuid". |

**Response Shape**

```json
{
  "data": {
    "property": {
      "pg_name": "OXO ZEPHYR RABALE",
      "address_line_1": "Plot 7, TTC Industrial Area",
      "address_line_2": "Rabale",
      "city": "Navi Mumbai",
      "eazypg_id": "4000053335H",
      "owner_name": "Rohan Mehta",
      "personal_contact": "02222334455",
      "notice_period": "30 days",
      "agreement_period": "11 months",
      "locking_period": "3 months",
      "emergency_stay_rate": 500,
      "checkin_time": "10:00 AM",
      "checkout_time": "11:00 AM",
      "gst_on_rent": "18%",
      "google_map": "https://maps.google.com/?q=...",
      "microsite_url": "https://www.oxotel.in/property/oxo-zephyr-rabale"
    },
    "propertyMicrosite": {
      "about": "Modern co-living in the heart of Rabale IT park",
      "min_token_amount": 1000,
      "property_rules": "No alcohol, No pets",
      "security_deposit": "₹5,000",
      "property_amenities": ["WiFi", "AC", "CCTV", "Gym"],
      "room_amenities": ["Attached Bath", "AC"],
      "faqs": [...],
      "reviews": [...]
    }
  }
}
```

> **⚠️ Live-tested response shape corrections (March 2026):**
> - Top-level key is `data` (not `property_data`)
> - Property fields are under `data["property"]` (214 flat fields) — no `property_name` or `location` keys
> - Microsite fields (about, amenities, rules, FAQs, reviews) are under `data["propertyMicrosite"]` (21 fields)
> - Property name field: `pg_name` (not `property_name`)
> - Address fields: `address_line_1`, `address_line_2`, `city` (not `location` or `address`)

**Notes**
- Parse as: `pd = resp_data["data"]["property"]`, `ms = resp_data["data"]["propertyMicrosite"]`
- `pd` contains ~214 flat property fields including ownership, financials, and logistics.
- `ms` contains rich text content: `about`, `property_rules`, `faqs`, `reviews`, amenity lists.
- No separate `property_rooms` array — room-level details come from the search API (`p_*` fields) or A5.

---

#### A5. Real-Time Room Availability

```
GET /bookingBot/getAvailableRoomFromEazyPGID
```

Returns live bed availability per room. Unlike A4, this uses the **room-level `eazypg_id`** and gives real-time counts.

**Query Parameters**

| Param | Type | Required | Example |
|---|---|---|---|
| `eazypg_id` | `string` | ✅ | `"4000053335H"` |

**Response Shape**

```json
{
  "rooms": [
    {
      "room_name": "Room 101",
      "sharing_type": "Double",
      "beds_available": 2,
      "amenities": "AC, Attached Bath"
    }
  ]
}
```

**Notes**
- May return `rooms` or `data` as the array key.
- If `rooms` is empty, fall back to search cache data (sharing types, amenities, rent from `p_*` fields).

> **⚠️ Live-tested (March 2026): This endpoint returns HTTP 404 "Cannot GET /bookingBot/getAvailableRoomFromEazyPGID" in at least one production RentOK instance (OxOtel).** The endpoint may not be deployed for all tenants. Treat HTTP 404 as "endpoint unavailable" (not a data error) and fall back to search cache data silently — do not surface the 404 to the user.

---

#### A6. Property Images

```
POST /bookingBot/fetchPropertyImages
```

Returns a list of image URLs for a property. Called concurrently for the top 5 search results during enrichment.

**Request Body**

| Field | Type | Required | Example |
|---|---|---|---|
| `pg_id` | `string` | ✅ | `"l5zf3ckOnRQV9OHdv5YTTXkvLHp1"` |
| `pg_number` | `int` | ✅ | `3` |

**Response Shape**

```json
{
  "images": [
    {
      "url": "https://cdn.rentok.com/property/abc123.jpg",
      "media_id": "abc123"
    }
  ]
}
```

**Notes**
- May return `images` or `data` as the array key.
- Each item may be an object `{url, media_id}` or a plain string URL.

---

### B. Bookings & Scheduling

---

#### B1. Schedule a Visit / Call / Video Tour

```
POST /bookingBot/add-booking
```

Creates a new booking (physical visit, phone call, or video tour). Used by both `save_visit_time` and `save_call_time`.

**Request Body**

| Field | Type | Required | Example | Notes |
|---|---|---|---|---|
| `user_id` | `string` | ✅ | `"uat_abc12345"` | Bot's internal user identifier (from Redis/localStorage). |
| `property_id` | `string` or `int` | ✅ | `12345` | The numeric `p_id` from search results. |
| `visit_date` | `string` | ✅ | `"15/03/2026"` | Format: `DD/MM/YYYY`. |
| `visit_time` | `string` | ✅ | `"10:00 AM"` | As stated by user. |
| `visit_type` | `string` | ✅ | `"Physical visit"` | One of: `"Physical visit"`, `"Phone Call"`, `"Video Tour"`. |
| `property_name` | `string` | ✅ | `"OXO ZEPHYR RABALE"` | Display name for confirmation messaging. |

**Response Shape (HTTP 200 — Success)**

```json
{
  "data": {
    "booking_id": 98765,
    "user_id": "uat_abc12345",
    "property_id": 12345,
    "visit_date": "15/03/2026",
    "visit_time": "10:00 AM",
    "visit_type": "Physical visit"
  }
}
```

> **Note:** No `success` field. Success is inferred from `status_code == 200`. The `data` object contains booking details.

**Response Shape (Duplicate — HTTP 200 with inner status)**

```json
{
  "status": 400,
  "message": "A visit for this property already exists on this date or the booking already exists."
}
```

> **⚠️ Live-tested (March 2026): Duplicate bookings return HTTP 200 with an inner `status: 400` field — NOT an outer HTTP 400.** The outer HTTP status is always 200. Check `data.get("status") == 400` after parsing JSON to detect duplicates.

**Error Handling**
- Duplicate = `resp.status_code == 200` AND `data.get("status") == 400`. Surface to user: "A visit is already scheduled for this property."
- Genuine error = `resp.status_code` is 4xx/5xx (not 200). Surface via `raise_for_status()`.
- Check `inner_status = data.get("status") if isinstance(data, dict) else None` before treating HTTP 200 as success.

---

#### B2. Cancel a Booking

```
POST /bookingBot/cancel-booking
```

Cancels an existing booking for a property.

**Request Body**

| Field | Type | Required | Example |
|---|---|---|---|
| `user_id` | `string` | ✅ | `"uat_abc12345"` |
| `property_id` | `string` or `int` | ✅ | `12345` |

**Response Shape (HTTP 200)**

```json
{
  "message": "Property cancelled successfully"
}
```

> **⚠️ Important:** This endpoint returns **HTTP 200 even for non-existent booking IDs**. There is no `success` field. The only reliable error signal is an HTTP status code other than 200. Success is assumed on HTTP 200.

**Error Handling**
- `raise_for_status()` is the only guard. If HTTP 200 is returned, treat as success regardless of message content.

---

#### B3. Reschedule a Booking

```
POST /bookingBot/update-booking
```

Updates one or more fields of an existing booking. At least one update field must be provided.

**Request Body**

| Field | Type | Required | Example | Notes |
|---|---|---|---|---|
| `user_id` | `string` | ✅ | `"uat_abc12345"` | |
| `property_id` | `string` or `int` | ✅ | `12345` | |
| `visit_date` | `string` | ❌ | `"20/03/2026"` | Format: `DD/MM/YYYY`. Omit if not changing. |
| `visit_time` | `string` | ❌ | `"2:00 PM"` | Omit if not changing. |
| `visit_type` | `string` | ❌ | `"Video Tour"` | Omit if not changing. |

**Response Shape (HTTP 200 — Success)**

```json
{
  "success": true,
  "message": "Booking updated successfully"
}
```

> **Note:** This endpoint **does** return `success: true` on success — checking `data.get("success")` is correct here.

**Response Shape (HTTP 200 — Failure)**

```json
{
  "success": false,
  "message": "Booking not found"
}
```

---

#### B4. Reserve a Bed

```
POST /bookingBot/reserveProperty
```

Reserves a bed/room at a property. Can also be used in **check-only mode** to query current reservation status without creating a new one.

**Request Body**

| Field | Type | Required | Example | Notes |
|---|---|---|---|---|
| `user_id` | `string` | ✅ | `"uat_abc12345"` | |
| `property_id` | `string` or `int` | ✅ | `12345` | |
| `check_only` | `bool` | ❌ | `true` | If `true`, returns reservation status without creating one. |

**Response Shape — Check Mode (`check_only: true`)**

```json
{
  "success": true,
  "reserved": true,
  "message": "Bed already reserved"
}
```
or
```json
{
  "success": false,
  "reserved": false,
  "message": "No reservation found"
}
```

**Response Shape — Reserve Mode (no `check_only`)**

```json
{
  "success": true,
  "message": "Bed reserved successfully"
}
```
or
```json
{
  "success": false,
  "message": "No beds available"
}
```

**Notes**
- In check-only mode, success can be either `data.get("success")` OR `data.get("reserved")` — check both.
- In reserve mode, `data.get("success")` is the definitive signal.
- Reservation is a prerequisite for payment in the standard booking flow.

---

#### B5. Get Scheduled Events

```
GET /bookingBot/booking/{user_id}/events
```

Returns all scheduled visits, calls, and video tours for a user.

**Path Parameters**

| Param | Type | Required | Example |
|---|---|---|---|
| `user_id` | `string` | ✅ | `uat_abc12345` |

**Response Shape**

```json
{
  "data": [
    {
      "property_name": "OXO ZEPHYR RABALE",
      "property_id": 12345,
      "visit_date": "15/03/2026",
      "visit_time": "10:00 AM",
      "visit_type": "Physical visit",
      "status": "Scheduled"
    }
  ]
}
```

**Notes**
- Use `data.get("data") or []` (None-safe) — field may be `null` if no events.
- `status` values observed: `"Scheduled"`, `"Completed"`, `"Cancelled"`.

---

#### B6. Shortlist a Property

```
POST /bookingBot/shortlist-booking-bot-property
```

Saves a property to the user's CRM shortlist. Fire-and-forget — errors are logged but do not block the user.

**Request Body**

| Field | Type | Required | Example | Notes |
|---|---|---|---|---|
| `user_id` | `string` | ✅ | `"9876543210"` | **Phone number if available**, else the bot's internal `user_id`. |
| `property_id` | `string` or `int` | ✅ | `"l5zf3ckOnRQV..."` | Uses `pg_id` (Firebase UID), not `p_id`. Field in code: `prop_id = prop.get("prop_id") or prop.get("pg_id")`. |
| `property_contact` | `string` | ❌ | `"02222334455"` | Property's own phone number from listing data. |

**Response Shape**

HTTP 200 expected. Response body not validated (fire-and-forget).

---

### C. Payments

The payment flow is a 3-step sequence:

```
Step 1: GET /tenant/get-tenant_uuid          ← does tenant exist?
         └─ Not found → POST /tenant/addLeadFromEazyPGID  ← create tenant first
Step 2: GET /tenant/{uuid}/lead-payment-link  ← generate payment link
Step 3: (user pays via Razorpay)
Step 4: POST /bookingBot/addPayment           ← record payment in backend
```

---

#### C1. Get Tenant UUID

```
GET /tenant/get-tenant_uuid
```

Looks up the CRM tenant UUID by phone number and property identifier. Must exist before a payment link can be generated.

**Query Parameters**

| Param | Type | Required | Example |
|---|---|---|---|
| `phone` | `string` | ✅ | `"9876543210"` |
| `eazypg_id` | `string` | ✅ | `"4000053335H"` |

**Response Shape**

```json
{
  "status": 200,
  "data": {
    "tenant_uuid": "a3f9c0d1-7b2e-4f8a-9c3d-0e1f2a3b4c5d"
  }
}
```

**Error Handling**
- Use `check_rentok_response()` after parsing.
- If `tenant_uuid` is empty or missing, create a lead first via `POST /tenant/addLeadFromEazyPGID`, then retry this endpoint.

---

#### C2. Generate Payment Link

```
GET /tenant/{tenant_uuid}/lead-payment-link
```

Generates a Razorpay token payment link for the given property and amount. The returned `link` value is the Razorpay short link subscription ID — prepend `https://pay.rentok.com/p/` to get the full payment URL.

**Path Parameters**

| Param | Type | Required | Example |
|---|---|---|---|
| `tenant_uuid` | `string` (UUID) | ✅ | `a3f9c0d1-7b2e-4f8a-9c3d-0e1f2a3b4c5d` |

**Query Parameters**

| Param | Type | Required | Example | Notes |
|---|---|---|---|---|
| `pg_id` | `string` | ✅ | `"l5zf3ckOnRQV9OH..."` | Firebase pg_id of the property. |
| `pg_number` | `int` | ✅ | `3` | Property sub-number. |
| `amount` | `int` | ✅ | `1000` | Token amount in ₹. Source: `property_min_token_amount` from search results; default `1000`. |

**Response Shape**

```json
{
  "status": 200,
  "data": {
    "link": "sub_XXXXXXXXXXXXXXXX",
    "pg_name": "OXO ZEPHYR RABALE"
  }
}
```

**Notes**
- Full payment URL: `https://pay.rentok.com/p/{link}` (e.g., `https://pay.rentok.com/p/sub_XXXXXXXXXXXXXXXX`)
- `pg_name` is the property display name for confirmation messaging.
- Use `check_rentok_response()` to validate. If `link` is empty, return an error to the user.

---

#### C3. Record Payment

```
POST /bookingBot/addPayment
```

Records a completed payment in the RentOK backend after the user confirms payment via the Razorpay link.

**Request Body**

| Field | Type | Required | Example | Notes |
|---|---|---|---|---|
| `user_id` | `string` | ✅ | `"uat_abc123"` | **Max 12 characters** — truncated to `user_id[:12]` in the code. |
| `pg_id` | `string` | ✅ | `"l5zf3ckOnRQV9OH..."` | |
| `pg_number` | `int` | ✅ | `3` | |
| `amount` | `string` | ✅ | `"1000"` | Stored as string in Redis; pass as-is. |
| `short_link` | `string` | ✅ | `"sub_XXXXXXXXXXXXXXXX"` | The `link` value from C2 (not the full URL). |

**Response Shape**

HTTP 200 expected. Response body is not validated — failure is caught as exception.

**Notes**
- After recording payment, the bot updates the lead status to `"Token"` via `POST /tenant/addLeadFromEazyPGID`.
- Fire-and-forget; payment link follow-up reminders are cancelled on success.

---

### D. Lead Management

---

#### D1. Create or Update Lead

```
POST /tenant/addLeadFromEazyPGID
```

Creates or updates a CRM lead in RentOK. Called automatically after a visit is scheduled, a call is booked, or a payment is made.

**Request Body**

| Field | Type | Required | Example | Notes |
|---|---|---|---|---|
| `eazypg_id` | `string` | ✅ | `"4000053335H"` | Room-level identifier from search results. |
| `phone` | `string` | ✅ | `"9876543210"` | 10-digit mobile number. |
| `name` | `string` | ✅ | `"Rahul Sharma"` | Tenant name. |
| `gender` | `string` | ✅ | `"Male"` | Values observed: `"Male"`, `"Female"`, `"Any"`. |
| `firebase_id` | `string` | ✅ (inferred) | `"cust_2026_03_15_10_30_00"` | Client-generated dedup key. Format: `cust_YYYY_MM_DD_HH_MM_SS`. |
| `lead_source` | `string` | ✅ (inferred) | `"Booking Bot"` | Always `"Booking Bot"` from this integration. |
| `lead_status` | `string` | ✅ (inferred) | `"Visit Scheduled"` | See Lead Status Values below. |
| `rent_range` | `string` or `int` | ❌ | `"10000"` | Budget from user preferences. |
| `visit_date` | `string` | ❌ | `"15/03/2026"` | Pass `""` if no visit scheduled. |
| `visit_time` | `string` | ❌ | `"10:00 AM"` | Pass `""` if no visit scheduled. |
| `visit_type` | `string` | ❌ | `"Physical visit"` | Pass `""` if no visit scheduled. |

**Lead Status Values**

| Value | When Used |
|---|---|
| `"Visit Scheduled"` | After `save_visit_time` or `save_call_time` |
| `"Token"` | After `verify_payment` |

**Response Shape (HTTP 200 — Success)**

```json
{
  "status": 200,
  "message": "Lead created!"
}
```

> **Note:** No `success` field. Success is `message == "Lead created!"` + `raise_for_status()`. The API uses Zod schema validation.

**Response Shape (HTTP 401 — Duplicate Lead)**

```json
{
  "message": "Lead already exists!"
}
```

> **⚠️ Live-tested (March 2026): When a lead already exists for the same `phone` + `eazypg_id` combination, the API returns HTTP 401 (Unauthorized) with `{"message": "Lead already exists!"}` — NOT HTTP 200 or 409.** This is an idempotency signal, not an auth error. Treat `resp.status_code == 401 and "already exists" in message.lower()` as a non-fatal success (lead is already in CRM).

**Response Shape (HTTP 400 — Validation Error)**

```json
{
  "data": {
    "error": {
      "issues": [
        {
          "path": ["name"],
          "message": "Required"
        },
        {
          "path": ["gender"],
          "message": "Required"
        }
      ]
    }
  }
}
```

**Confirmed Required Fields (from live Zod validation test)**

The following fields triggered HTTP 400 when omitted with all other fields present:
- `eazypg_id`
- `phone`
- `name`
- `gender`
- `firebase_id` *(inferred — dedup key)*
- `lead_source` *(inferred)*
- `lead_status` *(inferred)*

**Confirmed Optional Fields (accepted without errors in 25-field test)**

The following fields are accepted but not required. Unknown fields are also silently accepted:
`rent_range`, `visit_date`, `visit_time`, `visit_type`, `move_in_date`, `area`, `sharing_type`, `sharing_types_enabled`, `commute_from`, `persona`, `must_haves`, `deal_breakers`, `occupation`, `food_preference`, `food_pref`, `notes`, `city`, `budget`

**Notes**
- This is a **fire-and-forget** call in the current integration. Errors are logged as warnings but do not block the user.
- The `firebase_id` acts as an idempotency/deduplication key in the RentOK CRM.
- Sending the same `firebase_id` may update the existing lead rather than creating a duplicate.

---

### E. KYC / Identity Verification

> **Feature Flag:** KYC is currently disabled in production (`KYC_ENABLED=false` in `config.py`). These endpoints exist in the codebase but are not activated in the standard booking flow.

---

#### E1. Initialize KYC Entry

```
GET /bookingBotKyc/user-kyc/{user_id}
```

Creates or initialises a KYC tracking entry for the user. Called as a setup step before checking status. Response is not used — fire-and-forget.

**Path Parameters**

| Param | Type | Required |
|---|---|---|
| `user_id` | `string` | ✅ |

---

#### E2. Check KYC Status

```
GET /bookingBotKyc/booking/{user_id}/kyc-status
```

Returns the user's current KYC verification status.

**Path Parameters**

| Param | Type | Required |
|---|---|---|
| `user_id` | `string` | ✅ |

**Response Shape**

```json
{
  "data": {
    "kyc_status": 1
  }
}
```

**Status Values**

| `kyc_status` | Meaning |
|---|---|
| `0` or absent | KYC not completed |
| `1` | KYC verified |

---

#### E3. Generate Aadhaar OTP

```
POST /checkIn/generateAadharOTP
```

Initiates the Aadhaar OTP flow. Sends an OTP to the mobile number registered with the given Aadhaar.

**Request Body**

| Field | Type | Required | Example | Notes |
|---|---|---|---|---|
| `aadhar_number` | `string` | ✅ | `"123456789012"` | 12 digits, no spaces. Validated client-side before calling. |
| `user_phone_number` | `string` | ✅ | `"9876543210"` | Must be on file in Redis before calling. |

**Response Shape**

HTTP 200 on success. OTP is sent to the phone registered with Aadhaar (which may differ from `user_phone_number`). Response body is not validated — no meaningful data fields.

---

#### E4. Verify Aadhaar OTP

```
POST /checkIn/verifyAadharOTP
```

Verifies the OTP entered by the user. Returns the KYC data (name, gender) from the Aadhaar record on success.

**Request Body**

| Field | Type | Required | Example |
|---|---|---|---|
| `otp` | `string` | ✅ | `"123456"` |
| `user_phone_number` | `string` | ✅ | `"9876543210"` |

**Response Shape (Success)**

```json
{
  "status": 200,
  "data": {
    "name": "RAHUL SHARMA",
    "gender": "Male",
    "dob": "01/01/1995",
    "address": "..."
  }
}
```

**Response Shape (OTP Failure)**

```json
{
  "status": 400,
  "message": "Invalid OTP. Please try again."
}
```

> **⚠️ Gotcha:** OTP failure is signalled by `resp_data.get("status") == 400` — **not** by HTTP status code. The HTTP response is still 200.

**Notes**
- On success, `data.name` and `data.gender` are stored in Redis as the verified identity.
- The KYC data dict is then passed to `POST /bookingBotKyc/update-kyc` to persist the record.

---

#### E5. Update KYC Record

```
POST /bookingBotKyc/update-kyc
```

Persists the verified KYC data returned by E4 into the RentOK booking backend.

**Request Body**

| Field | Type | Required | Example |
|---|---|---|---|
| `user_id` | `string` | ✅ | `"uat_abc12345"` |
| `kyc_data` | `object` | ✅ | `{"name": "RAHUL SHARMA", "gender": "Male", ...}` |

The `kyc_data` object is the `data` dict returned by `POST /checkIn/verifyAadharOTP` — passed through as-is.

**Response Shape**

HTTP 200 expected. Response is not validated. Failure triggers a warning log but does not block the user.

---

## 5. Lead Enrichment Opportunity

The current `addLeadFromEazyPGID` call sends 11 fields. The following additional data points are **available in Redis** and can be sent to enrich the CRM lead without any user-facing changes.

| Field | Redis Source | Current Status | Value Added |
|---|---|---|---|
| `move_in_date` | `{uid}:user_memory` → `move_in_date` | ❌ Not sent | Helps sales team prioritise high-intent leads |
| `area` | `{uid}:preferences` → `location` | ❌ Not sent | Enables geographic lead routing |
| `sharing_types_enabled` | `{uid}:preferences` → `sharing_types` | ❌ Not sent | Matches lead to correct room type |
| `commute_from` | `{uid}:preferences` → `commute_from` | ❌ Not sent | Useful context for property recommendation calls |
| `persona` | `{uid}:user_memory` → `persona` | ❌ Not sent | Student / Working Professional segmentation |
| `must_haves` | `{uid}:preferences` → `must_haves` list | ❌ Not sent | Helps sales team prepare relevant pitch |
| `deal_breakers` | `{uid}:preferences` → `deal_breakers` list | ❌ Not sent | Prevents wasted calls on wrong properties |
| `budget` | `{uid}:preferences` → `max_budget` | ❌ Not sent (sent as `rent_range` string) | Confirm budget is passed numerically |

**Also note:** `payment.py`'s `verify_payment` call uses the old name chain (`get_aadhar_user_name(user_id) or phone or "Guest"`) and **does not fall back to `user_memory.profile_name`**. This was fixed in `schedule_visit.py` and `schedule_call.py` (commit `5dbcd33`) but not in the payment path.

---

## 6. Known Behaviours & Limitations

### Idempotency
- `cancel-booking` has no idempotency issues — calling it multiple times always returns HTTP 200.
- `add-booking` signals duplicate as HTTP 200 + inner `status: 400` — **not** an outer HTTP 400. Safe to detect and surface without retrying.
- `addLeadFromEazyPGID` returns HTTP 401 `"Lead already exists!"` when the same `phone` + `eazypg_id` is already in the CRM. Treat this as a non-fatal idempotency signal. The `firebase_id` field also acts as a deduplication key within a session.

### Inner Error 500
`POST /property/getPropertyDetailsAroundLatLong` can return HTTP 200 with `data.data.status == 500` and an error message in `data.data.data.error`. **Always check the inner status** before processing results.

### Empty Results ≠ Error
`GET /bookingBot/getAvailableRoomFromEazyPGID` returns HTTP 200 with an empty `rooms` array when no beds are configured. This is not an error — fall back to cached listing data.

### Room Availability Endpoint May Be Unavailable
`GET /bookingBot/getAvailableRoomFromEazyPGID` returned HTTP 404 "Cannot GET ..." for all tested `eazypg_id` values against the OxOtel RentOK instance. The endpoint may not be deployed for all RentOK tenants. Handle HTTP 404 silently (fall back to search cache, do not surface to user).

### Cancel Returns 200 for Non-Existent Bookings
`POST /bookingBot/cancel-booking` returns HTTP 200 and `"Property cancelled successfully"` even for a `property_id` that has no scheduled booking. There is no way to distinguish "successfully cancelled" from "nothing to cancel" via the API response alone.

### Zod Validation Errors on HTTP 400
When required fields are missing, the API returns a Zod validation error shape with `data.error.issues[]`. Each issue has `path` (array of field names) and `message`. Parse this for precise debugging.

### No Pagination
`POST /property/getPropertyDetailsAroundLatLong` and `POST /bookingBot/fetch-all-properties` return all results in a single response. There is no pagination cursor or limit parameter.

### Search Filter Quirks (Live-Tested March 2026)

Three `POST /property/getPropertyDetailsAroundLatLong` filters have been confirmed to produce **0 results** under common conditions. Avoid them unless the use case demands explicit filtering, or test carefully per RentOK tenant.

#### `pg_available_for` — Gender Filter Returns 0 for "Any" Properties
Passing `"pg_available_for": "All Girls"` or `"pg_available_for": "All Boys"` returns **zero results** for properties whose availability is set to `"Any"` in RentOK (co-living / mixed). The filter only matches properties explicitly tagged with the exact gender label. Properties tagged `"Any"` are excluded, even if they accept that gender.

**Implication:** A search with `pg_available_for: "All Girls"` will silently omit properties open to all genders. For multi-property brands that include co-living stock, this filter can return 0 results even when availability exists.

**Bot behaviour:** Omit this filter from the API call when the user has not explicitly stated a gender preference. Let the bot handle gender preference as a text-layer suggestion rather than an API filter.

#### `unit_types_available` — Room Type Filter Returns 0 Results
Passing `"unit_types_available": "double sharing"` (or similar) returns **0 results** for all tested OxOtel properties. The API appears to expect a specific format or value set that differs from common strings. The exact accepted values are not documented by RentOK.

**Implication:** Do not pass `unit_types_available` unless the correct server-side value strings are confirmed for the tenant. Use the bot's own scoring/filtering post-API call to apply room-type preferences.

#### Properties with `null` Coordinates Still Appear in Results
Some properties have `p_lat: null, p_long: null` in the RentOK database but **still appear in search results** when the search radius is large enough. A 100,000m radius from a central Mumbai point (e.g., 19.1136, 72.8697) returns all OxOtel properties including coordinate-less ones.

**Implication:** A null lat/lng does not mean a property is excluded from search — it depends on the radius. Bot tests should use a large radius (≥50,000m) when trying to retrieve all brand properties regardless of their geocoding status. Conversely, location-specific searches with smaller radii will naturally exclude these properties.

### `pg_ids` Is Mandatory — Not Optional
The `pg_ids` array (whitelabel property group IDs) **must be non-empty**. Passing an empty array or omitting the field returns zero results from `getPropertyDetailsAroundLatLong` and `fetch-all-properties`.

These IDs are brand-specific Firebase UIDs. They are **not** the same as the Rentok UUID (`p_id`) or the room-level `eazypg_id`. The bot receives them via `account_values.pg_ids` from the frontend (web widget) or WhatsApp webhook payload. For web users, the frontend must pass `pg_ids` explicitly in the chat request body — passing only a brand token UUID without `pg_ids` results in empty search results.

**Critical:** If `account_values.pg_ids` is missing or empty when the bot tries to search, `get_whitelabel_pg_ids()` returns `[]` → search tool logs a warning and returns `[]` → bot apologises with "no properties found." This can silently masquerade as a location issue.

### Rate Limiting
No rate limiting has been observed from the RentOK API. The client-side rate limits (6/min per user, 30/hr per user) are enforced by the bot's own `core/rate_limiter.py`.

### Timeout Values
| Endpoint Category | Timeout Used |
|---|---|
| Most booking/booking-bot endpoints | 15 seconds |
| Property search (`getPropertyDetailsAroundLatLong`) | 30 seconds |
| Room availability (`getAvailableRoomFromEazyPGID`) | 10 seconds |
| Property details (`property-details-bots`) | 30 seconds |
| Retry-wrapped calls (`http_post`) | 2 retries + exponential backoff |

---

## 7. Production Bug Report

> Discovered during live integration testing (March 2026). All three bugs cause **silent failures** — the tool code runs without exceptions but returns stale/empty data to Claude.

---

### Bug B1 — `fetch_property_details`: Wrong ID Type → HTTP 500 → Always Cache Fallback

**File:** `tools/broker/property_details.py`, line ~48
**Severity:** High — users never see live property details, only cached search-result data

**Root Cause:**
```python
# BUG: prop.get("prop_id") returns pg_id (Firebase UID), NOT the UUID
prop_id = prop.get("prop_id") or prop.get("pg_id")
# This sends Firebase UID (e.g. "l5zf3ckOnRQV9OH...") as property_id
resp = await client.post(".../property-details-bots", json={"property_id": prop_id})
```

**What happens:** API returns HTTP 500 "invalid input syntax for type uuid". The tool catches the exception silently and falls back to the Redis search cache. Claude receives cached data (which has different field names) and the user never gets live property details.

**Fix:** Use the UUID (`p_id` field from search results), not the Firebase UID.
```python
# CORRECT: p_id from search results is the UUID (e.g. "6087ef52-8755-44cc-9551-b80fd43958cb")
prop_id = prop.get("p_id")
```

**Also fix response parsing** (line ~64):
```python
# BUG: checks for non-existent keys
pd = data.get("property_data", data.get("data", {}))
name = pd.get("property_name")  # field is actually pg_name under data["property"]

# CORRECT:
outer = data.get("data", {})
pd = outer.get("property", {})
ms = outer.get("propertyMicrosite", {})
name = pd.get("pg_name")
```

---

### Bug B2 — `fetch_properties_by_query`: Wrong Field Name → Zero Matches for All Queries

**File:** `tools/broker/query_properties.py`, lines 47–49
**Severity:** High — the text-search tool never matches any property

**Root Cause:**
```python
for p in properties:
    # BUG: fetches 'property_name' and 'name' — neither exists in fetch-all response
    name = p.get("property_name", p.get("name", "")).strip().lower()
    if query_lower in name or name in query_lower:
        matches.append(p)
```

**What happens:** `fetch-all-properties` returns `pg_name` as the property name field. The code checks `property_name` and `name` (both absent). Every property returns `name=""`. No query ever matches. The tool always returns `"No properties matching '{query}' found."`.

**Fix:**
```python
name = p.get("pg_name", p.get("property_name", p.get("name", ""))).strip().lower()
```

**Also fix the results display** (lines 57–60):
```python
# BUG: uses non-existent field names for display
f"- {p.get('property_name', p.get('name', ''))} | "
f"{p.get('location', p.get('address', ''))} | "
f"Rent: {p.get('rent', p.get('rent_starts_from', ''))}"

# CORRECT: use actual field names from fetch-all response
f"- {p.get('pg_name', '')} | "
f"{(p.get('microsite_data') or {}).get('about', '')[:60]} | "
f"Link: {p.get('microsite_link', 'N/A')}"
```

---

### Bug B3 — `fetch_property_details` Response Parsing: Wrong Key Path → Always Fallback

**File:** `tools/broker/property_details.py`, line ~64–75
**Severity:** High — even if the correct UUID is passed (Bug B1 fixed), the response is still misparse

**Root Cause:**
```python
# BUG: checks for non-existent key "property_data"
pd = data.get("property_data", data.get("data", {}))
# Even with data.get("data"), pd = {"property": {...}, "propertyMicrosite": {...}}
# Then accesses pd.get("property_name") — doesn't exist at this level
if not pd.get("property_name"):
    # always falls through to fallback because property_name is nested at pd["property"]["pg_name"]
    return _build_from_cache(...)
```

**What happens:** Even with the correct UUID (B1 fixed), the property data is found but the code can't extract `property_name` because it looks at the wrong nesting level. The tool always hits the cache fallback path.

**Fix:**
```python
outer = data.get("data", {})
pd = outer.get("property", {})     # flat 214-field property dict
ms = outer.get("propertyMicrosite", {})  # 21-field microsite dict

if not pd.get("pg_name"):          # check the correct field name at the correct level
    return _build_from_cache(...)
```

---

### Combined Fix Impact

Fixing all three bugs enables:
- **Live property details** instead of stale search-cache data
- **Text-query property search** that actually finds properties by name
- **Rich property context** for Claude: about text, property rules, FAQs, security deposit, amenity lists — all from `propertyMicrosite`

---

*Document generated: March 2026. All response shapes confirmed from live API testing against `https://apiv2.rentok.com` using OxOtel pg_ids. Section 7 bugs confirmed by test_full_integration.py probe scripts.*
