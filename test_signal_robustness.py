"""R5: outcome-signal loading degrades gracefully AND visibly — never silently blind.

The ranking blends per-property outcome signals (conversion / no-show). Today a
Redis hiccup or a bad import makes every signal lookup silently no-op, so search
ranks signal-blind and no log/telemetry ever says so. R5 keeps the graceful
degradation (search must never crash on a signal failure) but makes the failure
OBSERVABLE: a WARNING is emitted so a dark learning loop is visible, not swallowed.

Deterministic — no real Redis/network/LLM. We drive the real
`_load_property_signals` helper and patch the signal source.

Run: `python test_signal_robustness.py` (exit 0 = pass).
"""
import logging
import sys

import db.redis.analytics as analytics
import tools.broker.search as search

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def warned_about_signals(self):
        return any(
            r.levelno >= logging.WARNING and "signal" in r.getMessage().lower()
            for r in self.records
        )


def _with_capture():
    cap = _Capture()
    search.logger.addHandler(cap)
    return cap


def test_success_path_returns_signals():
    cap = _with_capture()
    orig = analytics.get_property_signals
    try:
        analytics.get_property_signals = lambda pid: {"converted": 2, "no_show": 0} if pid == "P1" else {}
        props = [{"p_property_id": "P1"}, {"p_property_id": "P2"}]
        out = search._load_property_signals(props)
        check("success: signals returned for P1", out.get("P1") == {"converted": 2, "no_show": 0})
        check("success: empty signal not stored for P2", "P2" not in out)
        check("success: no spurious warning", not cap.warned_about_signals())
    finally:
        analytics.get_property_signals = orig
        search.logger.removeHandler(cap)


def test_fetch_failure_is_graceful_and_visible():
    cap = _with_capture()
    orig = analytics.get_property_signals

    def boom(pid):
        raise RuntimeError("redis down")

    try:
        analytics.get_property_signals = boom
        props = [{"p_property_id": "P1"}, {"p_property_id": "P2"}]
        # Must NOT raise — search has to survive a signal outage.
        out = search._load_property_signals(props)
        check("fetch fail: returns a dict, no crash", isinstance(out, dict))
        check("fetch fail: ranking gets empty signals (degraded)", out == {})
        check("fetch fail: emits a WARNING (not silent)", cap.warned_about_signals())
    finally:
        analytics.get_property_signals = orig
        search.logger.removeHandler(cap)


def test_missing_pid_skipped_quietly():
    cap = _with_capture()
    orig = analytics.get_property_signals
    try:
        analytics.get_property_signals = lambda pid: {"converted": 1}
        props = [{}, {"property_id": ""}]  # no usable id
        out = search._load_property_signals(props)
        check("no-pid: nothing fetched", out == {})
        check("no-pid: not treated as a failure (no warning)", not cap.warned_about_signals())
    finally:
        analytics.get_property_signals = orig
        search.logger.removeHandler(cap)


if __name__ == "__main__":
    test_success_path_returns_signals()
    test_fetch_failure_is_graceful_and_visible()
    test_missing_pid_skipped_quietly()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(0 if _failed == 0 else 1)
