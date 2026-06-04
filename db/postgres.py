import json
from datetime import date, datetime
from typing import Optional

import asyncpg

from config import settings
from core.log import get_logger

logger = get_logger("db.postgres")

_pool: Optional[asyncpg.Pool] = None


_pgvector_available = False  # Set True after successful pgvector init


async def _init_conn(conn) -> None:
    """Per-connection init callback.

    We intentionally do NOT register the pgvector asyncpg codec. The codec makes
    asyncpg expect a Python list for `vector` params, but update_document_embedding
    and search_relevant_docs bind a hand-built "[...]" STRING with an explicit
    ``::vector`` cast. With the codec registered, asyncpg tried to encode that
    string as a vector and failed with `could not convert string to float` —
    every semantic-KB query died (UAT log flood). Letting the param bind as text
    and casting server-side via ``::vector`` is dependency-free and works whether
    or not the pgvector Python package is installed.
    """
    return None


async def init_pool() -> None:
    global _pool
    try:
        pool_kwargs = dict(min_size=2, max_size=10, init=_init_conn)
        if settings.DATABASE_URL:
            # Render / managed Postgres provides a URL
            _pool = await asyncpg.create_pool(dsn=settings.DATABASE_URL, **pool_kwargs)
        else:
            _pool = await asyncpg.create_pool(
                host=settings.DB_HOST,
                port=settings.DB_PORT,
                user=settings.DB_USER,
                password=settings.DB_PASSWORD,
                database=settings.DB_NAME,
                **pool_kwargs,
            )
        logger.info("Connection pool created")
    except Exception as e:
        logger.warning("Could not connect (non-critical, continuing without DB): %s", e)
        _pool = None


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def insert_message(
    thread_id: str,
    user_phone: str,
    message_text: str,
    message_sent_by: int,
    platform_type: str,
    is_template: bool = False,
    pg_ids: Optional[list] = None,
    brand_hash: Optional[str] = None,
) -> Optional[int]:
    if _pool is None:
        return None
    now = datetime.utcnow()
    try:
        row = await _pool.fetchrow(
            """
            INSERT INTO booking_messages
            (thread_id, user_phone, message_text, message_sent_by,
             created_at, updated_at, platform_type, is_template, pg_ids, brand_hash)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING id
            """,
            thread_id,
            user_phone,
            message_text,
            message_sent_by,
            now,
            now,
            platform_type,
            is_template,
            json.dumps(pg_ids) if pg_ids else None,
            brand_hash,
        )
        return row["id"] if row else None
    except Exception as e:
        logger.error("insert_message error: %s", e)
        return None


async def get_message_volume(start_date: str, end_date: str, brand_hash: Optional[str] = None) -> dict:
    """Return daily message counts: {"2026-02-20": 42, ...}.
    Brand-scoped if brand_hash provided.
    """
    if _pool is None:
        return {}
    try:
        from datetime import timedelta
        start = datetime.fromisoformat(start_date)
        end = datetime.fromisoformat(end_date) + timedelta(days=1)
        if brand_hash:
            rows = await _pool.fetch(
                """
                SELECT DATE(created_at) AS day, COUNT(*) AS cnt
                FROM booking_messages
                WHERE created_at >= $1
                  AND created_at < $2
                  AND brand_hash = $3
                GROUP BY DATE(created_at)
                ORDER BY day
                """,
                start,
                end,
                brand_hash,
            )
        else:
            rows = await _pool.fetch(
                """
                SELECT DATE(created_at) AS day, COUNT(*) AS cnt
                FROM booking_messages
                WHERE created_at >= $1
                  AND created_at < $2
                GROUP BY DATE(created_at)
                ORDER BY day
                """,
                start,
                end,
            )
        return {str(r["day"]): r["cnt"] for r in rows}
    except Exception as e:
        logger.error("get_message_volume error: %s", e)
        return {}


async def create_booking_messages_table() -> None:
    """Create booking_messages table if it doesn't exist (called on startup)."""
    if _pool is None:
        return
    try:
        await _pool.execute("""
            CREATE TABLE IF NOT EXISTS booking_messages (
                id               SERIAL PRIMARY KEY,
                thread_id        VARCHAR(255) NOT NULL,
                user_phone       VARCHAR(50),
                message_text     TEXT,
                message_sent_by  INT,
                created_at       TIMESTAMP    DEFAULT NOW(),
                updated_at       TIMESTAMP    DEFAULT NOW(),
                platform_type    VARCHAR(50),
                is_template      BOOLEAN      DEFAULT FALSE,
                pg_ids           TEXT,
                brand_hash       VARCHAR(16)
            );
            CREATE INDEX IF NOT EXISTS idx_booking_messages_thread_id
                ON booking_messages(thread_id);
            CREATE INDEX IF NOT EXISTS idx_booking_messages_brand_hash
                ON booking_messages(brand_hash);
            CREATE INDEX IF NOT EXISTS idx_booking_messages_created_at
                ON booking_messages(created_at);
        """)
    except Exception as e:
        logger.warning("create_booking_messages_table: %s", e)


async def add_brand_hash_columns() -> None:
    """Add brand_hash column to booking_messages and leads tables (idempotent migration)."""
    if _pool is None:
        return
    try:
        await _pool.execute("""
            ALTER TABLE booking_messages ADD COLUMN IF NOT EXISTS brand_hash VARCHAR(16);
            CREATE INDEX IF NOT EXISTS idx_booking_messages_brand_hash
                ON booking_messages(brand_hash);
        """)
    except Exception as e:
        logger.warning("add_brand_hash_columns (booking_messages): %s", e)
    try:
        await _pool.execute("""
            ALTER TABLE leads ADD COLUMN IF NOT EXISTS brand_hash VARCHAR(16);
            CREATE INDEX IF NOT EXISTS idx_leads_brand_hash ON leads(brand_hash);
        """)
    except Exception as e:
        logger.warning("add_brand_hash_columns (leads): %s", e)
    try:
        await _pool.execute("""
            ALTER TABLE leads ADD COLUMN IF NOT EXISTS lead_outcome TEXT;
            ALTER TABLE leads ADD COLUMN IF NOT EXISTS outcome_at TIMESTAMP;
            ALTER TABLE leads ADD COLUMN IF NOT EXISTS converted_property_id TEXT;
        """)
    except Exception as e:
        logger.warning("add_brand_hash_columns (leads outcome cols): %s", e)


async def create_property_documents_table() -> None:
    """Create property_documents table if it doesn't exist (called on startup)."""
    if _pool is None:
        return
    try:
        await _pool.execute("""
            CREATE TABLE IF NOT EXISTS property_documents (
                id          SERIAL PRIMARY KEY,
                property_id VARCHAR(100) NOT NULL,
                filename    VARCHAR(255) NOT NULL,
                file_type   VARCHAR(20)  NOT NULL,
                content_text TEXT        NOT NULL DEFAULT '',
                size_bytes  INT          NOT NULL,
                uploaded_at TIMESTAMP    DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_property_documents_property_id
                ON property_documents(property_id);
        """)
    except Exception as e:
        logger.warning("create_property_documents_table: %s", e)


async def insert_property_document(
    property_id: str,
    filename: str,
    file_type: str,
    content_text: str,
    size_bytes: int,
    category: Optional[str] = None,
) -> dict:
    """Insert a document and return its metadata."""
    if _pool is None:
        raise RuntimeError("Database not available")
    row = await _pool.fetchrow(
        """
        INSERT INTO property_documents (property_id, filename, file_type, content_text, size_bytes, category)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id, property_id, filename, file_type, size_bytes, category, uploaded_at
        """,
        property_id, filename, file_type, content_text, size_bytes, category,
    )
    return {
        "id":          row["id"],
        "property_id": row["property_id"],
        "filename":    row["filename"],
        "file_type":   row["file_type"],
        "size_bytes":  row["size_bytes"],
        "category":    row["category"],
        "uploaded_at": row["uploaded_at"].isoformat(),
    }


async def get_property_documents(property_id: str) -> list[dict]:
    """Return document metadata (no content_text) for a property."""
    if _pool is None:
        return []
    rows = await _pool.fetch(
        """
        SELECT id, property_id, filename, file_type, size_bytes, category, uploaded_at
        FROM property_documents
        WHERE property_id = $1
        ORDER BY uploaded_at DESC
        """,
        property_id,
    )
    return [
        {
            "id":          r["id"],
            "property_id": r["property_id"],
            "filename":    r["filename"],
            "file_type":   r["file_type"],
            "size_bytes":  r["size_bytes"],
            "category":    r["category"],
            "uploaded_at": r["uploaded_at"].isoformat(),
        }
        for r in rows
    ]


async def get_property_documents_text(property_ids: list[str], max_chars: int = 8000) -> list[dict]:
    """Return documents with content_text for KB injection (for broker agent)."""
    if _pool is None or not property_ids:
        return []
    rows = await _pool.fetch(
        """
        SELECT property_id, filename, content_text
        FROM property_documents
        WHERE property_id = ANY($1::varchar[])
        ORDER BY uploaded_at DESC
        LIMIT 10
        """,
        property_ids,
    )
    results = []
    total = 0
    for r in rows:
        text = (r["content_text"] or "")[:max_chars - total]
        if text:
            results.append({
                "property_id": r["property_id"],
                "filename":    r["filename"],
                "text":        text,
            })
            total += len(text)
            if total >= max_chars:
                break
    return results


async def create_leads_table() -> None:
    """Create the leads snapshot table (called on startup)."""
    if _pool is None:
        return
    try:
        await _pool.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                uid               TEXT PRIMARY KEY,
                name              TEXT,
                phone             TEXT,
                phone_collected   BOOLEAN   DEFAULT FALSE,
                persona           TEXT,
                stage             TEXT,
                first_seen        TEXT,
                last_seen         TEXT,
                session_count     INTEGER   DEFAULT 0,
                viewed_count      INTEGER   DEFAULT 0,
                shortlisted_count INTEGER   DEFAULT 0,
                visits_count      INTEGER   DEFAULT 0,
                deal_breakers     JSONB     DEFAULT '[]',
                must_haves        JSONB     DEFAULT '[]',
                lead_score        INTEGER   DEFAULT 0,
                location_pref     TEXT,
                budget_min        NUMERIC,
                budget_max        NUMERIC,
                budget            TEXT,
                property_type     TEXT,
                amenities         JSONB     DEFAULT '[]',
                sharing_types     JSONB     DEFAULT '[]',
                cost_usd          NUMERIC   DEFAULT 0,
                synced_at         TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_leads_stage     ON leads(stage);
            CREATE INDEX IF NOT EXISTS idx_leads_score     ON leads(lead_score DESC);
            CREATE INDEX IF NOT EXISTS idx_leads_last_seen ON leads(last_seen);
        """)
    except Exception as e:
        logger.warning("create_leads_table: %s", e)


async def upsert_leads(
    rows: list[dict],
    brand_hash: Optional[str] = None,
    lead_outcome: Optional[str] = None,
    outcome_at: Optional[datetime] = None,
    converted_property_id: Optional[str] = None,
) -> None:
    """Batch upsert enriched lead snapshots. Called fire-and-forget from admin endpoints.

    lead_outcome, outcome_at, converted_property_id are optional; when provided they
    override the stored value via COALESCE (non-null wins). When omitted the existing
    DB value is preserved.
    """
    if _pool is None or not rows:
        return
    try:
        await _pool.executemany(
            """
            INSERT INTO leads (
                uid, name, phone, phone_collected, persona, stage,
                first_seen, last_seen, session_count, viewed_count, shortlisted_count,
                visits_count, deal_breakers, must_haves, lead_score, location_pref,
                budget_min, budget_max, budget, property_type, amenities,
                sharing_types, cost_usd, brand_hash,
                lead_outcome, outcome_at, converted_property_id,
                synced_at
            )
            VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,NOW()
            )
            ON CONFLICT (uid) DO UPDATE SET
                name=EXCLUDED.name, phone=EXCLUDED.phone,
                phone_collected=EXCLUDED.phone_collected, persona=EXCLUDED.persona,
                stage=EXCLUDED.stage, first_seen=EXCLUDED.first_seen,
                last_seen=EXCLUDED.last_seen, session_count=EXCLUDED.session_count,
                viewed_count=EXCLUDED.viewed_count, shortlisted_count=EXCLUDED.shortlisted_count,
                visits_count=EXCLUDED.visits_count, deal_breakers=EXCLUDED.deal_breakers,
                must_haves=EXCLUDED.must_haves, lead_score=EXCLUDED.lead_score,
                location_pref=EXCLUDED.location_pref, budget_min=EXCLUDED.budget_min,
                budget_max=EXCLUDED.budget_max, budget=EXCLUDED.budget,
                property_type=EXCLUDED.property_type, amenities=EXCLUDED.amenities,
                sharing_types=EXCLUDED.sharing_types, cost_usd=EXCLUDED.cost_usd,
                brand_hash=EXCLUDED.brand_hash,
                lead_outcome=COALESCE(EXCLUDED.lead_outcome, leads.lead_outcome),
                outcome_at=COALESCE(EXCLUDED.outcome_at, leads.outcome_at),
                converted_property_id=COALESCE(EXCLUDED.converted_property_id, leads.converted_property_id),
                synced_at=NOW()
            """,
            [
                (
                    r["uid"], r["name"], r["phone"],
                    bool(r.get("phone_collected", False)),
                    r.get("persona") or "",
                    r.get("stage") or "",
                    r.get("first_seen") or "",
                    r.get("last_seen") or "",
                    int(r.get("session_count") or 0),
                    int(r.get("viewed_count") or 0),
                    int(r.get("shortlisted_count") or 0),
                    int(r.get("visits_count") or 0),
                    json.dumps(r.get("deal_breakers") or []),
                    json.dumps(r.get("must_haves") or []),
                    int(r.get("lead_score") or 0),
                    r.get("location_pref") or "",
                    r.get("budget_min"),
                    r.get("budget_max"),
                    r.get("budget") or "",
                    r.get("property_type") or "",
                    json.dumps(r.get("amenities") or []),
                    json.dumps(r.get("sharing_types") or []),
                    float(r.get("cost_usd") or 0.0),
                    brand_hash,
                    r.get("lead_outcome") or lead_outcome,
                    r.get("outcome_at") or outcome_at,
                    r.get("converted_property_id") or converted_property_id,
                )
                for r in rows
            ],
        )
    except Exception as e:
        logger.error("upsert_leads error: %s", e)


async def delete_property_document(property_id: str, doc_id: int) -> bool:
    """Delete a document. Returns True if a row was deleted."""
    if _pool is None:
        return False
    result = await _pool.execute(
        "DELETE FROM property_documents WHERE id = $1 AND property_id = $2",
        doc_id, property_id,
    )
    return result == "DELETE 1"


# ---------------------------------------------------------------------------
# pgvector — semantic KB retrieval
# ---------------------------------------------------------------------------

async def enable_pgvector() -> None:
    """Enable pgvector extension + add embedding columns (idempotent, called on startup).

    Sets _pgvector_available = True on success so the pool init callback
    registers the vector type on new connections.
    """
    global _pgvector_available
    if _pool is None:
        return
    # category column has no pgvector dependency — always add it
    try:
        await _pool.execute(
            "ALTER TABLE property_documents ADD COLUMN IF NOT EXISTS category VARCHAR(30);"
        )
    except Exception as e:
        logger.warning("category column migration: %s", e)
    # embedding column requires the pgvector extension
    try:
        await _pool.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await _pool.execute(
            "ALTER TABLE property_documents ADD COLUMN IF NOT EXISTS embedding vector(256);"
        )
        _pgvector_available = True
        logger.info("pgvector enabled, embedding column ready")
    except Exception as e:
        logger.warning("pgvector setup skipped (non-critical): %s", e)
        _pgvector_available = False


async def get_property_doc_counts(property_ids: list[str]) -> dict[str, int]:
    """Return {property_id: doc_count} for the given ids. Missing ids default to 0."""
    if _pool is None or not property_ids:
        return {}
    try:
        rows = await _pool.fetch(
            "SELECT property_id, COUNT(*)::int AS cnt"
            " FROM property_documents WHERE property_id = ANY($1::text[])"
            " GROUP BY property_id",
            property_ids,
        )
        return {r["property_id"]: r["cnt"] for r in rows}
    except Exception as e:
        logger.warning("get_property_doc_counts: %s", e)
        return {}


async def update_document_embedding(doc_id: int, embedding: list[float]) -> None:
    """Set the embedding vector for a document (called after background embed)."""
    if _pool is None or not _pgvector_available:
        return
    try:
        vec_str = "[" + ",".join(f"{v:.8f}" for v in embedding) + "]"
        await _pool.execute(
            "UPDATE property_documents SET embedding = $1::vector WHERE id = $2",
            vec_str, doc_id,
        )
    except Exception as e:
        logger.warning("update_document_embedding(%s): %s", doc_id, e)


async def search_relevant_docs(
    query_embedding: list[float],
    property_ids: list[str],
    categories: list[str],
    limit: int = 5,
) -> list[dict]:
    """Semantic search: find top-k docs by cosine similarity.

    Pre-filters by property_ids and categories, then ranks by embedding distance.
    Returns list of {property_id, filename, text} dicts (same format as get_property_documents_text).
    """
    if _pool is None or not _pgvector_available:
        return []
    if not property_ids or not categories or not query_embedding:
        return []
    try:
        vec_str = "[" + ",".join(f"{v:.8f}" for v in query_embedding) + "]"
        rows = await _pool.fetch(
            """
            SELECT property_id, filename, content_text
            FROM property_documents
            WHERE property_id = ANY($1::varchar[])
              AND category = ANY($2::varchar[])
              AND embedding IS NOT NULL
            ORDER BY embedding <=> $3::vector
            LIMIT $4
            """,
            property_ids, categories, vec_str, limit,
        )
        return [
            {"property_id": r["property_id"], "filename": r["filename"], "text": r["content_text"]}
            for r in rows
        ]
    except Exception as e:
        logger.warning("search_relevant_docs error: %s", e)
        return []


async def get_docs_by_category(
    property_ids: list[str],
    categories: list[str],
    limit: int = 10,
) -> list[dict]:
    """Category-filtered text dump (fallback when embeddings unavailable).

    Returns docs filtered by category but without similarity ranking.
    Same output format as get_property_documents_text.
    """
    if _pool is None or not property_ids or not categories:
        return []
    try:
        rows = await _pool.fetch(
            """
            SELECT property_id, filename, content_text
            FROM property_documents
            WHERE property_id = ANY($1::varchar[])
              AND category = ANY($2::varchar[])
            ORDER BY uploaded_at DESC
            LIMIT $3
            """,
            property_ids, categories, limit,
        )
        return [
            {"property_id": r["property_id"], "filename": r["filename"], "text": r["content_text"]}
            for r in rows
        ]
    except Exception as e:
        logger.warning("get_docs_by_category error: %s", e)
        return []


async def create_error_events_table() -> None:
    """Create error_events table for structured error tracking (called on startup)."""
    if _pool is None:
        return
    try:
        await _pool.execute("""
            CREATE TABLE IF NOT EXISTS error_events (
                id           SERIAL PRIMARY KEY,
                user_id      TEXT NOT NULL,
                brand_hash   VARCHAR(16),
                error_type   VARCHAR(50) NOT NULL,
                error_source VARCHAR(100) NOT NULL,
                error_message TEXT,
                context      JSONB DEFAULT '{}',
                created_at   TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_error_events_brand_date
                ON error_events(brand_hash, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_error_events_type
                ON error_events(error_type, created_at DESC);
        """)
    except Exception as e:
        logger.warning("create_error_events_table: %s", e)


async def insert_error_event(
    user_id: str,
    brand_hash: Optional[str],
    error_type: str,
    error_source: str,
    error_message: str = "",
    context: Optional[dict] = None,
) -> Optional[int]:
    """Insert an error event. Returns row id or None.

    error_type: tool_failure | api_timeout | empty_response | routing_override
    error_source: tool name, agent name, or component identifier
    """
    if _pool is None:
        return None
    try:
        row = await _pool.fetchrow(
            """
            INSERT INTO error_events
                (user_id, brand_hash, error_type, error_source, error_message, context)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            RETURNING id
            """,
            user_id,
            brand_hash,
            error_type,
            error_source,
            error_message or "",
            json.dumps(context or {}),
        )
        return row["id"] if row else None
    except Exception as e:
        logger.error("insert_error_event error: %s", e)
        return None


async def get_error_events(
    brand_hash: Optional[str] = None,
    error_type: Optional[str] = None,
    days: int = 7,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Return paginated error events, optionally filtered by brand_hash and error_type."""
    if _pool is None:
        return []
    try:
        conditions = ["created_at >= NOW() - ($1 || ' days')::INTERVAL"]
        params: list = [str(days)]
        idx = 2

        if brand_hash:
            conditions.append(f"brand_hash = ${idx}")
            params.append(brand_hash)
            idx += 1
        if error_type:
            conditions.append(f"error_type = ${idx}")
            params.append(error_type)
            idx += 1

        where = " AND ".join(conditions)
        params.append(limit)
        params.append(offset)

        rows = await _pool.fetch(
            f"""
            SELECT id, user_id, brand_hash, error_type, error_source,
                   error_message, context, created_at
            FROM error_events
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params,
        )
        return [
            {
                "id": r["id"],
                "user_id": r["user_id"],
                "brand_hash": r["brand_hash"],
                "error_type": r["error_type"],
                "error_source": r["error_source"],
                "error_message": r["error_message"],
                "context": json.loads(r["context"]) if r["context"] else {},
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("get_error_events error: %s", e)
        return []


async def get_error_summary(
    brand_hash: Optional[str] = None,
    days: int = 7,
) -> dict:
    """Return {error_type: count} aggregate for the last N days."""
    if _pool is None:
        return {}
    try:
        if brand_hash:
            rows = await _pool.fetch(
                """
                SELECT error_type, COUNT(*) AS cnt
                FROM error_events
                WHERE created_at >= NOW() - ($1 || ' days')::INTERVAL
                  AND brand_hash = $2
                GROUP BY error_type
                ORDER BY cnt DESC
                """,
                str(days),
                brand_hash,
            )
        else:
            rows = await _pool.fetch(
                """
                SELECT error_type, COUNT(*) AS cnt
                FROM error_events
                WHERE created_at >= NOW() - ($1 || ' days')::INTERVAL
                GROUP BY error_type
                ORDER BY cnt DESC
                """,
                str(days),
            )
        return {r["error_type"]: r["cnt"] for r in rows}
    except Exception as e:
        logger.error("get_error_summary error: %s", e)
        return {}


async def cleanup_old_error_events(days: int = 90) -> int:
    """Delete error events older than N days. Returns count deleted."""
    if _pool is None:
        return 0
    try:
        result = await _pool.execute(
            "DELETE FROM error_events WHERE created_at < NOW() - ($1 || ' days')::INTERVAL",
            str(days),
        )
        # result is like "DELETE 42"
        count = int(result.split()[-1]) if result else 0
        if count:
            logger.info("Cleaned up %d error events older than %d days", count, days)
        return count
    except Exception as e:
        logger.error("cleanup_old_error_events error: %s", e)
        return 0


async def get_messages(thread_id: str, limit: int = 50) -> list[dict]:
    if _pool is None:
        return []
    try:
        rows = await _pool.fetch(
            """
            SELECT id, thread_id, user_phone, message_text, message_sent_by,
                   platform_type, is_template, created_at
            FROM booking_messages
            WHERE thread_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            thread_id,
            limit,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("get_messages error: %s", e)
        return []
