-- Migration 011: next_attempt_at so a Neo4j outage backs off instead of
-- re-claiming every pending row on each drain. NULL means never failed and
-- stays eligible immediately. The pending-id index from 002 still serves
-- ORDER BY id; the time predicate is only a filter on that small set.

BEGIN;

ALTER TABLE neo4j_outbox
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;

COMMIT;
