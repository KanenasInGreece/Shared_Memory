-- Migration 043: add superseded_by pointer to community_summaries (PR 2, A4).
-- Points a superseded summary at its successor summary.
-- An insight follows this successor when the superseded thematic summary is replaced.

BEGIN;

ALTER TABLE community_summaries
    ADD COLUMN IF NOT EXISTS superseded_by INT4 REFERENCES community_summaries(id) ON DELETE SET NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'community_summaries_successor_only_when_superseded'
    ) THEN
        ALTER TABLE community_summaries
            ADD CONSTRAINT community_summaries_successor_only_when_superseded
            CHECK (superseded_by IS NULL OR superseded);
    END IF;
END $$;

COMMIT;
