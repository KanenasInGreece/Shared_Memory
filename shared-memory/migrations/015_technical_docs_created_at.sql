-- Migration 015: server-stamped created_at so the reranker has a time magnitude.
-- The serial id is order, not duration. Backfill uses the earliest surviving
-- outbox row; a deleted outbox row leaves NULL, which means no recency boost.

ALTER TABLE technical_docs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;

UPDATE technical_docs td
   SET created_at = ob.first_seen
  FROM (SELECT pg_id, min(created_at) AS first_seen FROM neo4j_outbox GROUP BY pg_id) ob
 WHERE td.id = ob.pg_id
   AND td.created_at IS NULL;

ALTER TABLE technical_docs ALTER COLUMN created_at SET DEFAULT now();

CREATE INDEX IF NOT EXISTS technical_docs_created_at_idx ON technical_docs (created_at);
