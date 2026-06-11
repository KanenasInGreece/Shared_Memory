-- Migration 010: switch embedding indexes from ivfflat to hnsw
--
-- Migration 000 created the two vector indexes as `ivfflat`. HNSW gives better
-- recall and query latency for this workload (no list-count tuning, graceful
-- with incremental inserts), at the cost of a slower build and more memory —
-- an acceptable trade at this corpus size. Production already runs hnsw (a
-- manual swap); this migration brings the chain — and therefore fresh installs
-- via schema_init.sql — in line, so every install converges on the same index.
--
-- IDEMPOTENT AND CHEAP TO RE-RUN. apply.py re-runs the whole chain on every
-- invocation, so this must be a true no-op once the index is already hnsw — a
-- bare DROP+CREATE would rebuild the vector index (exclusive lock, expensive)
-- on every run. The DO block rebuilds ONLY when the index is not yet hnsw.

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
