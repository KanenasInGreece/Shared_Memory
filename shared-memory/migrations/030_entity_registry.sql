-- 030 — entity_registry table for human-vetted Fact entity validation
--
-- WHAT WAS WRONG. Free-text metadata['entities'] on facts was projected verbatim
-- into Neo4j :Entity nodes without a write-time registry gate, creating graph bloat
-- (2,167 entity nodes, 3,695 decision/retro entity edges).
--
-- WHAT THIS DOES.
--   1. Create `entity_registry` table:
--      (name VARCHAR PRIMARY KEY, created_at TIMESTAMPTZ, registered_by VARCHAR).
--   2. Seed `entity_registry` from existing Fact entity strings in technical_docs.
--
-- Idempotent: safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS entity_registry (
    name          VARCHAR(255) PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    registered_by VARCHAR(64) NOT NULL DEFAULT 'system'
);

CREATE INDEX IF NOT EXISTS entity_registry_created_at_idx ON entity_registry (created_at);

-- Seed entity_registry from existing Fact records in technical_docs
INSERT INTO entity_registry (name, registered_by)
SELECT DISTINCT ename AS name, 'bootstrap' AS registered_by
FROM (
    SELECT jsonb_array_elements_text(metadata->'entities') AS ename
    FROM technical_docs
    WHERE (metadata->>'kind' IS NULL OR metadata->>'kind' = 'fact')
      AND metadata->'entities' IS NOT NULL
      AND jsonb_typeof(metadata->'entities') = 'array'
) sub
WHERE ename IS NOT NULL
  AND btrim(ename) <> ''
  AND length(ename) <= 255
ON CONFLICT (name) DO NOTHING;

COMMIT;
