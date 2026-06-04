import asyncio
import json as _json
import time
from typing import AsyncGenerator, Optional

import anthropic

from config import settings
from core.log import get_logger
from core.tool_executor import ToolExecutor

logger = get_logger("core.claude")

MAX_TOOL_ROUNDS = 15
MAX_TOKENS_RESPONSE = 4096
MAX_TOKENS_CLASSIFY = 256

# Anthropic ephemeral prompt-cache pricing multipliers (relative to base input rate).
_CACHE_WRITE_MULT = 1.25  # tokens written to cache
_CACHE_READ_MULT = 0.10   # tokens served from cache


def _usage_cost(usage, rates: dict) -> tuple[int, int, float]:
    """Cache-aware token + cost accounting for one Anthropic response.

    `usage.input_tokens` reports ONLY the uncached delta — the cached system
    prompt and tools (the bulk of input on this app) surface as
    cache_read/cache_creation tokens and were previously dropped, under-counting
    both tokens and spend. Returns (total_input_tokens, output_tokens, cost_usd)
    with cache reads billed at 0.1x and cache writes at 1.25x the input rate.
    """
    base_in = usage.input_tokens or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    out = usage.output_tokens or 0
    cost = (
        base_in * rates["in"]
        + cache_write * rates["in"] * _CACHE_WRITE_MULT
        + cache_read * rates["in"] * _CACHE_READ_MULT
        + out * rates["out"]
    ) / 1_000_000
    return base_in + cache_write + cache_read, out, cost


class AnthropicEngine:
    def __init__(self, tool_executor: ToolExecutor):
        self.client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        self.tool_executor = tool_executor

    async def run_agent(
        self,
        system_prompt: str | list[str],
        tools: list[dict],
        messages: list[dict],
        model: str,
        user_id: str,
        max_iterations: int = None,
        agent_name: str = "unknown",
    ) -> str:
        if max_iterations is None:
            max_iterations = settings.MAX_AGENT_ITERATIONS

        system = self._build_system_blocks(system_prompt)

        cached_tools = []
        for i, tool in enumerate(tools):
            t = dict(tool)
            if i == len(tools) - 1:
                t["cache_control"] = {"type": "ephemeral"}
            cached_tools.append(t)

        # Fix P11: merge consecutive same-role messages before sending to Anthropic
        messages = self._sanitize_messages(messages)

        for iteration in range(max_iterations):
            response = await self._call_api(model, system, cached_tools, messages)
            if response is None:
                return "I'm experiencing a temporary issue. Please try again."

            logger.debug("iteration %d/%d | stop_reason=%s", iteration + 1, max_iterations, response.stop_reason)

            if response.stop_reason == "end_turn":
                # Fire-and-forget cost tracking (non-blocking) — mirrors streaming path
                try:
                    from db.redis_store import increment_session_cost, get_user_brand
                    from db.redis.analytics import increment_agent_cost, increment_daily_cost
                    _bh = get_user_brand(user_id)
                    rates = getattr(settings, "COST_PER_MTK", {}).get(model, {"in": 0.0, "out": 0.0})
                    tokens_in, tokens_out, turn_cost = _usage_cost(response.usage, rates)
                    asyncio.create_task(asyncio.to_thread(
                        increment_session_cost, user_id, tokens_in, tokens_out, turn_cost,
                    ))
                    asyncio.create_task(asyncio.to_thread(
                        increment_agent_cost, agent_name,
                        tokens_in, tokens_out, turn_cost, brand_hash=_bh,
                    ))
                    asyncio.create_task(asyncio.to_thread(
                        increment_daily_cost, turn_cost, brand_hash=_bh,
                    ))
                except Exception:
                    pass  # intentional: metrics are best-effort
                return self._extract_text(response)

            if response.stop_reason == "tool_use":
                messages.append({
                    "role": "assistant",
                    "content": self._serialize_content(response.content),
                })

                # Collect all tool_use blocks and execute in parallel
                tool_blocks = [b for b in response.content if b.type == "tool_use"]

                for b in tool_blocks:
                    logger.info("tool call: %s | input=%s", b.name, b.input)

                results = await asyncio.gather(*[
                    self.tool_executor.execute(b.name, b.input, user_id)
                    for b in tool_blocks
                ], return_exceptions=True)

                tool_results = []
                for block, result in zip(tool_blocks, results):
                    if isinstance(result, Exception):
                        logger.warning("tool %s failed: %s", block.name, result)
                        result = f"Error executing {block.name}: {result}"
                    logger.debug("tool result: %s → %s", block.name, str(result)[:300])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(result),
                    })

                messages.append({"role": "user", "content": tool_results})

                # Phase C cancellation checkpoint — checked between tool-call iterations
                # so a new incoming WhatsApp message can interrupt a long-running tool chain.
                try:
                    from db.redis_store import is_cancel_requested, clear_cancel_requested
                    if is_cancel_requested(user_id):
                        clear_cancel_requested(user_id)
                        logger.info(
                            "run_agent cancelled at iteration %d by new message, user=%s",
                            iteration + 1, user_id,
                        )
                        return ""  # empty return — drain task will process the new message
                except Exception:
                    pass  # intentional: cancellation check is best-effort

                continue

            return self._extract_text(response)

        return "I'm having trouble processing this request. Could you rephrase?"

    # ------------------------------------------------------------------
    # Streaming variant — yields SSE event dicts
    # ------------------------------------------------------------------

    async def run_agent_stream(
        self,
        system_prompt: str | list[str],
        tools: list[dict],
        messages: list[dict],
        model: str,
        user_id: str,
        tool_executor: ToolExecutor | None = None,
        max_iterations: int | None = None,
        agent_name: str = "unknown",
    ) -> AsyncGenerator[dict, None]:
        """Streaming version of run_agent. Yields dicts like
        {"event": "content_delta", "data": {"text": "…"}} that the
        caller serialises as SSE frames.
        """
        if max_iterations is None:
            max_iterations = settings.MAX_AGENT_ITERATIONS

        executor = tool_executor or self.tool_executor

        system = self._build_system_blocks(system_prompt)

        cached_tools = []
        for i, tool in enumerate(tools):
            t = dict(tool)
            if i == len(tools) - 1:
                t["cache_control"] = {"type": "ephemeral"}
            cached_tools.append(t)

        # Fix P11: merge consecutive same-role messages before sending to Anthropic
        messages = self._sanitize_messages(messages)

        for iteration in range(max_iterations):
            logger.debug("stream iteration %d/%d", iteration + 1, max_iterations)

            kwargs = {
                "model": model,
                "max_tokens": MAX_TOKENS_RESPONSE,
                "system": system,
                "messages": messages,
                "timeout": settings.LLM_REQUEST_TIMEOUT,
            }
            if cached_tools:
                kwargs["tools"] = cached_tools

            # Track tool-use blocks built from deltas
            current_tool_id: str | None = None
            current_tool_name: str | None = None
            current_tool_json = ""

            try:
                async with self.client.messages.stream(**kwargs) as stream:
                    async for event in stream:
                        # -- text deltas --
                        if event.type == "content_block_delta":
                            if hasattr(event.delta, "text"):
                                yield {"event": "content_delta", "data": {"text": event.delta.text}}
                            elif hasattr(event.delta, "partial_json"):
                                current_tool_json += event.delta.partial_json

                        # -- block boundaries --
                        elif event.type == "content_block_start":
                            block = event.content_block
                            if block.type == "tool_use":
                                current_tool_id = block.id
                                current_tool_name = block.name
                                current_tool_json = ""
                                logger.info("stream tool call: %s", block.name)
                                yield {"event": "tool_start", "data": {"tool": block.name}}

                        elif event.type == "content_block_stop":
                            # Reset per-block trackers (tool input fully received)
                            current_tool_id = None
                            current_tool_name = None
                            current_tool_json = ""

                    # Get fully-assembled response for the tool-use loop
                    response = await stream.get_final_message()

            except anthropic.RateLimitError:
                logger.warning("rate limited during stream")
                await asyncio.sleep(2)
                continue
            except anthropic.APIError as e:
                logger.error("API error during stream: %s", e)
                yield {"event": "error", "data": {"text": "I'm experiencing a temporary issue. Please try again."}}
                return

            if response is None:
                yield {"event": "error", "data": {"text": "I'm experiencing a temporary issue. Please try again."}}
                return

            logger.debug("stream stop_reason=%s", response.stop_reason)

            if response.stop_reason == "end_turn":
                # Fire-and-forget cost tracking (non-blocking)
                try:
                    from db.redis_store import increment_session_cost, get_user_brand
                    from db.redis.analytics import increment_agent_cost, increment_daily_cost
                    _bh = get_user_brand(user_id)
                    rates = getattr(settings, "COST_PER_MTK", {}).get(model, {"in": 0.0, "out": 0.0})
                    tokens_in, tokens_out, turn_cost = _usage_cost(response.usage, rates)
                    # Per-user rolling session cost (7-day TTL)
                    asyncio.create_task(asyncio.to_thread(
                        increment_session_cost, user_id, tokens_in, tokens_out, turn_cost,
                    ))
                    # Per-agent + daily totals (90-day TTL, powers command-center)
                    asyncio.create_task(asyncio.to_thread(
                        increment_agent_cost, agent_name,
                        tokens_in, tokens_out, turn_cost, brand_hash=_bh,
                    ))
                    asyncio.create_task(asyncio.to_thread(
                        increment_daily_cost, turn_cost, brand_hash=_bh,
                    ))
                except Exception:
                    pass  # intentional: metrics are best-effort
                return  # all text already streamed via content_delta events

            if response.stop_reason == "tool_use":
                # Append assistant turn so the loop can continue
                messages.append({
                    "role": "assistant",
                    "content": self._serialize_content(response.content),
                })

                # Execute all tool calls in parallel
                tool_blocks = [b for b in response.content if b.type == "tool_use"]

                results = await asyncio.gather(*[
                    executor.execute(b.name, b.input, user_id)
                    for b in tool_blocks
                ], return_exceptions=True)

                tool_results = []
                for block, result in zip(tool_blocks, results):
                    if isinstance(result, Exception):
                        logger.warning("stream tool %s failed: %s", block.name, result)
                        result = f"Error executing {block.name}: {result}"
                    logger.debug("stream tool result: %s → %s", block.name, str(result)[:200])
                    yield {"event": "tool_done", "data": {"tool": block.name}}
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(result),
                    })

                # Inject a separator so post-tool response text starts on a new line
                # instead of being glued directly onto the pre-tool preamble text.
                yield {"event": "content_delta", "data": {"text": "\n\n"}}

                messages.append({"role": "user", "content": tool_results})

                # Phase C cancellation checkpoint — streaming path
                # If the user interrupted (web AbortController), we can stop here so
                # the partial text already streamed stands as-is.
                try:
                    from db.redis_store import is_cancel_requested, clear_cancel_requested
                    if is_cancel_requested(user_id):
                        clear_cancel_requested(user_id)
                        logger.info(
                            "run_agent_stream cancelled at iteration %d by interrupt, user=%s",
                            iteration + 1, user_id,
                        )
                        return  # caller handles partial text as interrupted response
                except Exception:
                    pass  # intentional: cancellation check is best-effort

                continue

            # Unexpected stop reason — text was already streamed
            return

    async def classify(
        self,
        system_prompt: str,
        messages: list[dict],
        model: str,
    ) -> Optional[dict]:
        """Single-turn classification (supervisor routing). No tool loop."""
        system = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        for attempt in range(3):
            try:
                response = await self.client.messages.create(
                    model=model,
                    max_tokens=MAX_TOKENS_CLASSIFY,
                    system=system,
                    messages=messages,
                    timeout=settings.LLM_CLASSIFY_TIMEOUT,
                )
                raw = self._extract_text(response).strip()
                if not raw:
                    logger.warning(
                        "classify attempt %d: empty response — stop_reason=%s content_types=%s",
                        attempt + 1,
                        getattr(response, "stop_reason", "?"),
                        [getattr(b, "type", "?") for b in response.content],
                    )
                    await asyncio.sleep(0.5)
                    continue
                import json
                cleaned = self._clean_json(raw)
                logger.debug("classify raw=%s → cleaned=%s", repr(raw[:80]), repr(cleaned))
                return json.loads(cleaned)
            except Exception as e:
                logger.warning("classify attempt %d failed: %s", attempt + 1, e)
                await asyncio.sleep(0.5)
        return None

    async def _call_api(
        self,
        model: str,
        system: list,
        tools: list,
        messages: list,
    ):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                kwargs = {
                    "model": model,
                    "max_tokens": MAX_TOKENS_RESPONSE,
                    "system": system,
                    "messages": messages,
                    "timeout": settings.LLM_REQUEST_TIMEOUT,
                }
                if tools:
                    kwargs["tools"] = tools
                return await self.client.messages.create(**kwargs)
            except anthropic.RateLimitError:
                wait = 2 ** attempt
                logger.warning("rate limited, waiting %ds", wait)
                await asyncio.sleep(wait)
            except anthropic.APIError as e:
                logger.error("API error: %s", e)
                if attempt == max_retries - 1:
                    # Fire-and-forget: log API error to PostgreSQL
                    try:
                        from db.postgres import insert_error_event
                        asyncio.create_task(insert_error_event(
                            user_id="system",
                            brand_hash=None,
                            error_type="api_timeout",
                            error_source="anthropic",
                            error_message=str(e)[:500],
                            context={"model": model, "attempt": attempt + 1},
                        ))
                    except Exception:
                        pass
                    return None
                await asyncio.sleep(1)
        return None

    @staticmethod
    def _build_system_blocks(system_prompt: str | list[str]) -> list[dict]:
        """Build Anthropic API system blocks with cache_control.

        Supports two formats:
        - str: Single block, fully cached (legacy — all agents except dynamic broker)
        - list[str]: Two blocks — first cached (base), second dynamic (NOT cached)
        """
        from core.untrusted import UNTRUSTED_CONTENT_RULE
        # Prepend the standing untrusted-content rule to the first (cached) block so
        # every agent inherits it. The prefix is constant → cache stays warm.
        prefix = UNTRUSTED_CONTENT_RULE + "\n\n"
        if isinstance(system_prompt, list):
            blocks = [
                {"type": "text", "text": prefix + system_prompt[0], "cache_control": {"type": "ephemeral"}},
            ]
            if len(system_prompt) > 1 and system_prompt[1]:
                blocks.append({"type": "text", "text": system_prompt[1]})
            return blocks
        return [
            {"type": "text", "text": prefix + system_prompt, "cache_control": {"type": "ephemeral"}},
        ]

    @staticmethod
    def _extract_text(response) -> str:
        parts = []
        for block in response.content:
            if block.type == "text":
                parts.append(block.text)
        return "\n".join(parts)

    @staticmethod
    def _clean_json(text: str) -> str:
        """Extract a JSON object from text that may have markdown fences,
        surrounding prose, nested objects, or single quotes.

        Robustness matters: the supervisor's reply is Haiku output, and any
        recovery failure here burns a retry and can drop routing to `default`
        (the UAT `classify attempt N failed` log spam). Improvements over the
        old ``\\{[^{}]*\\}`` regex: (1) extract the first BRACE-BALANCED object
        so nested objects and trailing prose are handled; (2) tolerate
        single-quoted JSON as a last resort.
        """
        import json as _json
        import re
        # Strip markdown code fences (```json ... ``` or ``` ... ```)
        text = re.sub(r"```(?:json)?\s*", "", text)
        text = re.sub(r"```", "", text).strip()
        # Extract the first brace-balanced {...} object (drops surrounding prose
        # and trailing text; handles nested objects the flat regex could not).
        start = text.find("{")
        if start != -1:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        text = text[start:i + 1]
                        break
        # Tolerate single-quoted JSON (a common Haiku slip) — only if the
        # double-quoted form doesn't already parse, so valid JSON is untouched.
        try:
            _json.loads(text)
        except Exception:
            swapped = text.replace("'", '"')
            try:
                _json.loads(swapped)
                text = swapped
            except Exception:
                pass
        return text

    @staticmethod
    def _sanitize_messages(messages: list[dict]) -> list[dict]:
        """Merge consecutive same-role messages to comply with Anthropic's alternating role requirement.

        Handles two real-world cases:
        - Consecutive assistant messages: admin message injected after AI response (human takeover)
        - Consecutive user messages: user messages accumulated during human_mode bypass in pipeline

        Content merging rules:
        - str + str → concatenate with double newline
        - list + str → append text block to list
        - str + list → prepend text block to list
        - list + list → concatenate lists (handles tool_result blocks)
        """
        if not messages:
            return messages

        sanitized: list[dict] = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")

            if sanitized and sanitized[-1]["role"] == role:
                # Merge consecutive same-role messages
                prev = sanitized[-1]
                prev_content = prev["content"]
                if isinstance(prev_content, str) and isinstance(content, str):
                    prev["content"] = prev_content + "\n\n" + content
                elif isinstance(prev_content, list) and isinstance(content, str):
                    prev["content"] = prev_content + [{"type": "text", "text": content}]
                elif isinstance(prev_content, str) and isinstance(content, list):
                    prev["content"] = [{"type": "text", "text": prev_content}] + content
                elif isinstance(prev_content, list) and isinstance(content, list):
                    prev["content"] = prev_content + content
                else:
                    prev["content"] = str(prev_content) + "\n\n" + str(content)
                logger.debug("sanitize_messages: merged consecutive %s messages", role)
            else:
                sanitized.append({k: v for k, v in msg.items()})

        return sanitized

    @staticmethod
    def _serialize_content(content) -> list[dict]:
        serialized = []
        for block in content:
            if block.type == "text":
                serialized.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                serialized.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
        return serialized
