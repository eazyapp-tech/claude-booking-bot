"""
test_cost_accounting.py — cache-aware cost accounting regression test.

Proves Wave 1 cheap win #1: prompt-cache tokens (cache_read at 0.1x,
cache_creation at 1.25x the base input rate) are counted in both token totals
and USD cost. Previously only `usage.input_tokens` (the uncached delta) was
billed, under-reporting spend on this cache-heavy app.

Deterministic: no Redis, no network, no LLM. Run: `python test_cost_accounting.py`.
"""

import inspect
import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

from core.claude import _usage_cost  # noqa: E402
from db.redis.admin import increment_session_cost  # noqa: E402

_passed = 0
_failed = 0


def check(name, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


class _Usage:
    """Minimal stand-in for anthropic.types.Usage."""
    def __init__(self, input_tokens, output_tokens,
                 cache_creation_input_tokens=None, cache_read_input_tokens=None):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        if cache_creation_input_tokens is not None:
            self.cache_creation_input_tokens = cache_creation_input_tokens
        if cache_read_input_tokens is not None:
            self.cache_read_input_tokens = cache_read_input_tokens


# Haiku rates (per million tokens)
RATES = {"in": 0.80, "out": 4.00}

print("Cache-aware cost accounting\n")

# ── 1. Full usage with cache writes + reads ──────────────────────────────────
u = _Usage(input_tokens=100, output_tokens=200,
           cache_creation_input_tokens=1000, cache_read_input_tokens=5000)
tin, tout, cost = _usage_cost(u, RATES)
# total input includes all three input buckets
check("total input sums all input buckets", tin == 6100, f"got {tin}")
check("output passed through", tout == 200, f"got {tout}")
# 100*.80 + 1000*.80*1.25 + 5000*.80*0.10 + 200*4.00 = 80+1000+400+800 = 2280 / 1e6
expected = 2280 / 1_000_000
check("cost includes cache write 1.25x + cache read 0.1x",
      abs(cost - expected) < 1e-12, f"got {cost}, want {expected}")

# ── 2. No cache attributes present → defaults to 0, never errors ─────────────
u2 = _Usage(input_tokens=100, output_tokens=200)
tin2, tout2, cost2 = _usage_cost(u2, RATES)
check("missing cache attrs → input is base only", tin2 == 100, f"got {tin2}")
expected2 = (100 * 0.80 + 200 * 4.00) / 1_000_000
check("missing cache attrs → cost is base only",
      abs(cost2 - expected2) < 1e-12, f"got {cost2}, want {expected2}")

# ── 3. Cache fields explicitly None → `or 0` guard handles it ────────────────
u3 = _Usage(input_tokens=100, output_tokens=200,
            cache_creation_input_tokens=None, cache_read_input_tokens=None)
tin3, _, cost3 = _usage_cost(u3, RATES)
check("None cache fields treated as 0 (tokens)", tin3 == 100, f"got {tin3}")
check("None cache fields treated as 0 (cost)",
      abs(cost3 - expected2) < 1e-12, f"got {cost3}")

# ── 4. Cache reads are cheaper than the same tokens billed uncached ──────────
cached = _usage_cost(_Usage(0, 0, cache_read_input_tokens=10000), RATES)[2]
uncached = _usage_cost(_Usage(10000, 0), RATES)[2]
check("cache read costs ~0.1x of uncached input",
      abs(cached - uncached * 0.10) < 1e-12, f"cached={cached} uncached={uncached}")

# ── 5. Old code (input_tokens only) would have under-reported ────────────────
old_cost = (u.input_tokens * RATES["in"] + u.output_tokens * RATES["out"]) / 1_000_000
check("new cost strictly exceeds the old cache-blind cost",
      cost > old_cost, f"new={cost} old={old_cost}")
old_tokens = u.input_tokens
check("new token total strictly exceeds old cache-blind total",
      tin > old_tokens, f"new={tin} old={old_tokens}")

# ── 6. increment_session_cost reconciled to accept precomputed cost ──────────
params = list(inspect.signature(increment_session_cost).parameters)
check("increment_session_cost takes a precomputed cost_usd (no model recompute)",
      params == ["uid", "tokens_in", "tokens_out", "cost_usd"], f"got {params}")

# ── Summary ──────────────────────────────────────────────────────────────────
print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
