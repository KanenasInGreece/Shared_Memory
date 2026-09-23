-- Migration 005: 003 backfilled metadata->'source_pg_ids', but the writer has
-- always stored metadata->'source_ids', so that backfill matched nothing.
-- New rows are filled at insert; this only repairs history while source_pg_ids is NULL.

BEGIN;

UPDATE community_summaries
SET source_pg_ids = ARRAY(
    SELECT (jsonb_array_elements_text(metadata->'source_ids'))::integer
)
WHERE source_pg_ids IS NULL
  AND metadata ? 'source_ids'
  AND jsonb_typeof(metadata->'source_ids') = 'array';

COMMIT;
