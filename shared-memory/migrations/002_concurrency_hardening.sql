-- Migration 002: one summary per entity, and a pending-outbox index so
-- ORDER BY id FOR UPDATE SKIP LOCKED needs no extra sort.
--
-- The DELETE keeps one row per entity, the key this file was written against.
-- If community_summaries_entity_domain_unique already exists (migration 007),
-- that DELETE would destroy legitimate per-domain summaries. apply.py runs
-- each file once; the guard is defence in depth for any later re-entry.

BEGIN;

DO $guard$
BEGIN
    IF to_regclass('public.community_summaries_entity_domain_unique') IS NOT NULL THEN
        RAISE NOTICE 'migration 002: entity-level dedup SKIPPED — summaries are keyed on (entity, domain) since migration 007, so this DELETE would destroy legitimately distinct summaries.';
    ELSE
        DELETE FROM community_summaries a
        USING community_summaries b
        WHERE a.id < b.id
          AND a.metadata->>'entity' = b.metadata->>'entity'
          AND a.metadata->>'entity' IS NOT NULL;
    END IF;
END
$guard$;

-- Partial so rows with no entity key stay outside the unique index.
CREATE UNIQUE INDEX IF NOT EXISTS community_summaries_entity_unique
    ON community_summaries ((metadata->>'entity'))
    WHERE metadata->>'entity' IS NOT NULL;

CREATE INDEX IF NOT EXISTS neo4j_outbox_pending_id_idx
    ON neo4j_outbox (id)
    WHERE status = 'pending';

COMMIT;
