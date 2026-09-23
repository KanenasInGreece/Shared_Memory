-- Migration 030: a registry so fact entity strings are not projected verbatim
-- into Neo4j :Entity nodes. Seeded from existing fact entities. Names longer
-- than 255 characters are skipped because the column is still VARCHAR(255);
-- migration 034 widens it.

BEGIN;

CREATE TABLE IF NOT EXISTS entity_registry (
    name          VARCHAR(255) PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    registered_by VARCHAR(64) NOT NULL DEFAULT 'system'
);

CREATE INDEX IF NOT EXISTS entity_registry_created_at_idx ON entity_registry (created_at);

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
