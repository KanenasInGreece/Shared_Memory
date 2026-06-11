-- schema_init.sql — full schema for a fresh install.
--
-- AUTO-GENERATED from the migration chain — do NOT edit by hand. The
-- generator applies every NNN_*.sql migration to a throwaway database and
-- introspects the result, so this file is equivalent to running apply.py
-- on an empty database by construction.
--
-- USE THIS for new installs: creates the complete schema in one shot.
-- Idempotent (IF NOT EXISTS throughout).
--
-- Upgrading an existing install? Use apply.py — it only runs pending migrations.
--
-- Regenerate after every new migration:
--   uv run --with psycopg2-binary python shared-memory/migrations/generate_schema_init.py
--
-- EMBEDDING DIMENSION: vector columns default to 1024-dim for BGE-M3. To use
-- a different model, change vector(1024) in 000_base_schema.sql, then
-- regenerate. The invariant is that ALL agents share ONE model via the
-- gateway — not the specific dimension.
--
-- Also run neo4j_init.cypher to initialise the Neo4j constraint set.
--
-- Usage:
--   psql -U postgres agent_data < shared-memory/migrations/schema_init.sql

BEGIN;

-- ─── Extensions ────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS vector;

-- ─── community_summaries ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS community_summaries (
    id               SERIAL PRIMARY KEY,
    content          TEXT NOT NULL,
    metadata         JSONB,
    embedding        vector(1024),
    agent_id         TEXT NOT NULL DEFAULT 'legacy'::text,
    scope            TEXT NOT NULL DEFAULT 'global'::text,
    visibility       TEXT NOT NULL DEFAULT 'global'::text,
    source_pg_ids    INT4[],
    summary_history  JSONB NOT NULL DEFAULT '[]'::jsonb,
    superseded       BOOLEAN NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS community_summaries_active_idx ON public.community_summaries USING btree (id) WHERE (NOT superseded);
CREATE INDEX IF NOT EXISTS community_summaries_agent_id_idx ON public.community_summaries USING btree (agent_id);
CREATE INDEX IF NOT EXISTS community_summaries_embedding_idx ON public.community_summaries USING hnsw (embedding vector_cosine_ops);
CREATE UNIQUE INDEX IF NOT EXISTS community_summaries_entity_domain_unique ON public.community_summaries USING btree (((metadata ->> 'entity'::text)), ((metadata ->> 'domain'::text))) WHERE (COALESCE((metadata ->> 'kind'::text), 'thematic'::text) <> 'insight'::text);
CREATE INDEX IF NOT EXISTS community_summaries_scope_idx ON public.community_summaries USING btree (scope);
CREATE INDEX IF NOT EXISTS community_summaries_visibility_idx ON public.community_summaries USING btree (visibility);

-- ─── neo4j_outbox ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS neo4j_outbox (
    id               BIGSERIAL PRIMARY KEY,
    pg_id            BIGINT NOT NULL,
    cypher_params    JSONB NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'::text,
    retries          INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ DEFAULT now(),
    applied_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS neo4j_outbox_pending_id_idx ON public.neo4j_outbox USING btree (id) WHERE (status = 'pending'::text);
CREATE INDEX IF NOT EXISTS neo4j_outbox_pending_idx ON public.neo4j_outbox USING btree (status) WHERE (status = 'pending'::text);

-- ─── technical_docs ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS technical_docs (
    id               SERIAL PRIMARY KEY,
    content          TEXT NOT NULL,
    metadata         JSONB,
    embedding        vector(1024),
    content_hash     TEXT,
    agent_id         TEXT NOT NULL DEFAULT 'legacy'::text,
    scope            TEXT NOT NULL DEFAULT 'global'::text,
    visibility       TEXT NOT NULL DEFAULT 'global'::text,
    superseded       BOOLEAN NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS technical_docs_agent_id_idx ON public.technical_docs USING btree (agent_id);
CREATE UNIQUE INDEX IF NOT EXISTS technical_docs_content_hash_key ON public.technical_docs USING btree (content_hash);
CREATE INDEX IF NOT EXISTS technical_docs_embedding_idx ON public.technical_docs USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS technical_docs_scope_idx ON public.technical_docs USING btree (scope);
CREATE INDEX IF NOT EXISTS technical_docs_visibility_idx ON public.technical_docs USING btree (visibility);

COMMIT;
