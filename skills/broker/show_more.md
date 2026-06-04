---
skill: show_more
tools: [search_properties, fetch_properties_by_query, show_more_properties]
depends: []
description: "Show next batch of results or expand search radius"
---

<instructions>
SHOW MORE HANDLING:
- Next batch from the SAME search (unshown results remain) → call show_more_properties. It renders the next few as CARDS automatically. Write a SHORT one-line intro only — do NOT type out property blocks yourself; the cards do that.
- If show_more_properties tells you everything has already been shown (or the user wants a wider area), call search_properties(radius_flag=true) to expand the radius. That also renders cards automatically — again, a SHORT intro, no prose list.
- Never repeat properties already shown.
</instructions>

<example>
<user>Show me more options</user>
<assistant>
[call show_more_properties(user_id=...)]

Here are a few more in the same area — slightly farther but great value. Want details on any, or should I compare your favourites?
</assistant>
</example>

<example>
<user_context>All results from the initial search already shown (only 3 matched). Needs a wider radius.</user_context>
<user>Any other options?</user>
<assistant>
[call search_properties(user_id=..., radius_flag=true)]

Expanded the search area — a few more options here. The closest one is the best value of the lot. Want to compare it with your earlier favourites?
</assistant>
</example>
