-- Migration 022: register the project strings already in technical_docs so a
-- typo is not a new project. Descriptions stay NULL — a guess would be read as
-- framing — and near-duplicate spellings are not merged. pg_trgm proposes
-- matches without an embedder. general_discussion is reserved so the parked
-- sentinel cannot satisfy the insight gate's distinct-project count.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS projects (
    name        text PRIMARY KEY,
    description text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text,
    CONSTRAINT projects_sentinel_reserved CHECK (name <> 'general_discussion')
);

CREATE INDEX IF NOT EXISTS idx_projects_name_trgm
    ON projects USING gin (name gin_trgm_ops);

DO $$
DECLARE
    seeded int;
BEGIN
    INSERT INTO projects (name, created_by)
    SELECT DISTINCT COALESCE(metadata->'decision'->>'project', metadata->>'project'),
           'migration_022'
      FROM technical_docs
     WHERE COALESCE(metadata->'decision'->>'project', metadata->>'project') IS NOT NULL
       AND btrim(COALESCE(metadata->'decision'->>'project', metadata->>'project')) <> ''
       AND COALESCE(metadata->'decision'->>'project', metadata->>'project') <> 'general_discussion'
    ON CONFLICT (name) DO NOTHING;
    GET DIAGNOSTICS seeded = ROW_COUNT;

    RAISE NOTICE 'migration 022: registered % project(s) from existing records; descriptions are owed from the operator', seeded;
END $$;
