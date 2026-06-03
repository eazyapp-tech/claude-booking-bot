"""Canonical UI contract vocabulary. Vocabulary is DERIVED from contract.json,
the single source of truth shared byte-for-byte with eazypg-chat/src/contract.json.
This module owns the SHAPE (make_unit / is_valid_unit).

Every renderable unit is {kind, state, data, surface}. This is the wire contract
between backend egress and the frontend renderer registry.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_CANON = json.loads((Path(__file__).with_name("contract.json")).read_text(encoding="utf-8"))

KINDS = frozenset(_CANON["kinds"])

# NOTE: the field a renderer keys on is `kind`; `state` is a separate axis.
# `awaiting_input` is the STATE for a unit waiting on the user; `input_request`
# is the KIND of field shown. They are deliberately different strings so they
# never collide.
STATES = frozenset(_CANON["states"])

SURFACES = frozenset(_CANON["surfaces"])

# Sub-vocabularies carried inside `data`.
CAROUSEL_PAYLOADS = frozenset(_CANON["carousel_payloads"])
STATUS_VARIANTS = frozenset(_CANON["status_variants"])


def make_unit(kind: str, state: str, data: dict[str, Any] | None = None,
              surface: str = "inline") -> dict[str, Any]:
    return {"kind": kind, "state": state, "data": data or {}, "surface": surface}


def is_valid_unit(u: Any) -> bool:
    if not isinstance(u, dict):
        return False
    if u.get("kind") not in KINDS:
        return False
    if u.get("state") not in STATES:
        return False
    surface = u.get("surface", "inline")
    if surface not in SURFACES:
        return False
    if not isinstance(u.get("data"), dict):
        return False
    return True
