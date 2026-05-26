-- Migration 001: Multi-agent schema support
--
-- Adds agent_id, scope, visibility to technical_docs and community_summaries.
-- Creates neo4j_outbox table for the coordinator outbox pattern.
--
-- All changes are additive. Existing rows default to:
--   agent_id = 'legacy'  (pre-coordinator writes)
--   scope    = 'global'  (visible to all agents)
--   visibility = 'global'
--
-- Idempotent: safe to run multiple times (IF NOT EXISTS throughout).

BEGIN;

-- ─── technical_docs ───────────────────────────────────────────────────────────

ALTER TABLE technical_docs
    ADD COLUMN IF NOT EXISTS agent_id   TEXT NOT NULL DEFAULT 'legacy',
    ADD COLUMN IF NOT EXISTS scope      TEXT NOT NULL DEFAULT 'global',
    ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'global';

CREATE INDEX IF NOT EXISTS technical_docs_agent_id_idx   ON technical_docs (agent_id);
CREATE INDEX IF NOT EXISTS technical_docs_scope_idx      ON technical_docs (scope);
CREATE INDEX IF NOT EXISTS technical_docs_visibility_idx ON technical_docs (visibility);

-- ─── community_summaries ──────────────────────────────────────────────────────

ALTER TABLE community_summaries
    ADD COLUMN IF NOT EXISTS agent_id   TEXT NOT NULL DEFAULT 'legacy',
    ADD COLUMN IF NOT EXISTS scope      TEXT NOT NULL DEFAULT 'global',
    ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'global';

CREATE INDEX IF NOT EXISTS community_summaries_agent_id_idx   ON community_summaries (agent_id);
CREATE INDEX IF NOT EXISTS community_summaries_scope_idx      ON community_summaries (scope);
CREATE INDEX IF NOT EXISTS community_summaries_visibility_idx ON community_summaries (visibility);

-- ─── neo4j_outbox ─────────────────────────────────────────────────────────────
-- Outbox pattern for cross-DB atomicity (coordinator Phase 2).
-- Each row is a pending Neo4j write, applied asynchronously by the outbox worker.
-- Partial index on status='pending' keeps worker scans fast as the table grows.

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
