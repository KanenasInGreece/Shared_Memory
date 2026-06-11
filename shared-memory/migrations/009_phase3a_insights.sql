-- Migration 009: Phase 3a insight consolidation (decision pg_id 276)
--
-- 1. technical_docs.superseded — decision-level reversal filter for Tier-1
--    search. Set when a decision is reversed (retrospective rating
--    'reversed'); search excludes superseded rows from candidates. Insights
--    are never invalidated this way — a re-fold writes a superseding insight
--    instead.
--
-- 2. Re-create the community_summaries (entity, domain) unique index as a
--    PARTIAL index excluding kind='insight' rows. Thematic summaries keep
--    their conflict-UPDATE upsert (migration 007); insight rows are
--    always-INSERT with supersession as the dedup mechanism. If insights
--    shared the upsert key, a re-fold would conflict-UPDATE the superseded
--    row in place and superseded=true would silently survive, making the
--    fresh insight invisible (the "resurrection trap").
--
-- Idempotent: safe to re-run.

BEGIN;

ALTER TABLE technical_docs
    ADD COLUMN IF NOT EXISTS superseded boolean NOT NULL DEFAULT false;

DROP INDEX IF EXISTS community_summaries_entity_domain_unique;

CREATE UNIQUE INDEX IF NOT EXISTS community_summaries_entity_domain_unique
    ON community_summaries ((metadata->>'entity'), (metadata->>'domain'))
    WHERE COALESCE(metadata->>'kind', 'thematic') <> 'insight';

COMMIT;
