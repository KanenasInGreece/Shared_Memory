-- Migration 006: REM/NREM supersession support + source normalisation
--
-- Part A: superseded flag on community_summaries
--   Enables the CommunitySummary supersession hard rule:
--     if A.source_pg_ids ⊆ B.source_pg_ids → A is superseded by B
--   coordinator.py search filters WHERE NOT superseded.
--   consolidation_loop.py marks rows superseded and writes SUPERSEDES edges to Neo4j.
--
-- Part B: source normalisation backfill (authorised by Xenofon 2026-06-04)
--   Pre-Phase-2C saves used ad-hoc source labels. Canonical names after Phase 2C:
--     claude     — all Claude Code / Claude session variants
--     lm_studio  — LM Studio local inference (workstation-assistant + null-source rows)
--     cloe       — Cloe agent (design_session_cloe left as-is, not touched here)
--   Idempotent: UPDATE is a no-op for already-normalised rows.

BEGIN;

-- ─── Part A: superseded flag ──────────────────────────────────────────────────

ALTER TABLE community_summaries
    ADD COLUMN IF NOT EXISTS superseded BOOLEAN NOT NULL DEFAULT false;

-- Partial index keeps retrieval scan O(active rows) as superseded history accumulates.
CREATE INDEX IF NOT EXISTS community_summaries_active_idx
    ON community_summaries (id)
    WHERE NOT superseded;

-- ─── Part B: source normalisation ────────────────────────────────────────────

-- Guard: only touch rows where metadata is a JSON object (not null/scalar/array).
-- jsonb_set raises "cannot set path in scalar" on non-object JSON values.

-- Claude Code and Claude session variants → canonical "claude"
UPDATE technical_docs
SET metadata = jsonb_set(metadata, '{source}', '"claude"')
WHERE metadata IS NOT NULL
  AND jsonb_typeof(metadata) = 'object'
  AND metadata->>'source' IN (
    'claude_code',
    'claude-code',
    'claude_session',
    'claude_code_fix',
    'claude_code_session',
    'claude_code_verification',
    'claude-sonnet-4-6',
    'design_session',
    'architectural_hardening',
    'architectural_fix'
);

-- LM Studio local inference variants → canonical "lm_studio"
UPDATE technical_docs
SET metadata = jsonb_set(metadata, '{source}', '"lm_studio"')
WHERE metadata IS NOT NULL
  AND jsonb_typeof(metadata) = 'object'
  AND (metadata->>'source' = 'workstation-assistant'
       OR metadata->>'source' IS NULL);

COMMIT;
