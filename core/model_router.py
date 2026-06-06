"""
core/model_router.py — Transparent engine wrapper with per-brand model overrides.

Sits in front of AnthropicEngine and LiteLLMEngine.  On every call it:
  1. Reads the active model override from Redis (brand-scoped → global → None).
  2. If an override is set, creates (or reuses) a LiteLLMEngine and delegates.
  3. If no override, delegates to AnthropicEngine as before — zero behaviour change.

The supervisor classify() always stays on AnthropicEngine (never overridden).

Exposes the same interface as AnthropicEngine so all callers (booking, broker,
profile, default agents) need zero changes.
"""

from typing import AsyncGenerator, Optional

from core.log import get_logger
from core.tool_executor import ToolExecutor

logger = get_logger("core.model_router")


class ModelRouter:
    """Transparent wrapper that picks the right engine per agent/brand call."""

    def __init__(self, tool_executor: ToolExecutor):
        self.tool_executor = tool_executor
        self._anthropic: "AnthropicEngine | None" = None
        self._litellm: "LiteLLMEngine | None" = None

    # -- lazy engine access ---------------------------------------------------

    @property
    def _anthropic_engine(self):
        if self._anthropic is None:
            from core.claude import AnthropicEngine
            self._anthropic = AnthropicEngine(tool_executor=self.tool_executor)
        return self._anthropic

    @property
    def _litellm_engine(self):
        if self._litellm is None:
            from core.litellm_engine import LiteLLMEngine
            self._litellm = LiteLLMEngine(tool_executor=self.tool_executor)
        return self._litellm

    # -- engine picker --------------------------------------------------------

    def _pick_engine(self, agent_name: str, user_id: str):
        """Return (engine, effective_model | None).

        Reads per-brand override first, then global.  Returns (anthropic_engine, None)
        when no override is set so callers pass their configured model unchanged.
        """
        try:
            from config import settings
            # An override is meaningless without an OpenRouter key — skip LiteLLM
            # entirely (no failed-call-then-fallback tax) until the key is set.
            if not settings.OPENROUTER_API_KEY:
                self._anthropic_engine.tool_executor = self.tool_executor
                return self._anthropic_engine, None

            from db.redis_store import get_user_brand
            from db.redis.brand import get_model_override
            brand_hash = get_user_brand(user_id)
            override = (
                get_model_override(agent_name, brand_hash)
                or get_model_override(agent_name, None)
            )
            if override:
                logger.info("model override: agent=%s brand=%s → %s", agent_name, brand_hash, override)
                # Keep tool_executor in sync (agents proxy it onto the engine)
                self._litellm_engine.tool_executor = self.tool_executor
                return self._litellm_engine, override
        except Exception as e:
            logger.debug("model_router Redis read failed (fallback to Anthropic): %s", e)

        self._anthropic_engine.tool_executor = self.tool_executor
        return self._anthropic_engine, None

    # -- public interface (mirrors AnthropicEngine) ---------------------------

    async def run_agent(
        self,
        system_prompt,
        tools: list[dict],
        messages: list[dict],
        model: str,
        user_id: str,
        max_iterations: int = None,
        agent_name: str = "unknown",
    ) -> str:
        engine, override_model = self._pick_engine(agent_name, user_id)
        if override_model is None:
            # No override → pure Anthropic path, zero added behaviour.
            return await engine.run_agent(
                system_prompt=system_prompt, tools=tools, messages=messages,
                model=model, user_id=user_id,
                max_iterations=max_iterations, agent_name=agent_name,
            )
        # Override active → try the routed engine; on a HARD infra failure only,
        # fall back to Anthropic with the agent's configured model so a booking
        # never breaks on an OpenRouter outage. Quality issues never fall back.
        from core.litellm_engine import EngineError
        try:
            return await engine.run_agent(
                system_prompt=system_prompt, tools=tools, messages=messages,
                model=override_model, user_id=user_id,
                max_iterations=max_iterations, agent_name=agent_name,
            )
        except EngineError as e:
            logger.warning(
                "routed engine hard-failed (agent=%s model=%s) → Anthropic fallback: %s",
                agent_name, override_model, e,
            )
            fallback = self._anthropic_engine
            fallback.tool_executor = self.tool_executor
            return await fallback.run_agent(
                system_prompt=system_prompt, tools=tools, messages=messages,
                model=model, user_id=user_id,
                max_iterations=max_iterations, agent_name=agent_name,
            )

    async def run_agent_stream(
        self,
        system_prompt,
        tools: list[dict],
        messages: list[dict],
        model: str,
        user_id: str,
        tool_executor: ToolExecutor | None = None,
        max_iterations: int | None = None,
        agent_name: str = "unknown",
    ) -> AsyncGenerator[dict, None]:
        # NOTE: this is an async GENERATOR (yields) — callers do
        # `async for ev in engine.run_agent_stream(...)`, matching AnthropicEngine.
        engine, override_model = self._pick_engine(agent_name, user_id)

        if override_model is None:
            # No override → pure Anthropic passthrough, zero added behaviour.
            async for ev in engine.run_agent_stream(
                system_prompt=system_prompt, tools=tools, messages=messages,
                model=model, user_id=user_id, tool_executor=tool_executor,
                max_iterations=max_iterations, agent_name=agent_name,
            ):
                yield ev
            return

        # Override active → stream the routed engine. Fall back to Anthropic ONLY
        # if it hard-fails BEFORE any content streamed (otherwise we'd duplicate
        # already-sent text). Mid-stream failure surfaces a clean error event.
        from core.litellm_engine import EngineError
        yielded_content = False
        try:
            async for ev in engine.run_agent_stream(
                system_prompt=system_prompt, tools=tools, messages=messages,
                model=override_model, user_id=user_id, tool_executor=tool_executor,
                max_iterations=max_iterations, agent_name=agent_name,
            ):
                if ev.get("event") in ("content_delta", "tool_start", "tool_done"):
                    yielded_content = True
                yield ev
            return
        except EngineError as e:
            if yielded_content:
                logger.error(
                    "routed engine hard-failed mid-stream (agent=%s model=%s) — no fallback: %s",
                    agent_name, override_model, e,
                )
                yield {"event": "error", "data": {"text": "I'm experiencing a temporary issue. Please try again."}}
                return
            logger.warning(
                "routed engine hard-failed pre-content (agent=%s model=%s) → Anthropic fallback: %s",
                agent_name, override_model, e,
            )

        # Reached only when the routed engine failed before emitting content.
        fallback = self._anthropic_engine
        fallback.tool_executor = self.tool_executor
        async for ev in fallback.run_agent_stream(
            system_prompt=system_prompt, tools=tools, messages=messages,
            model=model, user_id=user_id, tool_executor=tool_executor,
            max_iterations=max_iterations, agent_name=agent_name,
        ):
            yield ev

    async def classify(
        self,
        system_prompt: str,
        messages: list[dict],
        model: str,
    ):
        """Supervisor always uses Anthropic — never overridden."""
        return await self._anthropic_engine.classify(
            system_prompt=system_prompt,
            messages=messages,
            model=model,
        )
