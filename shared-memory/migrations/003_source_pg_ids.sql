-- Migration 003: source_pg_ids as its own column so provenance is an array
-- membership test instead of a JSONB scan. The backfill reads
-- metadata->'source_pg_ids'; migration 005 repairs rows stored under 'source_ids'.

BEGIN;

ALTER TABLE community_summaries
    ADD COLUMN IF NOT EXISTS source_pg_ids INTEGER[];

UPDATE community_summaries
SET source_pg_ids = ARRAY(
    SELECT (jsonb_array_elements_text(metadata->'source_pg_ids'))::integer
)
WHERE source_pg_ids IS NULL
  AND metadata ? 'source_pg_ids'
  AND jsonb_typeof(metadata->'source_pg_ids') = 'array';

COMMIT;
