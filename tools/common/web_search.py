"""
Web Intelligence Layer: general-purpose web search available to all agents.

The LLM decides when to invoke it, like a real broker Googling market data.
Uses Tavily API for search, with Redis caching by category:
  - area_intel:*  → TTL 7 days  (neighborhoods change slowly)
  - brand_intel:* → TTL 24 hours (reviews/reputation change faster)
  - general:*     → TTL 3 days

Safety: domain allowlist per category, per-conversation rate limit,
competitor name filtering.
"""

import hashlib
import json as _json

import httpx

from config import settings
from core.log import get_logger
from core.untrusted import fence
from db.redis_store import _r as _redis

logger = get_logger("tools.web_search")

# ---------------------------------------------------------------------------
# Category config: TTLs, domain restrictions
# ---------------------------------------------------------------------------

_CATEGORY_CONFIG = {
    "area": {
        "ttl": 7 * 86400,  # 7 days
        "domains": [
            "housing.com", "nobroker.in", "magicbricks.com",
            "99acres.com", "squareyards.com", "commonfloor.com",
            "wikipedia.org", "google.com",
        ],
        "prefix": "web_intel:area",
    },
    "brand": {
        "ttl": 86400,  # 24 hours
        "domains": [
            "google.com", "justdial.com", "trustpilot.com",
            "mouthshut.com", "glassdoor.com",
        ],
        "prefix": "web_intel:brand",
    },
    "general": {
        "ttl": 3 * 86400,  # 3 days
        "domains": [],  # no restriction
        "prefix": "web_intel:general",
    },
}

# Rate limiting key per conversation (user)
_RATE_KEY_PREFIX = "web_search_count"
_RATE_TTL = 86400  # 24h (resets with conversation)

# Competitor names to strip from results
_COMPETITOR_NAMES = {
    "nobroker", "nestaway", "zolo", "stanza living", "oxotel",
    "colive", "your-space", "isthara", "settl", "housr",
}


TOOL_SCHEMA = {
    "name": "web_search",
    "description": "Search the web for real-time market data, area intelligence, brand info, or general knowledge. Use for: rent ranges, neighborhood safety, connectivity, brand reviews, or any factual question tools can't answer. Cached results are returned instantly. Max 3 searches per conversation.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query. Be specific: 'average rent for PG in Andheri West Mumbai 2024' is better than 'rent Andheri'",
            },
            "category": {
                "type": "string",
                "description": "One of: 'area' (neighborhood data, rent ranges, connectivity), 'brand' (reviews, reputation), 'general' (anything else)",
                "enum": ["area", "brand", "general"],
            },
            "context": {
                "type": "string",
                "description": "Brief context for why you need this search (helps with result relevance)",
            },
        },
        "required": ["query", "category"],
    },
}


def _cache_key(category: str, query: str) -> str:
    cfg = _CATEGORY_CONFIG.get(category, _CATEGORY_CONFIG["general"])
    h = hashlib.md5(query.lower().strip().encode()).hexdigest()
    return f"{cfg['prefix']}:{h}"


def _get_cache(category: str, query: str) -> str | None:
    key = _cache_key(category, query)
    raw = _redis().get(key)
    if raw:
        logger.info("web_search cache HIT: %s", key[-16:])
    return raw.decode() if raw else None


def _set_cache(category: str, query: str, result: str) -> None:
    cfg = _CATEGORY_CONFIG.get(category, _CATEGORY_CONFIG["general"])
    key = _cache_key(category, query)
    try:
        _redis().setex(key, cfg["ttl"], result)
        logger.debug("web_search cache SET: %s (TTL=%ds)", key[-16:], cfg["ttl"])
    except Exception as e:
        logger.warning("web_search cache SET failed: %s", e)


def _check_rate_limit(user_id: str) -> bool:
    """Return True if under rate limit, False if exceeded."""
    key = f"{_RATE_KEY_PREFIX}:{user_id}"
    r = _redis()
    count = r.get(key)
    if count and int(count) >= settings.WEB_SEARCH_MAX_PER_CONVERSATION:
        return False
    return True


def _increment_rate(user_id: str) -> None:
    key = f"{_RATE_KEY_PREFIX}:{user_id}"
    r = _redis()
    pipe = r.pipeline()
    pipe.incr(key)
    pipe.expire(key, _RATE_TTL)
    pipe.execute()


def _filter_competitors(text: str) -> str:
    """Remove competitor brand names from search results."""
    result = text
    for name in _COMPETITOR_NAMES:
        # Case-insensitive replacement
        lower = result.lower()
        idx = lower.find(name)
        while idx != -1:
            result = result[:idx] + "a competitor platform" + result[idx + len(name):]
            lower = result.lower()
            idx = lower.find(name, idx + len("a competitor platform"))
    return result


async def _tavily_search(query: str, domains: list[str], max_results: int = 5) -> str:
    """Call Tavily search API. Returns combined snippet text."""
    api_key = settings.TAVILY_API_KEY
    if not api_key:
        return ""

    payload = {
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
    }
    if domains:
        payload["include_domains"] = domains

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post("https://api.tavily.com/search", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("Tavily search failed: %s", e)
        return ""

    results = data.get("results", [])
    if not results:
        return ""

    snippets = []
    for r in results[:max_results]:
        title = r.get("title", "")
        content = r.get("content", "")
        snippets.append(f"- {title}: {content}")

    return "\n".join(snippets)


async def web_search(
    user_id: str,
    query: str,
    category: str = "general",
    context: str = "",
    **kwargs,
) -> str:
    """Search the web for market data, area info, brand info, or general knowledge.

    Args:
        query: Search query (e.g. "average rent in Andheri West Mumbai")
        category: One of "area", "brand", "general"
        context: Optional context about why this search is needed
    """
    if not settings.TAVILY_API_KEY:
        return "Web search is not configured. Please provide information based on your general knowledge."

    # Normalize category
    category = category.lower().strip()
    if category not in _CATEGORY_CONFIG:
        category = "general"

    # Rate limit check
    if not _check_rate_limit(user_id):
        return (
            "Web search limit reached for this conversation "
            f"(max {settings.WEB_SEARCH_MAX_PER_CONVERSATION} per session). "
            "Please answer based on your general knowledge or data already available."
        )

    # Check cache first
    cached = _get_cache(category, query)
    if cached:
        return fence(_filter_competitors(cached), "live web-search results")

    # Execute search
    cfg = _CATEGORY_CONFIG[category]
    raw_result = await _tavily_search(query, cfg["domains"])

    if not raw_result:
        return "No relevant web results found. Please answer based on your general knowledge."

    # Filter competitors and cache
    filtered = _filter_competitors(raw_result)
    _set_cache(category, query, filtered)
    _increment_rate(user_id)

    prefix = {
        "area": "Based on current market data",
        "brand": "Based on available information",
        "general": "Based on web search results",
    }.get(category, "Based on web search results")

    return f"{prefix}:\n" + fence(filtered, "live web-search results")
