-- Migration 018: run_id links a summary to the consolidation_runs row that wrote
-- it, so lineage is a join through source_pg_ids rather than copied ids. An
-- insight's run_id is the producing cycle; a thematic summary's is the last
-- in-place re-fold, not the cycle that first folded each fact.

ALTER TABLE community_summaries ADD COLUMN IF NOT EXISTS run_id BIGINT;
