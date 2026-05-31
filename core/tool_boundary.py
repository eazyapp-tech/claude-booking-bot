"""
core/tool_boundary.py — Pure, dependency-free tool-boundary helpers (Wave 3).

Two defense-in-depth concerns, both enforced at the single ToolExecutor seam:

  1. Idempotency — write-path tools whose execution has a real-world side effect
     (creating a booking / payment / lead in the Rentok CRM) get a burst-dedup
     key so the same call firing twice within a short window runs once. See
     db/redis/idempotency.py for the Redis lock + result-cache it keys.

  2. Input validation — a conservative JSON-Schema check of the model's tool
     arguments against the tool's declared input_schema, so malformed (or
     prompt-injected) calls are rejected with a clear, self-correcting message
     instead of blowing up inside the handler.

No Redis, network, or LLM here — everything is pure so it is trivially testable.
"""

import hashlib
import json

# Write-path tools where a duplicate execution has a real-world side effect.
# These get burst-dedup; read-only tools (search, details, landmarks, …) do not.
IDEMPOTENT_TOOLS = {
    "reserve_bed",
    "save_visit_time",
    "save_call_time",
    "create_payment_link",
    "verify_payment",
    "reschedule_booking",
    "cancel_booking",
}


def idempotency_key(user_id: str, tool_name: str, tool_input: dict) -> str:
    """Stable 16-char key for (user, tool, canonical args).

    Same user + same tool + same arguments → same key (so a re-fire dedups);
    different arguments (a genuinely new booking) → different key.
    """
    canonical = json.dumps(tool_input, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(f"{user_id}|{tool_name}|{canonical}".encode()).hexdigest()
    return digest[:16]


_TYPE_CHECKS = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def validate_tool_input(schema: dict | None, tool_input: dict) -> str | None:
    """Conservative JSON-Schema check for the subset our tools declare.

    Returns an error string on an *unambiguous* violation (wrong scalar type,
    missing required field, unexpected field when additionalProperties is
    false, value outside an enum), else None. Unknown schema constructs and
    null values pass through so a valid call is never blocked — this is a guard
    rail, not a full validator.
    """
    if not isinstance(schema, dict):
        return None
    if not isinstance(tool_input, dict):
        return f"expected an object of arguments, got {type(tool_input).__name__}"

    properties = schema.get("properties") or {}
    required = schema.get("required") or []

    missing = [f for f in required if f not in tool_input]
    if missing:
        return f"missing required field(s): {', '.join(missing)}"

    if schema.get("additionalProperties") is False and properties:
        extra = [k for k in tool_input if k not in properties]
        if extra:
            return f"unexpected field(s): {', '.join(extra)}"

    for field, spec in properties.items():
        if field not in tool_input or not isinstance(spec, dict):
            continue
        value = tool_input[field]
        if value is None:
            continue  # treat null as omitted — handlers default via kwargs

        expected = spec.get("type")
        py_type = _TYPE_CHECKS.get(expected) if isinstance(expected, str) else None
        if py_type is not None:
            # bool is a subclass of int — don't accept True where a number is asked
            if expected in ("integer", "number") and isinstance(value, bool):
                return f"field '{field}' should be {expected}, got boolean"
            if not isinstance(value, py_type):
                return f"field '{field}' should be {expected}, got {type(value).__name__}"

        enum = spec.get("enum")
        if isinstance(enum, list) and enum and value not in enum:
            return f"field '{field}' must be one of {enum}"

    return None
