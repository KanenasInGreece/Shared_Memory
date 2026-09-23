-- Migration 016: created_at and updated_at on community_summaries, the only
-- store that had no time column (decision: universal timestamps as latency
-- instrumentation, not just recency). created_at is the first fold; updated_at
-- moves on each in-place re-fold. The regex accepts only an ISO-shaped
-- metadata timestamp so a bad value cannot abort the migration.

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
