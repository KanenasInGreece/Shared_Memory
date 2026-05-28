-- Migration 004: summary_history column on community_summaries
--
-- Adds an append-only JSONB array that records the previous N versions of each
-- community summary before it is replaced by a new consolidation cycle.
-- Enables drift auditing without a full temporal schema.
--
-- Each element: {"content": "...", "source_pg_ids": [...], "timestamp": "..."}
-- Capped at 20 entries by consolidation_loop.py before every DO UPDATE.
-- Pre-existing rows start with an empty array (no data loss).

ALTER TABLE community_summaries
    ADD COLUMN IF NOT EXISTS summary_history JSONB NOT NULL DEFAULT '[]'::jsonb;
