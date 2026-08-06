-- 029 — thematic community_summaries key on (entity, project, section, level)
--
-- WHAT WAS WRONG. The unique index and upsert key were
--   (metadata.entity, metadata.domain)
-- but metadata.domain held the PROJECT name — a historical squat from when
-- "domain" meant "project bucket" in NREM. The real section axis (migration
-- 028) never reached the fold key, so multi-level fold and section-scoped
-- narratives could not exist without colliding with the project half.
--
-- WHAT THIS DOES.
--   1. DROP the old (entity, domain) partial unique index FIRST — the backfill
--      sets domain to '' for every thematic row; if the old index still stood,
--      two rows that shared an entity under different project-squats would
--      collide on (entity, '') mid-migration.
--   2. For thematic rows: copy the squat into metadata.project, clear
--      metadata.domain to '' (section unknown for historical rows), set
--      metadata.level = 'entity'. Idempotent: rows that already have
--      metadata.project are left alone on the project copy.
--   3. Create a partial unique index on
--        (COALESCE(entity,''), COALESCE(project,''),
--         COALESCE(domain,''), COALESCE(level,'entity'))
--      WHERE kind <> 'insight'.
--      COALESCE is load-bearing: domain-level summaries have no entity, and a
--      NULL in a unique index would make duplicates legal.
--
-- Insights are untouched (always-INSERT; kind='insight' excluded from the
-- partial index). No data is deleted; no row is marked superseded.
--
-- Idempotent: safe to re-run.

BEGIN;

-- 1. Drop the squat-era unique index BEFORE rewriting domain.
DROP INDEX IF EXISTS community_summaries_entity_domain_unique;

-- 2. Backfill project / clear section squat / stamp level (thematic only).
UPDATE community_summaries
SET metadata = metadata
    || jsonb_build_object(
        'project', COALESCE(NULLIF(metadata->>'domain', ''), 'general'),
        'domain',  '',
        'level',   COALESCE(NULLIF(metadata->>'level', ''), 'entity')
    )
WHERE COALESCE(metadata->>'kind', 'thematic') <> 'insight'
  AND (metadata->>'project' IS NULL OR metadata->>'project' = '');

-- Rows that already have project but no level still get a level stamp.
UPDATE community_summaries
SET metadata = metadata || jsonb_build_object('level', 'entity')
WHERE COALESCE(metadata->>'kind', 'thematic') <> 'insight'
  AND (metadata->>'level' IS NULL OR metadata->>'level' = '');

-- 3. Axis-keyed unique index (partial — insights stay always-INSERT).
CREATE UNIQUE INDEX IF NOT EXISTS community_summaries_axis_level_unique
    ON community_summaries (
        (COALESCE(metadata->>'entity', '')),
        (COALESCE(metadata->>'project', '')),
        (COALESCE(metadata->>'domain', '')),
        (COALESCE(metadata->>'level', 'entity'))
    )
    WHERE COALESCE(metadata->>'kind', 'thematic') <> 'insight';

COMMIT;
