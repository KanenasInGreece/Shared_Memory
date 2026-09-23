-- Migration 031: state for retiring a summary by reverse lookup on its
-- sources, which a subset comparison cannot express once the set shrinks.
-- superseded_reason is 'coverage' or 'lineage'; rows retired earlier keep
-- both stamps NULL because the reason was never stored. refold_ledger rows
-- are closed, never deleted, so a later question can name which invalidation
-- re-opened a fact. trigger_kind is 'technical_docs' or 'community_summaries',
-- not one untyped id. Duplicate pg_ids across two retired summaries are
-- legitimate; due-ness counts DISTINCT pg_id, so there is no unique constraint.

BEGIN;

ALTER TABLE community_summaries
    ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS superseded_reason TEXT;

CREATE TABLE IF NOT EXISTS refold_ledger (
    id             BIGSERIAL PRIMARY KEY,
    pg_id          BIGINT      NOT NULL,
    summary_id     BIGINT      NOT NULL,
    summary_kind   TEXT        NOT NULL,
    trigger_kind   TEXT        NOT NULL,
    trigger_id     BIGINT      NOT NULL,
    status         TEXT        NOT NULL DEFAULT 'open',
    closed_at      TIMESTAMPTZ,
    closed_reason  TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The sweep's open-set read.
CREATE INDEX IF NOT EXISTS refold_ledger_open_pgid_idx
    ON refold_ledger (pg_id)
    WHERE status = 'open';

-- Which ledger rows one summary's retirement raised.
CREATE INDEX IF NOT EXISTS refold_ledger_summary_idx
    ON refold_ledger (summary_id);

COMMIT;
