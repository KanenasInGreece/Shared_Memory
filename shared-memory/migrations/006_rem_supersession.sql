-- Migration 006: a superseded flag so search can hide a summary whose source
-- facts are covered by a later one, plus a one-time fold of ad-hoc source
-- labels onto "claude" and "lm_studio". design_session_cloe is left as-is.
-- jsonb_typeof = 'object' is required: jsonb_set errors on a scalar.

BEGIN;

ALTER TABLE community_summaries
    ADD COLUMN IF NOT EXISTS superseded BOOLEAN NOT NULL DEFAULT false;

-- Active rows only, so retrieval does not scan superseded history.
CREATE INDEX IF NOT EXISTS community_summaries_active_idx
    ON community_summaries (id)
    WHERE NOT superseded;

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

UPDATE technical_docs
SET metadata = jsonb_set(metadata, '{source}', '"lm_studio"')
WHERE metadata IS NOT NULL
  AND jsonb_typeof(metadata) = 'object'
  AND (metadata->>'source' = 'workstation-assistant'
       OR metadata->>'source' IS NULL);

COMMIT;
