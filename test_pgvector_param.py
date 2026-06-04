"""
test_pgvector_param.py — Semantic-KB pgvector binding regression (UAT).

UAT runtime logs flooded with:
  `search_relevant_docs error: could not convert string to float: '[-0.021...]'`

Cause: _init_conn registered the pgvector asyncpg codec, which makes asyncpg
expect a Python list for `vector` params. But update_document_embedding and
search_relevant_docs bind a hand-built "[...]" STRING with an explicit
``::vector`` cast. The registered codec then tried to encode that string as a
vector → encode error → every semantic-KB query died (Tier-2/3 text fallback
masked it for users, but it was wasted Nomic calls + log flood + no ranking).

Fix: _init_conn no longer registers the codec; the param binds as text and is
cast server-side via ``::vector`` — dependency-free, works with or without the
pgvector Python package. The query functions are unchanged (they already build
the string + cast); this test locks the contract so the codec is not re-added.

Deterministic: source/contract inspection only, no DB/network.
Run: `python test_pgvector_param.py` (exit 0 = pass).
"""

import asyncio
import inspect
import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

import db.postgres as pg  # noqa: E402

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


init_src = inspect.getsource(pg._init_conn)
search_src = inspect.getsource(pg.search_relevant_docs)
update_src = inspect.getsource(pg.update_document_embedding)

print("[1] _init_conn does NOT register the pgvector codec")
check("1a no register_vector in _init_conn", "register_vector" not in init_src, init_src)
check("1b _init_conn(dummy) is a no-op (no real-conn calls)", asyncio.run(pg._init_conn(object())) is None)

print("\n[2] query functions keep the string + ::vector text-cast contract")
check("2a search_relevant_docs casts ::vector", "::vector" in search_src)
check("2b search builds a bracket string param", 'vec_str = "[" +' in search_src)
check("2c update_document_embedding casts ::vector", "::vector" in update_src)
check("2d update builds a bracket string param", 'vec_str = "[" +' in update_src)

print(f"\n{'='*50}\n  {_passed} passed, {_failed} failed\n{'='*50}")
sys.exit(0 if _failed == 0 else 1)
