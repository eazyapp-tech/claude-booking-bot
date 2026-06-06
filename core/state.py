"""
core/state.py — Shared singleton state for the FastAPI app.

Engine and conversation manager are initialised in main.py's lifespan
and stored here so routers can import them without circular imports.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.model_router import ModelRouter
    from core.conversation import ConversationManager

# Populated by main.py lifespan on startup
engine: "ModelRouter | None" = None
conversation: "ConversationManager | None" = None
