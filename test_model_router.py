"""
test_model_router.py — Model-routing regression (hermetic, no network/Redis/LLM).

Proves the ModelRouter + LiteLLMEngine seam:
  [A] _parse_model_override — clean model + optional OpenRouter provider/quant pin;
      the suffix never leaks into the model string used for the API call or cost.
  [B] _to_openai_tools / _to_openai_messages — Anthropic↔OpenAI format conversion
      round-trips tool calls and tool results correctly.
  [C] ModelRouter._pick_engine — no key → Anthropic (zero regression); key + no
      override → Anthropic; key + override → LiteLLM with the override model.
  [D] ZERO REGRESSION — with no override the router delegates to Anthropic with
      the agent's configured model UNCHANGED (run_agent + run_agent_stream).
  [E] HARD-FAILURE FALLBACK — when the routed engine raises EngineError the
      router falls back to Anthropic with the configured model (non-streaming),
      and (streaming) only when the failure is BEFORE any content was yielded.
  [F] SUPERVISOR — classify() always uses Anthropic, never routed.

Run: python test_model_router.py   (exit 0 = pass)
"""

import asyncio
import os
import sys

# Hermetic env — no real keys, no real services.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("OPENROUTER_API_KEY", "")  # default: key absent

import config  # noqa: E402
from core.litellm_engine import (  # noqa: E402
    EngineError,
    _parse_model_override,
    _to_openai_tools,
    _to_openai_messages,
)
from core.model_router import ModelRouter  # noqa: E402

passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {name}")


# ---------------------------------------------------------------------------
# [A] _parse_model_override
# ---------------------------------------------------------------------------
def test_parse():
    m, eb = _parse_model_override("openrouter/z-ai/glm-4.6")
    check("no pin → clean model", m == "openrouter/z-ai/glm-4.6")
    check("no pin → no extra_body", eb is None)

    m, eb = _parse_model_override("openrouter/z-ai/glm-4.6@deepinfra/fp8")
    check("pin → suffix stripped from model", m == "openrouter/z-ai/glm-4.6")
    check("pin → provider order", eb["provider"]["order"] == ["deepinfra"])
    check("pin → quant floor", eb["provider"]["quantizations"] == ["fp8"])
    check("pin → fallbacks stay on", eb["provider"]["allow_fallbacks"] is True)

    m, eb = _parse_model_override("openrouter/z-ai/glm-4.6@deepinfra/bf16")
    check("bf16 pin", eb["provider"]["quantizations"] == ["bf16"])

    m, eb = _parse_model_override("openrouter/z-ai/glm-4.6@deepinfra")
    check("provider-only pin → no quant key", "quantizations" not in eb["provider"])
    check("provider-only pin → order set", eb["provider"]["order"] == ["deepinfra"])

    # Cost lookup must resolve on the CLEAN model, suffix-free.
    m, _ = _parse_model_override("openrouter/z-ai/glm-4.6@deepinfra/fp8")
    rates = config.settings.COST_PER_MTK.get(m)
    check("cost lookup resolves on clean model", rates == {"in": 0.43, "out": 1.74})


# ---------------------------------------------------------------------------
# [B] format conversion
# ---------------------------------------------------------------------------
def test_format():
    tools = [{
        "name": "search", "description": "find props",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    }]
    oai = _to_openai_tools(tools)
    check("tool → function type", oai[0]["type"] == "function")
    check("tool → name preserved", oai[0]["function"]["name"] == "search")
    check("tool → input_schema → parameters", oai[0]["function"]["parameters"]["required"] == ["q"])

    msgs = [
        {"role": "user", "content": "find me a PG"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "searching"},
            {"type": "tool_use", "id": "tu1", "name": "search", "input": {"q": "Kurla"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu1", "content": "[{\"name\": \"PG1\"}]"},
        ]},
    ]
    out = _to_openai_messages("SYS", msgs)
    check("msg[0] system injected", out[0]["role"] == "system" and out[0]["content"] == "SYS")
    check("user text preserved", out[1] == {"role": "user", "content": "find me a PG"})
    check("assistant tool_use → tool_calls", out[2]["role"] == "assistant" and out[2]["tool_calls"][0]["function"]["name"] == "search")
    check("assistant tool args JSON-encoded", out[2]["tool_calls"][0]["function"]["arguments"] == '{"q": "Kurla"}')
    check("tool_result → role=tool", out[3]["role"] == "tool" and out[3]["tool_call_id"] == "tu1")


# ---------------------------------------------------------------------------
# Fakes for router tests
# ---------------------------------------------------------------------------
class FakeAnthropic:
    def __init__(self):
        self.tool_executor = None
        self.calls = []
        self.stream_calls = []
        self.classify_calls = []

    async def run_agent(self, **kw):
        self.calls.append(kw)
        return f"anthropic:{kw['model']}"

    async def run_agent_stream(self, **kw):
        self.stream_calls.append(kw)
        yield {"event": "content_delta", "data": {"text": f"anthropic:{kw['model']}"}}

    async def classify(self, **kw):
        self.classify_calls.append(kw)
        return {"agent": "broker", "skills": []}


class FakeLiteLLM:
    def __init__(self, fail=False, fail_after_content=False):
        self.tool_executor = None
        self.calls = []
        self.stream_calls = []
        self.fail = fail
        self.fail_after_content = fail_after_content

    async def run_agent(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise EngineError("simulated hard failure")
        return f"litellm:{kw['model']}"

    async def run_agent_stream(self, **kw):
        self.stream_calls.append(kw)
        if self.fail and not self.fail_after_content:
            raise EngineError("pre-content hard failure")
        yield {"event": "content_delta", "data": {"text": f"litellm:{kw['model']}"}}
        if self.fail and self.fail_after_content:
            raise EngineError("mid-stream hard failure")


def _router_with_fakes(anthropic, litellm, override=None, key="test-or-key"):
    """Build a ModelRouter wired to fakes + patched Redis/key reads."""
    r = ModelRouter(tool_executor="EXEC")
    r._anthropic = anthropic
    r._litellm = litellm
    config.settings.OPENROUTER_API_KEY = key

    import db.redis_store as rs
    import db.redis.brand as brand
    rs.get_user_brand = lambda uid: "brandX"
    brand.get_model_override = lambda agent, bh=None: override if (agent == "broker") else None
    return r


async def collect(agen):
    return [ev async for ev in agen]


# ---------------------------------------------------------------------------
# [C][D][E][F] router behaviour
# ---------------------------------------------------------------------------
async def test_router():
    # [D] No key → Anthropic, model unchanged (zero regression even if override exists)
    a, l = FakeAnthropic(), FakeLiteLLM()
    r = _router_with_fakes(a, l, override="openrouter/z-ai/glm-4.6", key="")
    out = await r.run_agent(system_prompt="p", tools=[], messages=[], model="claude-haiku-4-5-20251001", user_id="u", agent_name="broker")
    check("no key → Anthropic used", out == "anthropic:claude-haiku-4-5-20251001")
    check("no key → litellm untouched", l.calls == [])

    # [C] key + no override → Anthropic, model unchanged
    a, l = FakeAnthropic(), FakeLiteLLM()
    r = _router_with_fakes(a, l, override=None)
    out = await r.run_agent(system_prompt="p", tools=[], messages=[], model="claude-sonnet-4-6", user_id="u", agent_name="booking")
    check("no override → Anthropic used", out == "anthropic:claude-sonnet-4-6")
    check("no override → litellm untouched", l.calls == [])

    # [C] key + override → LiteLLM with override model
    a, l = FakeAnthropic(), FakeLiteLLM()
    r = _router_with_fakes(a, l, override="openrouter/z-ai/glm-4.6@deepinfra/fp8")
    out = await r.run_agent(system_prompt="p", tools=[], messages=[], model="claude-haiku-4-5-20251001", user_id="u", agent_name="broker")
    check("override → LiteLLM used", out == "litellm:openrouter/z-ai/glm-4.6@deepinfra/fp8")
    check("override → Anthropic untouched", a.calls == [])
    check("override → tool_executor proxied", l.tool_executor == "EXEC")

    # [C] override only applies to the named agent (broker), not others
    a, l = FakeAnthropic(), FakeLiteLLM()
    r = _router_with_fakes(a, l, override="openrouter/z-ai/glm-4.6")
    out = await r.run_agent(system_prompt="p", tools=[], messages=[], model="claude-sonnet-4-6", user_id="u", agent_name="profile")
    check("non-targeted agent → Anthropic", out == "anthropic:claude-sonnet-4-6")

    # [E] hard failure (non-streaming) → Anthropic fallback with configured model
    a, l = FakeAnthropic(), FakeLiteLLM(fail=True)
    r = _router_with_fakes(a, l, override="openrouter/z-ai/glm-4.6")
    out = await r.run_agent(system_prompt="p", tools=[], messages=[], model="claude-haiku-4-5-20251001", user_id="u", agent_name="broker")
    check("hard fail → Anthropic fallback", out == "anthropic:claude-haiku-4-5-20251001")
    check("hard fail → litellm was attempted", len(l.calls) == 1)

    # [D] streaming, no override → Anthropic passthrough (async generator works)
    a, l = FakeAnthropic(), FakeLiteLLM()
    r = _router_with_fakes(a, l, override=None)
    evs = await collect(r.run_agent_stream(system_prompt="p", tools=[], messages=[], model="claude-haiku-4-5-20251001", user_id="u", agent_name="broker"))
    check("stream no override → Anthropic", evs and evs[0]["data"]["text"] == "anthropic:claude-haiku-4-5-20251001")

    # [C] streaming override → LiteLLM
    a, l = FakeAnthropic(), FakeLiteLLM()
    r = _router_with_fakes(a, l, override="openrouter/z-ai/glm-4.6")
    evs = await collect(r.run_agent_stream(system_prompt="p", tools=[], messages=[], model="claude-haiku-4-5-20251001", user_id="u", agent_name="broker"))
    check("stream override → LiteLLM", evs and evs[0]["data"]["text"] == "litellm:openrouter/z-ai/glm-4.6")

    # [E] streaming pre-content hard failure → Anthropic fallback
    a, l = FakeAnthropic(), FakeLiteLLM(fail=True, fail_after_content=False)
    r = _router_with_fakes(a, l, override="openrouter/z-ai/glm-4.6")
    evs = await collect(r.run_agent_stream(system_prompt="p", tools=[], messages=[], model="claude-haiku-4-5-20251001", user_id="u", agent_name="broker"))
    check("stream pre-content fail → Anthropic fallback", evs and evs[0]["data"]["text"] == "anthropic:claude-haiku-4-5-20251001")

    # [E] streaming mid-content hard failure → error event, NO fallback (no dup text)
    a, l = FakeAnthropic(), FakeLiteLLM(fail=True, fail_after_content=True)
    r = _router_with_fakes(a, l, override="openrouter/z-ai/glm-4.6")
    evs = await collect(r.run_agent_stream(system_prompt="p", tools=[], messages=[], model="claude-haiku-4-5-20251001", user_id="u", agent_name="broker"))
    check("stream mid-fail → litellm content first", evs[0]["data"]["text"] == "litellm:openrouter/z-ai/glm-4.6")
    check("stream mid-fail → error event, no Anthropic dup", evs[-1]["event"] == "error" and a.stream_calls == [])

    # [F] classify always Anthropic, even with an override set for "supervisor"
    a, l = FakeAnthropic(), FakeLiteLLM()
    r = ModelRouter(tool_executor="EXEC")
    r._anthropic = a
    r._litellm = l
    config.settings.OPENROUTER_API_KEY = "test-or-key"
    import db.redis.brand as brand
    brand.get_model_override = lambda agent, bh=None: "openrouter/z-ai/glm-4.6"  # try to override everything
    res = await r.classify(system_prompt="s", messages=[], model="claude-haiku-4-5-20251001")
    check("classify → Anthropic only", res == {"agent": "broker", "skills": []} and len(a.classify_calls) == 1)
    check("classify → never routed to LiteLLM", l.calls == [] and l.stream_calls == [])


def main():
    test_parse()
    test_format()
    asyncio.run(test_router())
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
