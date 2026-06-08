-- Migration 007: domain-scoped community summaries
--
-- NREM consolidation previously clustered facts by entity hub alone and keyed
-- each community summary on (metadata->>'entity').  Facts from unrelated domains
-- that happened to share an entity could therefore be fused into a single
-- incoherent narrative ("domain clutter").
--
-- This migration adds a second keying dimension: the fact domain, derived at
-- consolidation time as COALESCE(metadata->>'project', metadata->>'domain',
-- scope, 'general').  Summaries are now unique per (entity, domain), so a given
-- entity can hold one summary per domain instead of one global summary.
--
-- Backward compatible: untagged facts collapse to domain 'general', which
-- reproduces the previous single-summary-per-entity behaviour.  Domain
-- separation only takes effect once agents tag saves with a project/domain.
--
-- Idempotent: safe to re-run.

BEGIN;

-- ─── Backfill: existing summaries get an explicit 'general' domain ───────────
-- Required before the new unique index: NULL domains would otherwise be treated
-- as distinct, defeating the ON CONFLICT upsert in consolidation_loop.py.
UPDATE community_summaries
SET metadata = jsonb_set(metadata, '{domain}', '"general"')
WHERE metadata IS NOT NULL
  AND jsonb_typeof(metadata) = 'object'
  AND metadata->>'domain' IS NULL;

-- ─── Re-key the unique constraint: (entity) → (entity, domain) ───────────────
DROP INDEX IF EXISTS community_summaries_entity_unique;

CREATE UNIQUE INDEX IF NOT EXISTS community_summaries_entity_domain_unique
    ON community_summaries ((metadata->>'entity'), (metadata->>'domain'));

COMMIT;
