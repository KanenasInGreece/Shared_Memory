-- Migration 029: thematic summaries key on (entity, project, section, level).
-- metadata.domain held the project name, so the real section could not join
-- the fold key. The old unique index is dropped first: the backfill clears
-- domain, and two rows that shared an entity would collide on (entity, '')
-- if that index still stood. COALESCE is required because a NULL entity
-- (domain-level summaries) would make duplicates legal. Insights stay
-- always-insert.

BEGIN;

-- Before the rewrite. Leaving it up collides mid-backfill.
DROP INDEX IF EXISTS community_summaries_entity_domain_unique;

-- Thematic only. A row that already has project is not recopied.
UPDATE community_summaries
SET metadata = metadata
    || jsonb_build_object(
        'project', COALESCE(NULLIF(metadata->>'domain', ''), 'general'),
        'domain',  '',
        'level',   COALESCE(NULLIF(metadata->>'level', ''), 'entity')
    )
WHERE COALESCE(metadata->>'kind', 'thematic') <> 'insight'
  AND (metadata->>'project' IS NULL OR metadata->>'project' = '');

-- A row that already had project can still lack level.
UPDATE community_summaries
SET metadata = metadata || jsonb_build_object('level', 'entity')
WHERE COALESCE(metadata->>'kind', 'thematic') <> 'insight'
  AND (metadata->>'level' IS NULL OR metadata->>'level' = '');

CREATE UNIQUE INDEX IF NOT EXISTS community_summaries_axis_level_unique
    ON community_summaries (
        (COALESCE(metadata->>'entity', '')),
        (COALESCE(metadata->>'project', '')),
        (COALESCE(metadata->>'domain', '')),
        (COALESCE(metadata->>'level', 'entity'))
    )
    WHERE COALESCE(metadata->>'kind', 'thematic') <> 'insight';

COMMIT;
