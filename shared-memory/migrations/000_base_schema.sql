-- Migration 000: Base schema (vector extension + Tier 1 / Tier 3 tables)
--
-- This is the ORIGINAL schema that migrations 001+ evolve from. It exists so
-- that `apply.py` (which runs every *.sql in order) can take a brand-new,
-- empty `agent_data` database all the way to the latest schema in ONE command.
--
-- Only the original columns are created here:
--   technical_docs       id, content, metadata, embedding, content_hash
--   community_summaries  id, content, metadata, embedding
-- Later migrations add the rest:
--   001 → agent_id / scope / visibility + neo4j_outbox
--   003 → community_summaries.source_pg_ids
--   004 → community_summaries.summary_history
--   006 → community_summaries.superseded
--
-- Idempotent (IF NOT EXISTS throughout): safe to re-run, and a no-op on any
-- database that already has these tables (existing installs are unaffected).

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

-- ─── Tier 1: episodic facts from all agents ───────────────────────────────────
CREATE TABLE IF NOT EXISTS technical_docs (
    id            SERIAL PRIMARY KEY,
    content       TEXT NOT NULL,
    metadata      JSONB,
    embedding     vector(1024),
    content_hash  TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS technical_docs_embedding_idx
    ON technical_docs USING ivfflat (embedding vector_cosine_ops);

-- ─── Tier 3: consolidated thematic narratives ─────────────────────────────────
CREATE TABLE IF NOT EXISTS community_summaries (
    id        SERIAL PRIMARY KEY,
    content   TEXT NOT NULL,
    metadata  JSONB,
    embedding vector(1024)
);
CREATE INDEX IF NOT EXISTS community_summaries_embedding_idx
    ON community_summaries USING ivfflat (embedding vector_cosine_ops);

COMMIT;
