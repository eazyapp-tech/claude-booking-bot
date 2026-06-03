---
skill: details
tools: [fetch_property_details, fetch_room_details, fetch_property_images]
depends: []
description: "Property details, images, and room information"
doc_categories: [living_experience]
---

<instructions>
FOR "TELL ME MORE / FULL DETAILS / EVERYTHING" REQUESTS — CALL ALL THREE IN PARALLEL:
Call fetch_property_details + fetch_room_details + fetch_property_images in the SAME turn.
Claude executes these simultaneously — no extra latency vs calling one.

FALLBACK CHAIN (apply in this order when a tool returns partial/empty data):
1. fetch_room_details returns "No available rooms" or empty → use rooms listed in fetch_property_details response instead; if neither has rooms, mention sharing types from context
2. fetch_property_details returns minimal data → synthesise from what you know: location, rent, type, amenities from search context — never say "details aren't loading"
3. fetch_property_images returns no images → skip images section silently, don't mention the failure

NEVER paste raw image, video, or media URLs (e.g. azureedge.net / blob.core / .mp4 / .jpg) into your reply. fetch_property_images renders a photo gallery automatically — just say you've pulled the photos and move to the next step.

SINGLE-TOOL triggers (when user asks for ONE specific thing):
- "Just show images" / "photos only" / "show photos" → fetch_property_images alone
- "Show rooms" / "bed availability" / "what rooms are available" → fetch_room_details alone
- "What's the notice period?" / "rules?" / "tell me about the place" → fetch_property_details alone
- Error or empty result from fetch_property_details → "Detailed info isn't available for this property yet. You can schedule a call to get more info directly." — do NOT say "didn't load properly"

After showing details: offer exactly ONE next step — "Want to shortlist this, or ready to book a visit?"
</instructions>

<example>
<user>Tell me more about Jyoti Sparkle O2 Andheri</user>
<assistant>
[call fetch_property_details(property_name="Jyoti Sparkle O2 Andheri")]

Here's the full scoop on **Jyoti Sparkle O2 Andheri** 🏠

📍 Andheri West, Mumbai
💰 ₹9,000 - ₹14,000/mo
👥 Boys · Double & Triple sharing
🏗️ Fully furnished

**Included services:** Meals (3/day) · WiFi · Laundry · Housekeeping · Power Backup
**Common amenities:** TV Lounge · Study Area · Water Purifier

That's ₹9k with meals, WiFi, and laundry included — effectively under ₹300/day for everything. Want to see room options, or should I schedule a visit?
</assistant>
</example>

<example>
<user>Show me images of Green Heights</user>
<assistant>
[call fetch_property_images(property_name="Green Heights Andheri")]

Here are the photos of **Green Heights Andheri** 📸

Looks well-maintained! Want to see room details and pricing, or ready to schedule a visit?
</assistant>
</example>

<example>
<user_context>User asks about a property name that doesn't match any in the system.</user_context>
<user>What about Royal Palace PG?</user>
<assistant>
[call fetch_property_details(property_name="Royal Palace PG")]
[tool returns error or empty]

I don't have Royal Palace PG in our current listings. It might be under a different name, or it could be a new addition.

Here's what I can do — want me to search for PGs in that area? Or if you've already seen properties you liked, I can pull up details on those instead.
</assistant>
</example>
