"""
Capture the name the user gives for themselves, for personalization.

WhatsApp users arrive with a Meta-profile name (set in routers/webhooks.py), but
web-chat users are anonymous — there's no name unless Tarini asks for it. This
tool is the web-channel analog: the broker calls it the moment the user shares a
name, and every conversational agent then addresses them by it (see
core.prompts.build_name_directive).
"""

from db.redis_store import set_user_name

TOOL_SCHEMA = {
    "name": "save_name",
    "description": (
        "Save the name the user gives for themselves so you can address them personally. "
        "Call this the moment the user shares their name (e.g. \"I'm Rahul\", \"call me Meera\"). "
        "Pass just the name they want to be called — not a sentence."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {
                "type": "string",
                "description": "The name the user wants to be called (e.g. 'Rahul').",
            },
        },
        "required": ["name"],
    },
}


def save_name(user_id: str, name: str, **kwargs) -> str:
    """Persist the user's name. Boundary guard only — never errors the flow."""
    cleaned = (name or "").strip()
    if not cleaned or len(cleaned) > 60:
        # Empty or implausibly long → ignore silently; don't derail the conversation.
        return "Got it."
    set_user_name(user_id, cleaned)
    first = cleaned.split()[0]
    return f"Lovely to meet you, {first}! I'll remember that."
