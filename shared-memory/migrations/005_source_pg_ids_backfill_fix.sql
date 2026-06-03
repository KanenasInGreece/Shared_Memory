-- Migration 005: correct source_pg_ids backfill
--
-- Migration 003 expected metadata->'source_pg_ids' but consolidation_loop.py has always
-- written metadata->'source_ids' (without the pg_ prefix). Rows written before migration 003
-- landed with source_pg_ids IS NULL as a result — the backfill was a silent no-op.
--
-- consolidation_loop.py already populates source_pg_ids at INSERT time for all new rows,
-- so only the historical backfill is missing.
--
-- Idempotent: WHERE source_pg_ids IS NULL means re-running is safe.

BEGIN;

UPDATE community_summaries
SET source_pg_ids = ARRAY(
    SELECT (jsonb_array_elements_text(metadata->'source_ids'))::integer
)
WHERE source_pg_ids IS NULL
  AND metadata ? 'source_ids'
  AND jsonb_typeof(metadata->'source_ids') = 'array';

COMMIT;
