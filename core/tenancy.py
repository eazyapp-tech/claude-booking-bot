"""
core/tenancy.py — Server-authoritative multi-tenant identity for the web channel.

The web channel must NEVER trust client-supplied brand identity. The public
`GET /brand-config?token` endpoint hands a brand's pg_ids (and formerly its
brand_hash) to anyone holding the link, so trusting a client-sent
brand_hash / pg_ids would let any visitor act as another brand: pollute its
analytics + active-user lists, write into its conversation/lead records, and
read another brand's properties + KB — all with zero guessing.

Trust model: possession of a valid link token (or, for tokenless demo traffic,
the server-configured default brand) is the ONLY thing that authorizes acting
as a brand. `brand_hash` and `pg_ids` are always derived here from the verified
server-side config — never read from the request body.

The WhatsApp channel does NOT use this module: it derives the brand server-side
from the Meta `phone_number_id` reverse-lookup, which is already trusted.
"""

from db.redis_store import get_brand_by_token, get_default_brand_config


def resolve_web_brand(brand_token: str | None, account_values: dict | None = None):
    """Resolve the brand for a web request from its link token alone.

    Returns ``(brand_hash, pg_ids, safe_account_values)``.
    When no brand can be resolved (invalid token AND no default brand seeded),
    returns ``("", [], {})`` and the caller should treat the request as
    brand-less rather than fall back to any client-supplied identity.

    `account_values` is accepted only as a secondary source for the token
    itself (legacy clients embed it as ``account_values["token"]``); any
    `brand_hash` / `pg_ids` it carries are intentionally ignored.
    """
    token = (brand_token or "").strip()
    if not token and account_values:
        token = str(account_values.get("token") or "").strip()

    config = get_brand_by_token(token) if token else get_default_brand_config()
    if not config:
        return "", [], {}

    brand_hash = config.get("brand_hash", "")
    pg_ids = config.get("pg_ids", []) or []
    safe_account_values = {
        "brand_name": config.get("brand_name", ""),
        "cities": config.get("cities", ""),
        "areas": config.get("areas", ""),
        "pg_ids": pg_ids,
        "brand_hash": brand_hash,
    }
    return brand_hash, pg_ids, safe_account_values
