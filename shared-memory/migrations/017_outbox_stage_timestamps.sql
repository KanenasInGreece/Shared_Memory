-- Migration 017: outbox stage-transition timestamps — dream-cycle latency instrumentation
--
-- neo4j_outbox is the durable dream-cycle ledger (pending → applied → rem_reviewed →
-- consolidated → row DELETED), but it stamped only created_at + applied_at, so the REM
-- and NREM stage latencies were invisible and evaporated when the row was deleted. Add:
--   rem_reviewed_at  — stamped when REM finishes enriching the fact/decision.
--   consolidated_at  — stamped when NREM folds it into a summary.
--
-- WHILE a row is in flight these give LIVE per-stage latency (how long it has sat in
-- each stage — surfaced by telemetry). The HISTORICAL REM/NREM split is captured to
-- consolidation_runs.extra just before the delete (the daemon does this). Note the
-- COARSE fact→summary latency is already durable independent of the outbox, via
-- community_summaries.created_at (migration 016) minus the source facts' created_at.
--
-- IDEMPOTENT: ADD COLUMN IF NOT EXISTS.

ALTER TABLE neo4j_outbox ADD COLUMN IF NOT EXISTS rem_reviewed_at TIMESTAMPTZ;
ALTER TABLE neo4j_outbox ADD COLUMN IF NOT EXISTS consolidated_at TIMESTAMPTZ;
