-- Migration 031: lineage invalidation (Dreaming Cycle Plan to v2, §5 — C3)
--
-- Mechanism B (§5.1/§5.2) retires a community_summaries row when a record it
-- was built from is no longer valid — found by REVERSE LOOKUP on
-- source_pg_ids/summary_ids, never by comparing sets (subset coverage,
-- Mechanism A, cannot express a set that got SMALLER). This migration adds
-- the two pieces of durable state that mechanism needs:
--
--   1. community_summaries.superseded_at / superseded_reason — a durable
--      stamp distinguishing why a row is no longer active: 'coverage' (the
--      existing subset-coverage retirement, supersede_covered_summaries) vs
--      'lineage' (this migration's new invalidation cascade). Nullable, no
--      backfill — every row superseded before this migration keeps both
--      NULL, which is honest: its reason was never recorded.
--
--   2. refold_ledger — a durable ATTRIBUTION TRAIL for the fact-backlog
--      clock, mirroring the project_promotions pattern (every one-time/
--      recurring data operation leaves a queryable record of what ran and
--      why) rather than an in-memory or timestamp-inferred signal. The
--      outbox (neo4j_outbox) covers a record from save until first
--      consolidation, then DELETES the row — presence IS the state. This
--      table covers a record from invalidation until re-consolidation, and
--      does the OPPOSITE: rows are never deleted, only transitioned to a
--      terminal status, because the whole point is to be able to ask later
--      "was this record re-dreamed because of an invalidation, and which
--      one?" One row per RECORD (pg_id) awaiting re-fold — duplicates
--      across two different retired summaries sharing a constituent are
--      legitimate (no uniqueness constraint); due-ness counts DISTINCT
--      pg_id, never a row count.
--
--      trigger_kind/trigger_id carry TWO TYPED SHAPES (never collapsed into
--      one untyped id — the same source_pg_ids vs summary_ids confusion
--      §3.2 forbids one level up): 'technical_docs' (a superseded fact, or a
--      reversed decision — both set technical_docs.superseded) or
--      'community_summaries' (a retired thematic summary whose foundation
--      retirement cascades to an insight resting on it, §5.2 / plan §3.2's
--      summary_ids field).
--
-- Idempotent: safe to re-run.

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

-- Due-ness reads DISTINCT pg_id WHERE status='open' — the read this table
-- exists to serve, every sweep.
CREATE INDEX IF NOT EXISTS refold_ledger_open_pgid_idx
    ON refold_ledger (pg_id)
    WHERE status = 'open';

-- Attribution lookups ("what did summary X's retirement raise?") scan by
-- summary_id; rare relative to the open-row read above, but real (operator
-- diagnosis, per the project_promotions model this table follows).
CREATE INDEX IF NOT EXISTS refold_ledger_summary_idx
    ON refold_ledger (summary_id);

COMMIT;
