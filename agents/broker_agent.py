"""
Broker agent: handles property search, recommendations, details, images, shortlisting.
Uses Haiku (cost-optimized; switch back to SONNET_MODEL if quality drops).

Supports two modes (controlled by DYNAMIC_SKILLS_ENABLED):
- Dynamic skills: Loads only relevant prompt sections + filtered tools per turn
- Legacy: Full monolithic prompt + all broker tools (fallback)
"""

from config import settings
from core.claude import AnthropicEngine
from core.log import get_logger
from core.prompts import BROKER_AGENT_PROMPT, format_prompt, build_name_directive
from core.tool_executor import ToolExecutor
from db.redis_store import get_account_values, build_returning_user_context, get_property_id_for_search, get_user_name
from tools.registry import get_schemas_for_agent, get_handlers_for_agent
from utils.date import today_date, current_day

log = get_logger(__name__)


def get_config(user_id: str, language: str = "en", skills: list[str] | None = None) -> dict:
    """Return agent setup for use by both run() and streaming endpoint.

    When DYNAMIC_SKILLS_ENABLED:
      - Loads only relevant skill .md files + few-shot examples
      - Filters tools to match loaded skills
      - Returns system_prompt as list[str] for split caching

    When disabled (fallback):
      - Uses monolithic BROKER_AGENT_PROMPT (identical to pre-feature behavior)
      - Loads all broker tools
    """
    account = get_account_values(user_id)
    returning_ctx = build_returning_user_context(user_id)
    # Personalization: appended (uncached) so the cached _base.md prefix is untouched.
    name_directive = build_name_directive(get_user_name(user_id))

    # Resolve per-brand feature flags
    from db.redis_store import get_user_brand, get_effective_flags
    brand_hash = get_user_brand(user_id)
    flags = get_effective_flags(brand_hash)

    template_vars = dict(
        language=language,
        brand_name=account.get("brand_name", "our platform"),
        cities=account.get("cities", ""),
        areas=account.get("areas", ""),
        today_date=today_date(),
        current_day=current_day(),
        returning_user_context=returning_ctx,
        payment_required=flags.get("PAYMENT_REQUIRED"),
        kyc_enabled=flags.get("KYC_ENABLED"),
    )

    # ── Legacy path: monolithic prompt (feature flag OFF) ──────────────
    if not flags.get("DYNAMIC_SKILLS_ENABLED", settings.DYNAMIC_SKILLS_ENABLED):
        system_prompt = format_prompt(BROKER_AGENT_PROMPT, **template_vars) + name_directive
        tools = get_schemas_for_agent("broker")
        executor = ToolExecutor()
        executor.register_many(get_handlers_for_agent("broker"))
        return {
            "system_prompt": system_prompt,
            "tools": tools,
            "model": settings.HAIKU_MODEL,
            "executor": executor,
            "prop_ids": get_property_id_for_search(user_id),
            "semantic_kb_enabled": flags.get("SEMANTIC_KB_ENABLED", settings.SEMANTIC_KB_ENABLED),
        }

    # ── Dynamic skill path ─────────────────────────────────────────────
    from skills.loader import build_skill_prompt
    from skills.skill_map import get_tools_for_skills, ALWAYS_SKILLS
    from tools.registry import get_schemas_by_names, get_handlers_by_names

    is_returning = bool(returning_ctx)

    # Determine skills (fallback if supervisor didn't provide any)
    if not skills:
        skills = ["search", "qualify_returning" if is_returning else "qualify_new"]

    # Auto-add qualifying when search is present but no qualify skill
    if "search" in skills and not any(s.startswith("qualify") for s in skills):
        skills.insert(0, "qualify_returning" if is_returning else "qualify_new")

    # Selectively add selling guidance for detail/compare/objection turns
    if any(s in skills for s in ("details", "compare")) and "selling" not in skills:
        skills.append("selling")

    # Add always-on skills (currently empty — kept for future use)
    for s in ALWAYS_SKILLS:
        if s not in skills:
            skills.append(s)

    log.info("user=%s skills=%s", user_id, skills)

    # Build two-block prompt: base (cached) + dynamic skills (NOT cached)
    base_prompt, skill_prompt, doc_categories = build_skill_prompt("broker", skills, **template_vars)
    # Append the name directive to the UNCACHED skill block (keeps base_prompt cacheable).
    skill_prompt = skill_prompt + name_directive

    # Filter tools to match loaded skills
    tool_names = get_tools_for_skills(skills)
    tools = get_schemas_by_names(tool_names)

    executor = ToolExecutor()
    executor.register_many(get_handlers_by_names(tool_names))
    # Set fallback to all broker tools for graceful expansion on skill misses
    executor.set_fallback(get_handlers_for_agent("broker"))

    return {
        "system_prompt": [base_prompt, skill_prompt],  # Two blocks for split caching
        "tools": tools,
        "model": settings.HAIKU_MODEL,
        "executor": executor,
        "skills": skills,
        "prop_ids": get_property_id_for_search(user_id),
        "doc_categories": doc_categories,
        "semantic_kb_enabled": flags.get("SEMANTIC_KB_ENABLED", settings.SEMANTIC_KB_ENABLED),
    }


async def _inject_doc_context(cfg: dict, user_message: str = "") -> None:
    """Fetch relevant property docs and append to system prompt.

    Uses a 3-tier fallback chain:
      1. Semantic search (embed user query → cosine similarity vs doc embeddings, filtered by skill categories)
      2. Category-filtered text dump (if embeddings unavailable)
      3. Full text dump of all docs (legacy — if categories unavailable)

    Best-effort: silently skips if DB unavailable or no docs found.
    Pops 'prop_ids' and 'doc_categories' from cfg so callers don't pass them to the API.
    """
    prop_ids = cfg.pop("prop_ids", [])
    doc_categories = cfg.pop("doc_categories", [])
    semantic_kb = cfg.pop("semantic_kb_enabled", False)
    if not prop_ids:
        return

    try:
        from db import postgres as pg
        from utils.property_docs import format_property_docs

        docs = None
        top_ids = prop_ids[:3]

        # Tier 1: Semantic search (requires feature flag + user message + embeddings)
        if semantic_kb and user_message and doc_categories:
            try:
                from utils.embeddings import embed_query
                query_vec = await embed_query(user_message)
                if query_vec:
                    docs = await pg.search_relevant_docs(
                        query_embedding=query_vec,
                        property_ids=top_ids,
                        categories=doc_categories,
                        limit=5,
                    )
                    if docs:
                        log.debug("Semantic KB: %d docs retrieved for query", len(docs))
            except Exception as e:
                log.debug("Semantic search fallback: %s", e)
                docs = None

        # Tier 2: Category-filtered dump (if semantic search didn't produce results)
        if not docs and semantic_kb and doc_categories:
            try:
                docs = await pg.get_docs_by_category(
                    property_ids=top_ids,
                    categories=doc_categories,
                    limit=10,
                )
                if docs:
                    log.debug("Category KB: %d docs retrieved", len(docs))
            except Exception:
                docs = None

        # Tier 3: Full dump (legacy fallback)
        if not docs:
            docs = await pg.get_property_documents_text(top_ids)

        if docs:
            doc_ctx = format_property_docs(docs)
            sp = cfg["system_prompt"]
            if isinstance(sp, list):
                tail = (sp[1] + "\n\n" + doc_ctx) if len(sp) > 1 else doc_ctx
                cfg["system_prompt"] = [sp[0], tail]
            else:
                cfg["system_prompt"] = sp + "\n\n" + doc_ctx
    except Exception:
        pass  # intentional: KB injection is best-effort


async def run(
    engine: AnthropicEngine,
    messages: list[dict],
    user_id: str,
    language: str = "en",
    skills: list[str] | None = None,
) -> str:
    cfg = get_config(user_id, language=language, skills=skills)
    # Extract last user message for semantic KB retrieval
    last_user_msg = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user_msg = m.get("content", "") if isinstance(m.get("content"), str) else ""
            break
    await _inject_doc_context(cfg, user_message=last_user_msg)

    original_executor = engine.tool_executor
    engine.tool_executor = cfg["executor"]

    try:
        response = await engine.run_agent(
            system_prompt=cfg["system_prompt"],
            tools=cfg["tools"],
            messages=messages,
            model=cfg["model"],
            user_id=user_id,
            agent_name="broker",
        )
    finally:
        engine.tool_executor = original_executor

    return response
