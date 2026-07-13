-- Migration 015: created_at on technical_docs — temporal provenance for reranker recency
--
-- technical_docs never carried a creation timestamp — only the monotonic SERIAL id,
-- which is creation ORDER, not time. The reranker needs time MAGNITUDE for a recency-
-- decay blend (id-gaps are not uniform in time: a 100-id span may be a minute or a
-- week). We add a SERVER-stamped created_at (Postgres now() at insert — a remote
-- client's skewed clock never enters). id stays for stable ordering / tie-break.
--
-- Backfill: existing rows stored no creation time; recover it from the durable
-- neo4j_outbox.created_at (earliest outbox row per pg_id) where that row still exists
-- (un-consolidated rows). Consolidated rows whose outbox row was already deleted, and
-- legacy rows, stay NULL — creation time is genuinely unknown, and the reranker treats
-- NULL as "old / no recency boost". Future rows are stamped by the DEFAULT.
--
-- IDEMPOTENT: apply.py re-runs the whole chain — ADD COLUMN IF NOT EXISTS, the backfill
-- only fills NULLs, SET DEFAULT and CREATE INDEX IF NOT EXISTS are no-ops once present.

ALTER TABLE technical_docs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;

UPDATE technical_docs td
   SET created_at = ob.first_seen
  FROM (SELECT pg_id, min(created_at) AS first_seen FROM neo4j_outbox GROUP BY pg_id) ob
 WHERE td.id = ob.pg_id
   AND td.created_at IS NULL;

ALTER TABLE technical_docs ALTER COLUMN created_at SET DEFAULT now();

CREATE INDEX IF NOT EXISTS technical_docs_created_at_idx ON technical_docs (created_at);
