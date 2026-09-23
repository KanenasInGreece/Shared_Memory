-- Migration 032: the axis unique index must exclude superseded rows. Migration
-- 029's index still matched a retired summary, so ON CONFLICT updated it in
-- place and left superseded true — invisible, and the refold ledger could
-- never close. The next fold on that key inserts a new active row. This file
-- only rebuilds the index; it draws no graph SUPERSEDES edge.

BEGIN;

DROP INDEX IF EXISTS community_summaries_axis_level_unique;

CREATE UNIQUE INDEX IF NOT EXISTS community_summaries_axis_level_unique
    ON community_summaries (
        (COALESCE(metadata->>'entity', '')),
        (COALESCE(metadata->>'project', '')),
        (COALESCE(metadata->>'domain', '')),
        (COALESCE(metadata->>'level', 'entity'))
    )
    WHERE COALESCE(metadata->>'kind', 'thematic') <> 'insight'
      AND NOT superseded;

COMMIT;
