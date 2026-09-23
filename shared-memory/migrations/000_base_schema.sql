-- Migration 000: the original technical_docs and community_summaries, so apply.py
-- can build an empty database. Later files add the rest. IF NOT EXISTS makes a
-- re-run a no-op on an install that already has these tables.

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

-- Tier 1 episodic facts. The ivfflat index is replaced in migration 010.
CREATE TABLE IF NOT EXISTS technical_docs (
    id            SERIAL PRIMARY KEY,
    content       TEXT NOT NULL,
    metadata      JSONB,
    embedding     vector(1024),
    content_hash  TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS technical_docs_embedding_idx
    ON technical_docs USING ivfflat (embedding vector_cosine_ops);

-- Tier 3 thematic narratives.
CREATE TABLE IF NOT EXISTS community_summaries (
    id        SERIAL PRIMARY KEY,
    content   TEXT NOT NULL,
    metadata  JSONB,
    embedding vector(1024)
);
CREATE INDEX IF NOT EXISTS community_summaries_embedding_idx
    ON community_summaries USING ivfflat (embedding vector_cosine_ops);

COMMIT;
