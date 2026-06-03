import asyncio
import time
from typing import Any, Callable

from config import settings
from core.log import get_logger
from core.signals import record_signal
from core.tool_boundary import IDEMPOTENT_TOOLS, idempotency_key, validate_tool_input
from utils.properties import find_property

logger = get_logger("core.tool_executor")

# Tools that can fall back to cached property data on failure
_PROPERTY_FALLBACK_TOOLS = {
    "fetch_property_details",
    "fetch_room_details",
    "fetch_property_images",
    "fetch_landmarks",
    "fetch_nearby_places",
    "compare_properties",
}


def _build_fallback(tool_name: str, tool_input: dict, user_id: str, error: str) -> str:
    """Try to return useful cached data instead of a raw error message."""
    if tool_name not in _PROPERTY_FALLBACK_TOOLS:
        return f"Error executing {tool_name}: {error}"

    property_name = tool_input.get("property_name", tool_input.get("property_names", ""))
    if not property_name:
        return f"Error executing {tool_name}: {error}"

    try:
        prop = find_property(user_id, property_name)
        if not prop:
            return f"Error executing {tool_name}: {error}"

        # Build a helpful fallback from cached search data
        name = prop.get("property_name", property_name)
        parts = [f"[Tool error — showing cached data for '{name}']"]
        if prop.get("property_location"):
            parts.append(f"Location: {prop['property_location']}")
        if prop.get("property_rent"):
            parts.append(f"Rent starts from: ₹{prop['property_rent']}")
        if prop.get("pg_available_for"):
            parts.append(f"For: {prop['pg_available_for']}")
        if prop.get("property_type"):
            parts.append(f"Type: {prop['property_type']}")
        if prop.get("google_map"):
            parts.append(f"Map: {prop['google_map']}")
        if prop.get("property_link"):
            parts.append(f"Link: {prop['property_link']}")
        parts.append("Suggest: schedule a call to get more details directly from the property.")
        return "\n".join(parts)
    except Exception as e:
        logger.warning("Fallback lookup failed for %s: %s", tool_name, e)
        return f"Error executing {tool_name}: {error}"


class ToolExecutor:
    def __init__(self):
        self._handlers: dict[str, Callable] = {}
        self._fallback_handlers: dict[str, Callable] | None = None

    def register(self, name: str, handler: Callable) -> None:
        self._handlers[name] = handler

    def register_many(self, handlers: dict[str, Callable]) -> None:
        self._handlers.update(handlers)

    def set_fallback(self, handlers: dict[str, Callable]) -> None:
        """Set fallback handlers for graceful tool expansion on skill misses.

        When a tool is not found in the primary handler set, the executor
        checks the fallback set before returning an error. This ensures
        Claude can still call any broker tool even if skill detection was wrong.
        """
        self._fallback_handlers = handlers

    async def execute(self, tool_name: str, tool_input: dict, user_id: str) -> str:
        handler = self._handlers.get(tool_name)
        # Graceful expansion: if tool not in filtered set, try fallback
        if handler is None and self._fallback_handlers:
            handler = self._fallback_handlers.get(tool_name)
            if handler:
                logger.warning(
                    "Skill miss: tool '%s' not in filtered set — expanding from fallback",
                    tool_name,
                )
                # Track the miss for monitoring (brand-scoped)
                try:
                    from db.redis_store import track_skill_miss, get_user_brand
                    track_skill_miss(tool_name, brand_hash=get_user_brand(user_id))
                except Exception:
                    pass  # Non-blocking — don't break tool execution
                # Register for subsequent calls in this turn
                self._handlers[tool_name] = handler
        if handler is None:
            return f"Error: Unknown tool '{tool_name}'"

        # Tool-boundary input validation (defense-in-depth) — never let the
        # validation infra itself break an otherwise-valid call.
        try:
            from tools.registry import get_input_schema
            schema_err = validate_tool_input(get_input_schema(tool_name), tool_input)
        except Exception:
            schema_err = None
        if schema_err:
            logger.warning("Tool input rejected for %s: %s", tool_name, schema_err)
            return f"Invalid arguments for {tool_name}: {schema_err}. Please correct the arguments and try again."

        # Idempotency burst-dedup for write-path tools (creates bookings/payments).
        idem_key = None
        if tool_name in IDEMPOTENT_TOOLS:
            try:
                from db.redis_store import idem_begin
                idem_key = idempotency_key(user_id, tool_name, tool_input)
                cached, acquired = idem_begin(idem_key, settings.IDEMPOTENCY_WINDOW_SECONDS)
                if cached is not None:
                    logger.info("Idempotent replay for %s (user %s)", tool_name, user_id)
                    return cached
                if not acquired:
                    return ("I'm still processing your previous request — give me a moment "
                            "and I'll confirm shortly.")
            except Exception:
                idem_key = None  # Redis hiccup → skip dedup, never block the tool

        t0 = time.monotonic()
        try:
            result = handler(user_id=user_id, **tool_input)
            if hasattr(result, "__await__"):
                result = await asyncio.wait_for(result, timeout=settings.TOOL_TIMEOUT_SECONDS)
            latency_ms = int((time.monotonic() - t0) * 1000)
            self._track(tool_name, True, latency_ms, user_id)
            result = str(result)
            if idem_key:
                try:
                    from db.redis_store import idem_complete
                    idem_complete(idem_key, result, settings.IDEMPOTENCY_WINDOW_SECONDS)
                except Exception:
                    pass
            return result
        except Exception as e:
            latency_ms = int((time.monotonic() - t0) * 1000)
            if idem_key:
                try:
                    from db.redis_store import idem_release
                    idem_release(idem_key)  # failed → allow a genuine retry
                except Exception:
                    pass
            self._track(tool_name, False, latency_ms, user_id)
            err = str(e)
            if not err:
                err = (f"timed out after {settings.TOOL_TIMEOUT_SECONDS}s"
                       if isinstance(e, asyncio.TimeoutError) else type(e).__name__)
            logger.error("Error executing %s: %s", tool_name, err, exc_info=True)
            # Truth signal: an upstream tool call failed this turn. Egress reads
            # this to render an error rail (≠ empty), never a false "no results".
            record_signal(api_error=True, error_message=err)
            # Fire-and-forget: log structured error event to PostgreSQL
            try:
                from db.postgres import insert_error_event
                from db.redis_store import get_user_brand
                asyncio.create_task(insert_error_event(
                    user_id=user_id,
                    brand_hash=get_user_brand(user_id),
                    error_type="tool_failure",
                    error_source=tool_name,
                    error_message=err[:500],
                    context={"tool_input": {k: str(v)[:200] for k, v in tool_input.items()}, "latency_ms": latency_ms},
                ))
            except Exception:
                pass
            return _build_fallback(tool_name, tool_input, user_id, err)

    @staticmethod
    def _track(tool_name: str, success: bool, latency_ms: int, user_id: str) -> None:
        """Fire-and-forget tool reliability tracking."""
        try:
            from db.redis_store import track_tool_result, get_user_brand
            track_tool_result(tool_name, success, latency_ms, brand_hash=get_user_brand(user_id))
        except Exception:
            pass  # Non-blocking — never break tool execution
