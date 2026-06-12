-- Migration 011: outbox retry backoff
--
-- Without per-row backoff, a Neo4j outage turns the 2 s outbox drain cycle into
-- a retry storm: every poll re-claims up to OUTBOX_BATCH_SIZE failing rows and
-- re-hammers a down backend (~BATCH_SIZE × 30/min). This column lets the
-- coordinator push a failed row's next eligible attempt out by an exponential,
-- jittered delay (coordinator.py: OUTBOX_BACKOFF_BASE/MAX), so an outage backs
-- off instead of spinning.
--
-- The drain claim query gates on:
--   AND (next_attempt_at IS NULL OR next_attempt_at <= now())
-- NULL = never failed (or pre-backoff row) → eligible immediately, preserving
-- the fast path for healthy saves.
--
-- The partial drain index (migration 002, neo4j_outbox_pending_id_idx) still
-- covers ORDER BY id; the added time predicate is a cheap filter on the small
-- pending set, so no new index is needed.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS — safe to re-run on every apply.py pass.

BEGIN;

ALTER TABLE neo4j_outbox
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;

COMMIT;
