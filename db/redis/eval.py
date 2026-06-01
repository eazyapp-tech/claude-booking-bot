"""db/redis/eval.py — Eval run storage (CI stress-test results → Redis)."""

import json
from db.redis._base import _r

_LAST_RUN_KEY = "eval:last_run"
_HISTORY_KEY = "eval:history"
_HISTORY_MAX = 30
_LAST_RUN_TTL = 7 * 86400  # 7 days


def save_eval_run(run: dict) -> None:
    """Persist an eval run as the current last_run and append to capped history."""
    r = _r()
    payload = json.dumps(run)
    r.set(_LAST_RUN_KEY, payload, ex=_LAST_RUN_TTL)
    r.rpush(_HISTORY_KEY, payload)
    r.ltrim(_HISTORY_KEY, -_HISTORY_MAX, -1)
    r.expire(_HISTORY_KEY, _LAST_RUN_TTL)


def get_eval_last_run() -> dict | None:
    """Return the most recent eval run, or None if none recorded."""
    raw = _r().get(_LAST_RUN_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def get_eval_history(limit: int = 10) -> list[dict]:
    """Return up to `limit` most recent eval runs (oldest to newest)."""
    raw_list = _r().lrange(_HISTORY_KEY, -limit, -1)
    result = []
    for raw in raw_list:
        try:
            result.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            pass
    return result
