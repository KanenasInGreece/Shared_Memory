-- Migration 003: Promote source_pg_ids to a first-class column
--
-- community_summaries previously stored contributing fact IDs only inside the
-- metadata JSONB blob, making provenance queries expensive and non-indexable.
-- This migration adds a dedicated integer array column so callers can do:
--
--   SELECT * FROM community_summaries WHERE %s = ANY(source_pg_ids)
--
-- Back-fill populates the column from existing metadata for rows written before
-- this migration ran.
--
-- Idempotent: safe to run multiple times (IF NOT EXISTS / no-op UPDATE throughout).

BEGIN;

ALTER TABLE community_summaries
    ADD COLUMN IF NOT EXISTS source_pg_ids INTEGER[];

-- Back-fill from metadata JSONB for rows already on disk.
UPDATE community_summaries
SET source_pg_ids = ARRAY(
    SELECT (jsonb_array_elements_text(metadata->'source_pg_ids'))::integer
)
WHERE source_pg_ids IS NULL
  AND metadata ? 'source_pg_ids'
  AND jsonb_typeof(metadata->'source_pg_ids') = 'array';

COMMIT;
