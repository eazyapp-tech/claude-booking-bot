"""
core/litellm_engine.py — LiteLLM-backed engine for non-Anthropic model routing.

Same interface as AnthropicEngine: run_agent, run_agent_stream, tool_executor.
classify() raises NotImplementedError — supervisor ALWAYS stays on Anthropic.

Model names follow LiteLLM provider-prefix convention:
  openrouter/google/gemini-pro-1.5-flash-8b
  openrouter/anthropic/claude-haiku-4-5
  gemini/gemini-1.5-flash-latest
"""

import asyncio
import json
import os
from typing import AsyncGenerator, Optional

import litellm

from config import settings
from core.log import get_logger
from core.tool_executor import ToolExecutor

logger = get_logger("core.litellm_engine")

MAX_TOOL_ROUNDS = 15
MAX_TOKENS_RESPONSE = 4096


class EngineError(Exception):
    """Raised on a hard infra failure (API down / transport error) so the
    ModelRouter can fall back to Anthropic. NOT raised for normal completions,
    quality issues, or max-iteration exhaustion — only genuine infra failures."""


# ---------------------------------------------------------------------------
# Model-override parsing — optional provider/quant pin
# ---------------------------------------------------------------------------

def _parse_model_override(model: str) -> tuple[str, Optional[dict]]:
    """Split a model override into (litellm_model, extra_body | None).

    Optional OpenRouter provider pin via an "@provider/quant" suffix:
        openrouter/z-ai/glm-4.6                 → no pin
        openrouter/z-ai/glm-4.6@deepinfra/fp8   → pin DeepInfra, FP8 quant floor
        openrouter/z-ai/glm-4.6@deepinfra       → pin DeepInfra, any quant

    The returned litellm_model is the clean model string (used for both the
    API call AND the COST_PER_MTK lookup — the suffix must never leak into either).

    extra_body carries OpenRouter provider routing. An EXPLICIT provider pin is
    honest: allow_fallbacks=False, so if that provider doesn't serve the model
    (or is down) the call fails loudly → EngineError → Anthropic fallback, rather
    than silently routing to a different (possibly broken) endpoint. (Lesson from
    `@deepinfra/fp8`: DeepInfra serves no GLM-4.6 endpoint on OpenRouter, and with
    fallbacks ON the request silently routed to an endpoint that TRUNCATED tool
    names → a 62s max-iteration loop. Hard pin would have surfaced it instantly.)
    A quant-only pin (no provider) keeps fallbacks ON with `quantizations` as a
    hard quality floor (any fallback provider must serve that quant).
    """
    if "@" not in model:
        return model, None
    base, _, pin = model.partition("@")
    base = base.strip()
    pin = pin.strip()
    if not pin:
        return base, None
    provider, _, quant = pin.partition("/")
    provider = provider.strip().lower()
    quant = quant.strip().lower()
    routing: dict = {}
    if provider:
        routing["order"] = [provider]
        routing["allow_fallbacks"] = False  # honest pin: unavailable provider fails loud → Anthropic fallback
    else:
        routing["allow_fallbacks"] = True
    if quant:
        routing["quantizations"] = [quant]
    return base, {"provider": routing}


# ---------------------------------------------------------------------------
# Format conversion helpers
# ---------------------------------------------------------------------------

def _to_openai_tools(tools: list[dict]) -> list[dict]:
    """Convert Anthropic tool dicts (input_schema) → OpenAI function format."""
    result = []
    for t in tools:
        result.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return result


def _to_openai_messages(system_text: str, messages: list[dict]) -> list[dict]:
    """Convert Anthropic message history to OpenAI format.

    Anthropic tool results are embedded in user messages as a list of
    {type: tool_result, tool_use_id, content} blocks.
    OpenAI expects role="tool" messages with tool_call_id.

    Anthropic assistant turns containing tool_use blocks become assistant
    messages with tool_calls arrays (arguments as JSON string).
    """
    out: list[dict] = [{"role": "system", "content": system_text}]

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")

        if role == "user":
            if isinstance(content, list):
                tool_results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
                text_parts = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
                if tool_results:
                    for tr in tool_results:
                        out.append({
                            "role": "tool",
                            "tool_call_id": tr.get("tool_use_id", ""),
                            "content": tr.get("content", ""),
                        })
                    if text_parts:
                        text = "\n".join(b.get("text", "") for b in text_parts)
                        out.append({"role": "user", "content": text})
                else:
                    text = "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
                    out.append({"role": "user", "content": text})
            else:
                out.append({"role": "user", "content": content or ""})

        elif role == "assistant":
            if isinstance(content, list):
                tool_use_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
                text_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
                text_content = "\n".join(b.get("text", "") for b in text_blocks) or None
                if tool_use_blocks:
                    tool_calls = []
                    for b in tool_use_blocks:
                        tool_calls.append({
                            "id": b.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": b.get("name", ""),
                                "arguments": json.dumps(b.get("input", {})),
                            },
                        })
                    out.append({
                        "role": "assistant",
                        "content": text_content,
                        "tool_calls": tool_calls,
                    })
                else:
                    out.append({"role": "assistant", "content": text_content or ""})
            else:
                out.append({"role": "assistant", "content": content or ""})

    return out


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class LiteLLMEngine:
    """LiteLLM-backed engine — identical interface to AnthropicEngine."""

    def __init__(self, tool_executor: ToolExecutor):
        self.tool_executor = tool_executor
        if settings.OPENROUTER_API_KEY:
            os.environ["OPENROUTER_API_KEY"] = settings.OPENROUTER_API_KEY

    # ------------------------------------------------------------------
    # Non-streaming path
    # ------------------------------------------------------------------

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

        model, extra_body = _parse_model_override(model)
        system_text = self._build_system_text(system_prompt)
        oai_tools = _to_openai_tools(tools) if tools else []
        history = list(messages)

        for iteration in range(max_iterations):
            oai_messages = _to_openai_messages(system_text, history)
            kwargs = {
                "model": model,
                "messages": oai_messages,
                "max_tokens": MAX_TOKENS_RESPONSE,
                "timeout": settings.LLM_REQUEST_TIMEOUT,
            }
            if oai_tools:
                kwargs["tools"] = oai_tools
            if extra_body:
                kwargs["extra_body"] = extra_body

            try:
                response = await litellm.acompletion(**kwargs)
            except Exception as e:
                logger.error("LiteLLM API error (iteration %d): %s", iteration + 1, e)
                raise EngineError(str(e)) from e

            choice = response.choices[0]
            finish_reason = choice.finish_reason
            logger.debug("iteration %d/%d | finish_reason=%s", iteration + 1, max_iterations, finish_reason)

            if finish_reason in ("stop", None, "end_turn"):
                self._track_cost(response, model, agent_name, user_id)
                return choice.message.content or ""

            if finish_reason == "tool_calls":
                tool_calls = choice.message.tool_calls or []

                # Build Anthropic-format assistant content for our message store
                tool_use_blocks = []
                for tc in tool_calls:
                    try:
                        args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except Exception:
                        args = {}
                    tool_use_blocks.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.function.name,
                        "input": args,
                    })
                assistant_content = []
                if choice.message.content:
                    assistant_content.append({"type": "text", "text": choice.message.content})
                assistant_content.extend(tool_use_blocks)
                history.append({"role": "assistant", "content": assistant_content})

                for tc in tool_calls:
                    logger.info("tool call: %s", tc.function.name)

                results = await asyncio.gather(*[
                    self.tool_executor.execute(
                        b["name"], b["input"], user_id,
                    )
                    for b in tool_use_blocks
                ], return_exceptions=True)

                tool_results = []
                for b, result in zip(tool_use_blocks, results):
                    if isinstance(result, Exception):
                        logger.warning("tool %s failed: %s", b["name"], result)
                        result = f"Error executing {b['name']}: {result}"
                    logger.debug("tool result: %s → %s", b["name"], str(result)[:300])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": b["id"],
                        "content": str(result),
                    })
                history.append({"role": "user", "content": tool_results})

                # Phase C cancellation checkpoint (mirrors AnthropicEngine)
                try:
                    from db.redis_store import is_cancel_requested, clear_cancel_requested
                    if is_cancel_requested(user_id):
                        clear_cancel_requested(user_id)
                        logger.info("run_agent cancelled at iteration %d, user=%s", iteration + 1, user_id)
                        return ""
                except Exception:
                    pass

                continue

            # length / content_filter / etc.
            return choice.message.content or "I'm having trouble processing this request."

        return "I'm having trouble processing this request. Could you rephrase?"

    # ------------------------------------------------------------------
    # Streaming path
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
        if max_iterations is None:
            max_iterations = settings.MAX_AGENT_ITERATIONS

        executor = tool_executor or self.tool_executor
        model, extra_body = _parse_model_override(model)
        system_text = self._build_system_text(system_prompt)
        oai_tools = _to_openai_tools(tools) if tools else []
        history = list(messages)

        for iteration in range(max_iterations):
            oai_messages = _to_openai_messages(system_text, history)
            kwargs = {
                "model": model,
                "messages": oai_messages,
                "max_tokens": MAX_TOKENS_RESPONSE,
                "timeout": settings.LLM_REQUEST_TIMEOUT,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if oai_tools:
                kwargs["tools"] = oai_tools
            if extra_body:
                kwargs["extra_body"] = extra_body

            try:
                stream = await litellm.acompletion(**kwargs)
            except Exception as e:
                logger.error("LiteLLM stream error (iteration %d): %s", iteration + 1, e)
                raise EngineError(str(e)) from e

            # Accumulate streaming chunks
            collected_text = ""
            collected_tool_calls: dict[int, dict] = {}  # index → {id, name, arguments}
            finish_reason: str | None = None
            collected_usage = None

            try:
                async for chunk in stream:
                    # Usage arrives in a final chunk (empty choices) when
                    # stream_options.include_usage is set — capture before the skip.
                    chunk_usage = getattr(chunk, "usage", None)
                    if chunk_usage:
                        collected_usage = chunk_usage
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason

                    delta = choice.delta

                    # Text delta
                    text = getattr(delta, "content", None)
                    if text:
                        collected_text += text
                        yield {"event": "content_delta", "data": {"text": text}}

                    # Tool call delta
                    tc_deltas = getattr(delta, "tool_calls", None)
                    if tc_deltas:
                        for tc_delta in tc_deltas:
                            idx = getattr(tc_delta, "index", 0)
                            if idx not in collected_tool_calls:
                                collected_tool_calls[idx] = {"id": "", "name": "", "arguments": ""}

                            tc_id = getattr(tc_delta, "id", None)
                            if tc_id:
                                collected_tool_calls[idx]["id"] = tc_id

                            func = getattr(tc_delta, "function", None)
                            if func:
                                name = getattr(func, "name", None)
                                if name and not collected_tool_calls[idx]["name"]:
                                    collected_tool_calls[idx]["name"] = name
                                    logger.info("stream tool call: %s", name)
                                    yield {"event": "tool_start", "data": {"tool": name}}
                                elif name:
                                    collected_tool_calls[idx]["name"] = name
                                args_chunk = getattr(func, "arguments", None)
                                if args_chunk:
                                    collected_tool_calls[idx]["arguments"] += args_chunk

            except Exception as e:
                logger.error("LiteLLM stream chunk error: %s", e)
                raise EngineError(str(e)) from e

            logger.debug("stream finish_reason=%s", finish_reason)

            if finish_reason in ("stop", "end_turn") or (finish_reason is None and not collected_tool_calls):
                self._track_cost_usage(collected_usage, model, agent_name, user_id)
                return

            if finish_reason == "tool_calls" or collected_tool_calls:
                # Build Anthropic-format assistant content for history
                tool_use_blocks = []
                for idx in sorted(collected_tool_calls):
                    tc = collected_tool_calls[idx]
                    try:
                        args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                    except Exception:
                        args = {}
                    tool_use_blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["name"],
                        "input": args,
                    })

                assistant_content = []
                if collected_text:
                    assistant_content.append({"type": "text", "text": collected_text})
                assistant_content.extend(tool_use_blocks)
                history.append({"role": "assistant", "content": assistant_content})

                results = await asyncio.gather(*[
                    executor.execute(b["name"], b["input"], user_id)
                    for b in tool_use_blocks
                ], return_exceptions=True)

                tool_results = []
                for b, result in zip(tool_use_blocks, results):
                    if isinstance(result, Exception):
                        logger.warning("stream tool %s failed: %s", b["name"], result)
                        result = f"Error executing {b['name']}: {result}"
                    logger.debug("stream tool result: %s → %s", b["name"], str(result)[:200])
                    yield {"event": "tool_done", "data": {"tool": b["name"]}}
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": b["id"],
                        "content": str(result),
                    })

                yield {"event": "content_delta", "data": {"text": "\n\n"}}
                history.append({"role": "user", "content": tool_results})

                # Phase C cancellation checkpoint
                try:
                    from db.redis_store import is_cancel_requested, clear_cancel_requested
                    if is_cancel_requested(user_id):
                        clear_cancel_requested(user_id)
                        logger.info("run_agent_stream cancelled at iteration %d, user=%s", iteration + 1, user_id)
                        return
                except Exception:
                    pass

                continue

            return

    def classify(self, *args, **kwargs):
        raise NotImplementedError(
            "Supervisor always uses AnthropicEngine — classify() is not available on LiteLLMEngine"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_system_text(system_prompt: str | list[str]) -> str:
        """Flatten to a single string, prepending UNTRUSTED_CONTENT_RULE."""
        from core.untrusted import UNTRUSTED_CONTENT_RULE
        prefix = UNTRUSTED_CONTENT_RULE + "\n\n"
        if isinstance(system_prompt, list):
            text = "\n\n".join(p for p in system_prompt if p)
        else:
            text = system_prompt or ""
        return prefix + text

    def _track_cost(self, response, model: str, agent_name: str, user_id: str) -> None:
        """Best-effort fire-and-forget cost tracking (non-streaming path)."""
        self._track_cost_usage(getattr(response, "usage", None), model, agent_name, user_id)

    def _track_cost_usage(self, usage, model: str, agent_name: str, user_id: str) -> None:
        """Best-effort fire-and-forget cost tracking from a usage object."""
        if usage is None:
            return
        try:
            tokens_in = getattr(usage, "prompt_tokens", 0) or 0
            tokens_out = getattr(usage, "completion_tokens", 0) or 0
            rates = getattr(settings, "COST_PER_MTK", {}).get(model, {"in": 0.0, "out": 0.0})
            cost = (tokens_in * rates["in"] + tokens_out * rates["out"]) / 1_000_000

            from db.redis_store import increment_session_cost, get_user_brand
            from db.redis.analytics import increment_agent_cost, increment_daily_cost
            _bh = get_user_brand(user_id)
            asyncio.create_task(asyncio.to_thread(
                increment_session_cost, user_id, tokens_in, tokens_out, cost,
            ))
            asyncio.create_task(asyncio.to_thread(
                increment_agent_cost, agent_name, tokens_in, tokens_out, cost, brand_hash=_bh,
            ))
            asyncio.create_task(asyncio.to_thread(
                increment_daily_cost, cost, brand_hash=_bh,
            ))
        except Exception:
            pass  # intentional: metrics are best-effort
