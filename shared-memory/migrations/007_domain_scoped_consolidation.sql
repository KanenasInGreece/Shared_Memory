-- Migration 007: summaries are unique on (entity, domain), not entity alone,
-- so facts that share an entity but not a domain are not fused into one narrative.
-- NULL domain is distinct in a unique index and would break ON CONFLICT, so
-- untagged rows are set to 'general' first — also the old one-summary behaviour
-- until saves carry a project or domain.

BEGIN;

UPDATE community_summaries
SET metadata = jsonb_set(metadata, '{domain}', '"general"')
WHERE metadata IS NOT NULL
  AND jsonb_typeof(metadata) = 'object'
  AND metadata->>'domain' IS NULL;

DROP INDEX IF EXISTS community_summaries_entity_unique;

CREATE UNIQUE INDEX IF NOT EXISTS community_summaries_entity_domain_unique
    ON community_summaries ((metadata->>'entity'), (metadata->>'domain'));

COMMIT;
