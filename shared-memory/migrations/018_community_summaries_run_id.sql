-- Migration 018: run_id on community_summaries — fact↔cycle↔summary lineage (Stage 2b)
--
-- Normalises the link from a summary to the consolidation cycle that produced it, so
-- per-record consolidation lineage + cycle duration is a pure JOIN, with NO pg_id/timing
-- duplication (the fact→summary trace already exists via source_pg_ids):
--     fact --source_pg_ids--> community_summaries --run_id--> consolidation_runs
--
-- SEMANTICS — measure the right thing. run_id = the cycle that WROTE this summary row:
--   * insight summaries  (always-INSERT)         → the exact producing cycle (1:1).
--   * thematic summaries (re-fold in place)      → the LAST fold's cycle (SET on DO UPDATE).
-- So run_id answers "which cycle produced / last-refreshed this summary", NOT "which cycle
-- first folded fact X" — facts accumulate into a thematic summary across many cycles, and
-- that per-fact fold-cycle is deliberately NOT captured here (it would need the deleted
-- outbox row). The coarse per-fact latency stays available via community_summaries.created_at
-- − technical_docs.created_at (migrations 015/016).
--
-- Nullable; legacy summaries stay NULL. IDEMPOTENT.

ALTER TABLE community_summaries ADD COLUMN IF NOT EXISTS run_id BIGINT;
