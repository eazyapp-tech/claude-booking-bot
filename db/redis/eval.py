"""db/redis/eval.py — Eval run storage (CI stress-test results → Redis)."""

import json
from db.redis._base import _r

_HISTORY_MAX = 30
_LAST_RUN_TTL = 7 * 86400  # 7 days


def _keys(brand_hash: str) -> tuple[str, str]:
    return f"eval:{brand_hash}:last_run", f"eval:{brand_hash}:history"


def save_eval_run(brand_hash: str, run: dict) -> None:
    last_key, hist_key = _keys(brand_hash)
    r = _r()
    payload = json.dumps(run)
    r.set(last_key, payload, ex=_LAST_RUN_TTL)
    r.rpush(hist_key, payload)
    r.ltrim(hist_key, -_HISTORY_MAX, -1)
    r.expire(hist_key, _LAST_RUN_TTL)


def get_eval_last_run(brand_hash: str) -> dict | None:
    raw = _r().get(_keys(brand_hash)[0])
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def get_eval_history(brand_hash: str, limit: int = 10) -> list[dict]:
    raw_list = _r().lrange(_keys(brand_hash)[1], -limit, -1)
    result = []
    for raw in raw_list:
        try:
            result.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            pass
    return result
