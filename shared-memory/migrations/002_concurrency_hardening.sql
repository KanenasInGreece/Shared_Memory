-- Migration 002: Concurrency hardening
--
-- Fixes two correctness bugs identified in concurrent multi-agent workloads:
--
-- (a) community_summaries had no unique constraint on the entity name.
--     Two consolidation runs for the same entity cluster (e.g. during a proxy
--     restart overlap) would both INSERT, creating duplicate rows. Queries using
--     ORDER BY id DESC LIMIT 1 became non-deterministic.
--
--     Fix: deduplicate existing rows (keep the most recent per entity), then
--     add a unique index on (metadata->>'entity') and update the INSERT to use
--     ON CONFLICT ... DO UPDATE (handled in consolidation_loop.py).
--
-- (b) neo4j_outbox uses FOR UPDATE SKIP LOCKED for concurrent drain safety.
--     The existing partial index on status='pending' already covers the WHERE
--     clause, but adding an index on (id) WHERE status='pending' allows the
--     planner to satisfy the ORDER BY id efficiently without a sort step under
--     SKIP LOCKED.
--
-- Idempotent: safe to run multiple times (IF NOT EXISTS / DO NOTHING throughout).

BEGIN;

-- ─── community_summaries ──────────────────────────────────────────────────────

-- Step 1: Remove duplicate rows, keeping only the most recent per entity.
-- Uses a self-join: delete rows where a newer row for the same entity exists.
--
-- ⚠ GUARDED, and the guard is not decoration. This DELETE keeps one summary per
-- ENTITY, which was the summary key when this migration was written. Migration
-- 007 re-keyed summaries on (entity, domain), so from that point on several
-- summaries legitimately share an entity — and running this statement against
-- the later schema deletes all but one of them. That is not hypothetical: the
-- old apply.py re-ran every migration on every invocation, and this statement
-- destroyed 12 live summaries.
--
-- apply.py now runs each migration exactly once, which is the real fix. This
-- guard is defence in depth for any path that reaches this file again: if the
-- (entity, domain) index exists, the schema has moved past the key this DELETE
-- assumes and the dedup is not merely unnecessary but wrong.
--
-- The general rule this stands for: a migration is written against the schema as
-- it was at that moment, so a destructive step must assert that assumption still
-- holds rather than trusting it.
DO $guard$
BEGIN
    IF to_regclass('public.community_summaries_entity_domain_unique') IS NOT NULL THEN
        RAISE NOTICE 'migration 002: entity-level dedup SKIPPED — summaries are keyed on (entity, domain) since migration 007, so this DELETE would destroy legitimately distinct summaries.';
    ELSE
        DELETE FROM community_summaries a
        USING community_summaries b
        WHERE a.id < b.id
          AND a.metadata->>'entity' = b.metadata->>'entity'
          AND a.metadata->>'entity' IS NOT NULL;
    END IF;
END
$guard$;

-- Step 2: Unique partial index on entity name.
-- Partial (WHERE entity IS NOT NULL) so rows without an entity key are unaffected.
CREATE UNIQUE INDEX IF NOT EXISTS community_summaries_entity_unique
    ON community_summaries ((metadata->>'entity'))
    WHERE metadata->>'entity' IS NOT NULL;

-- ─── neo4j_outbox ─────────────────────────────────────────────────────────────

-- Covering index for the FOR UPDATE SKIP LOCKED drain query:
--   ORDER BY id LIMIT N FOR UPDATE SKIP LOCKED
-- The partial index keeps it small (only pending rows) and the planner uses it
-- for both the WHERE and ORDER BY without a separate sort.
CREATE INDEX IF NOT EXISTS neo4j_outbox_pending_id_idx
    ON neo4j_outbox (id)
    WHERE status = 'pending';

COMMIT;
