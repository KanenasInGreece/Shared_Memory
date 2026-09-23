-- Migration 009 (decision pg_id 276): technical_docs.superseded hides a reversed
-- decision from Tier-1 search. The (entity, domain) unique index is partial so
-- kind='insight' rows are always inserted — sharing the upsert key would update
-- a superseded row in place and leave the new insight invisible.

BEGIN;

ALTER TABLE technical_docs
    ADD COLUMN IF NOT EXISTS superseded boolean NOT NULL DEFAULT false;

DROP INDEX IF EXISTS community_summaries_entity_domain_unique;

CREATE UNIQUE INDEX IF NOT EXISTS community_summaries_entity_domain_unique
    ON community_summaries ((metadata->>'entity'), (metadata->>'domain'))
    WHERE COALESCE(metadata->>'kind', 'thematic') <> 'insight';

COMMIT;
