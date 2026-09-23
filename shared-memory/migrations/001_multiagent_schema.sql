-- Migration 001: agent_id, scope, and visibility on both stores, plus the
-- neo4j_outbox the coordinator drains. Existing rows default to agent_id
-- 'legacy' and scope/visibility 'global'.

BEGIN;

ALTER TABLE technical_docs
    ADD COLUMN IF NOT EXISTS agent_id   TEXT NOT NULL DEFAULT 'legacy',
    ADD COLUMN IF NOT EXISTS scope      TEXT NOT NULL DEFAULT 'global',
    ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'global';

CREATE INDEX IF NOT EXISTS technical_docs_agent_id_idx   ON technical_docs (agent_id);
CREATE INDEX IF NOT EXISTS technical_docs_scope_idx      ON technical_docs (scope);
CREATE INDEX IF NOT EXISTS technical_docs_visibility_idx ON technical_docs (visibility);

ALTER TABLE community_summaries
    ADD COLUMN IF NOT EXISTS agent_id   TEXT NOT NULL DEFAULT 'legacy',
    ADD COLUMN IF NOT EXISTS scope      TEXT NOT NULL DEFAULT 'global',
    ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'global';

CREATE INDEX IF NOT EXISTS community_summaries_agent_id_idx   ON community_summaries (agent_id);
CREATE INDEX IF NOT EXISTS community_summaries_scope_idx      ON community_summaries (scope);
CREATE INDEX IF NOT EXISTS community_summaries_visibility_idx ON community_summaries (visibility);

-- Pending Neo4j writes. The partial index keeps the worker's status='pending' scan small.

CREATE TABLE IF NOT EXISTS neo4j_outbox (
    id            BIGSERIAL   PRIMARY KEY,
    pg_id         BIGINT      NOT NULL,
    cypher_params JSONB       NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'pending',  -- pending | applied | failed
    retries       INT         NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ          DEFAULT now(),
    applied_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS neo4j_outbox_pending_idx
    ON neo4j_outbox (status) WHERE status = 'pending';

COMMIT;
