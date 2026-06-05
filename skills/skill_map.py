"""
Skill-to-tool mapping and keyword-based skill detection.

Each skill maps to specific tools. When skills are loaded dynamically,
only the tools needed for those skills are sent to the Anthropic API.

Also provides a keyword heuristic fallback for skill detection if the
supervisor doesn't return skills.
"""

from __future__ import annotations

from core.log import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Skill → Tool mapping
# ---------------------------------------------------------------------------

SKILL_TOOLS: dict[str, list[str]] = {
    "qualify_new":       ["save_preferences"],
    "qualify_returning": ["save_preferences"],
    "search":            ["save_preferences", "search_properties", "fetch_properties_by_query"],
    "details":           ["fetch_property_details", "fetch_room_details", "fetch_property_images"],
    "compare":           ["compare_properties", "fetch_landmarks", "fetch_nearby_places"],
    "commute":           ["estimate_commute", "fetch_landmarks"],
    "shortlist":         ["shortlist_property"],
    "show_more":         ["search_properties", "fetch_properties_by_query", "show_more_properties"],
    "selling":           ["fetch_nearby_places", "estimate_commute", "fetch_room_details", "web_search"],
    "web_search":        ["web_search", "fetch_nearby_places"],
    "learning":          ["save_preferences"],  # For deal_breakers update
}

# Always included regardless of skill detection (safety net).
# save_preferences + search are the two most common tools; save_name is always
# present so Tarini can capture a name the moment it's volunteered, on any turn.
ALWAYS_TOOLS: list[str] = ["save_preferences", "search_properties", "save_name"]

# Skills that are always loaded alongside any other skill.
# NOTE: selling.md is 6.9k chars — too large to always load.
# Instead, selling is loaded selectively by the supervisor when relevant
# (comparisons, details, objection handling). This keeps typical turns small.
ALWAYS_SKILLS: list[str] = []


def get_tools_for_skills(skills: list[str]) -> list[str]:
    """Return deduplicated tool names for a set of skills + ALWAYS_TOOLS."""
    tools: set[str] = set(ALWAYS_TOOLS)
    for skill in skills:
        tools.update(SKILL_TOOLS.get(skill, []))
    return sorted(tools)  # sorted for deterministic ordering


# ---------------------------------------------------------------------------
# Keyword-based fallback skill detection
# ---------------------------------------------------------------------------

SKILL_KEYWORDS: dict[str, list[str]] = {
    "compare": [
        "compare", "vs", "versus", "which is better", "difference between",
        "side by side", "between these",
    ],
    "commute": [
        "how far", "commute", "distance from", "travel time", "metro",
        "office", "how long", "minutes from", "transit",
    ],
    "details": [
        "details", "tell me more", "about this", "rooms", "images",
        "photos", "room details", "more info", "tell me about",
    ],
    "shortlist": [
        "shortlist", "save", "bookmark", "favorite", "add to list",
    ],
    "show_more": [
        "show more", "show me more", "more options", "next batch",
        "other properties", "anything else", "other options",
        "more results", "keep going",
    ],
    "web_search": [
        "search the web", "search online", "what about the area",
        "tell me about the area", "is it safe", "neighborhood",
        # Area-recommendation queries — must use web_search, not model memory
        "which area", "what area", "areas in", "area for", "best area",
        "good area", "areas near", "best place to stay", "places to stay",
        "good locality", "best locality", "localities in", "which locality",
        "good location for", "best location for", "which location",
        "safe area", "safe locality", "well connected area",
        "for working professionals", "for students", "for families",
        "posh area", "affordable area", "upcoming area",
    ],
    "selling": [
        "doesn't have", "no gym", "no ac", "no wifi", "no parking", "no laundry",
        "doesn't include", "missing", "lacks", "too expensive", "not in budget",
        "gym nearby", "gym near", "any gym", "nearby gym", "nearby amenity",
        "find a gym", "where to workout", "working out",
    ],
}


def detect_skills_heuristic(message: str) -> list[str]:
    """Fallback skill detection from keywords in the user's message.

    Returns a list of detected skills, or ["search"] as default.
    """
    msg_lower = message.lower()
    detected: list[str] = []

    for skill, keywords in SKILL_KEYWORDS.items():
        if any(kw in msg_lower for kw in keywords):
            detected.append(skill)

    if not detected:
        # Default to search — the most common user intent
        detected = ["search"]

    return detected
