-- Migration 004: prior summary versions for drift audit without a temporal
-- table. Each element is {content, source_pg_ids, timestamp}; consolidation_loop.py
-- caps the array at 20 before every DO UPDATE.

ALTER TABLE community_summaries
    ADD COLUMN IF NOT EXISTS summary_history JSONB NOT NULL DEFAULT '[]'::jsonb;
