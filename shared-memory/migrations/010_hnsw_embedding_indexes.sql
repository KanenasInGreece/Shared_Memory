-- Migration 010: embedding indexes move from ivfflat to hnsw so a fresh install
-- matches production. Rebuild only when the index is not already hnsw; dropping
-- and recreating it every time would take an exclusive lock.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public'
          AND indexname = 'technical_docs_embedding_idx'
          AND indexdef ILIKE '%USING hnsw%'
    ) THEN
        DROP INDEX IF EXISTS technical_docs_embedding_idx;
        CREATE INDEX technical_docs_embedding_idx
            ON technical_docs USING hnsw (embedding vector_cosine_ops);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public'
          AND indexname = 'community_summaries_embedding_idx'
          AND indexdef ILIKE '%USING hnsw%'
    ) THEN
        DROP INDEX IF EXISTS community_summaries_embedding_idx;
        CREATE INDEX community_summaries_embedding_idx
            ON community_summaries USING hnsw (embedding vector_cosine_ops);
    END IF;
END $$;

COMMIT;
