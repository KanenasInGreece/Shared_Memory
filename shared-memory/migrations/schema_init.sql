-- schema_init.sql — full schema for a fresh install (v0.4.7)
--
-- USE THIS for new installs. It creates the complete, final schema in one shot
-- without replaying the incremental migration chain (000–009). The result is
-- identical to running `apply.py` on an empty database.
--
-- Upgrading an existing install? Use apply.py instead — it applies only the
-- migrations not yet on disk and is idempotent on already-applied ones.
--
-- Usage:
--   psql -U postgres agent_data < shared-memory/migrations/schema_init.sql
-- or:
--   uv run --with psycopg2-binary python -c "
--     import psycopg2, pathlib
--     conn = psycopg2.connect('postgresql://postgres:<pw>@localhost:5432/agent_data')
--     conn.autocommit = True
--     conn.cursor().execute(pathlib.Path('shared-memory/migrations/schema_init.sql').read_text())
--   "
--
-- Idempotent: every statement uses IF NOT EXISTS / ON CONFLICT DO NOTHING —
-- safe to run on a database that is already partially or fully set up.

BEGIN;

-- ─── Extension ────────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS vector;

-- ─── Tier 1: episodic facts ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS technical_docs (
    id           SERIAL      PRIMARY KEY,
    content      TEXT        NOT NULL,
    metadata     JSONB,
    embedding    vector(1024),
    content_hash TEXT        UNIQUE,
    agent_id     TEXT        NOT NULL DEFAULT 'legacy',
    scope        TEXT        NOT NULL DEFAULT 'global',
    visibility   TEXT        NOT NULL DEFAULT 'global',
    superseded   BOOLEAN     NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS technical_docs_embedding_idx
    ON technical_docs USING ivfflat (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS technical_docs_agent_id_idx
    ON technical_docs (agent_id);
CREATE INDEX IF NOT EXISTS technical_docs_scope_idx
    ON technical_docs (scope);
CREATE INDEX IF NOT EXISTS technical_docs_visibility_idx
    ON technical_docs (visibility);

-- ─── Tier 3: consolidated thematic + insight narratives ───────────────────────
CREATE TABLE IF NOT EXISTS community_summaries (
    id              SERIAL      PRIMARY KEY,
    content         TEXT        NOT NULL,
    metadata        JSONB,
    embedding       vector(1024),
    agent_id        TEXT        NOT NULL DEFAULT 'legacy',
    scope           TEXT        NOT NULL DEFAULT 'global',
    visibility      TEXT        NOT NULL DEFAULT 'global',
    source_pg_ids   INTEGER[],
    summary_history JSONB       NOT NULL DEFAULT '[]'::jsonb,
    superseded      BOOLEAN     NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS community_summaries_embedding_idx
    ON community_summaries USING ivfflat (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS community_summaries_agent_id_idx
    ON community_summaries (agent_id);
CREATE INDEX IF NOT EXISTS community_summaries_scope_idx
    ON community_summaries (scope);
CREATE INDEX IF NOT EXISTS community_summaries_visibility_idx
    ON community_summaries (visibility);
-- Fast scan over non-superseded rows (retrieval always filters these out)
CREATE INDEX IF NOT EXISTS community_summaries_active_idx
    ON community_summaries (id)
    WHERE NOT superseded;
-- Thematic summaries: one per (entity, domain). Insight rows are always-INSERT
-- (kind='insight') — excluded from this unique constraint so supersession handles
-- their dedup without the resurrection trap.
CREATE UNIQUE INDEX IF NOT EXISTS community_summaries_entity_domain_unique
    ON community_summaries ((metadata->>'entity'), (metadata->>'domain'))
    WHERE COALESCE(metadata->>'kind', 'thematic') <> 'insight';

-- ─── Neo4j outbox (WAL for cross-DB atomicity) ────────────────────────────────
CREATE TABLE IF NOT EXISTS neo4j_outbox (
    id            BIGSERIAL   PRIMARY KEY,
    pg_id         BIGINT      NOT NULL,
    cypher_params JSONB       NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'pending',  -- pending|applied|rem_reviewed|consolidated|failed
    retries       INT         NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ          DEFAULT now(),
    applied_at    TIMESTAMPTZ
);

-- Worker drain: WHERE status='pending' ORDER BY id FOR UPDATE SKIP LOCKED
CREATE INDEX IF NOT EXISTS neo4j_outbox_pending_idx
    ON neo4j_outbox (status)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS neo4j_outbox_pending_id_idx
    ON neo4j_outbox (id)
    WHERE status = 'pending';

COMMIT;
