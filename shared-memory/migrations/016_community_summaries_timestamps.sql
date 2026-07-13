-- Migration 016: created_at + updated_at on community_summaries — universal temporal
-- provenance / latency instrumentation (decision: universal timestamps as latency
-- instrumentation, not just recency)
--
-- community_summaries was the ONLY authoritative record store without a time column.
--   created_at — first fold (immutable provenance).
--   updated_at — last fold (the recency + staleness signal). Thematic summaries mutate
--                in place (ON CONFLICT DO UPDATE), so the daemon stamps updated_at =
--                now() on each fold while created_at is preserved (not in the SET).
--
-- This ALSO unlocks the coarse dream-cycle latency purely in Postgres, no Neo4j:
--   summary.created_at − min(source technical_docs.created_at over source_pg_ids)
--   = the fact→summary end-to-end time.
--
-- Backfill: recover both from the ISO 'timestamp' the daemon already writes into
-- metadata per fold (regex-guarded so a malformed value can't abort the migration);
-- rows without a parseable timestamp stay NULL (unknown). Future rows get DEFAULT now().
-- IDEMPOTENT: ADD COLUMN IF NOT EXISTS; backfill only fills NULLs; SET DEFAULT / CREATE
-- INDEX IF NOT EXISTS are no-ops once present.

ALTER TABLE community_summaries ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;
ALTER TABLE community_summaries ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;

UPDATE community_summaries
   SET created_at = COALESCE(created_at, (metadata->>'timestamp')::timestamptz),
       updated_at = COALESCE(updated_at, (metadata->>'timestamp')::timestamptz)
 WHERE metadata ? 'timestamp'
   AND metadata->>'timestamp' ~ '^\d{4}-\d\d-\d\d'
   AND (created_at IS NULL OR updated_at IS NULL);

ALTER TABLE community_summaries ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE community_summaries ALTER COLUMN updated_at SET DEFAULT now();

CREATE INDEX IF NOT EXISTS community_summaries_updated_at_idx ON community_summaries (updated_at);
