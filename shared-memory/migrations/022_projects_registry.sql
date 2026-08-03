-- 022 — the projects registry.
--
-- Until now a project was whatever string a client happened to send. There was
-- nothing to be unknown AGAINST, so a typo was indistinguishable from a new
-- project and both entered the corpus silently. This table is what makes an
-- unrecognised value loud instead of a new spelling.
--
-- SEEDING: every distinct project already resolvable in technical_docs, via the
-- same COALESCE every reader shares (project_axis.PROJECT_SQL) — the decision
-- payload first, then the top-level field. Seeding from live data rather than a
-- hardcoded list is what makes this migration portable: it registers whatever a
-- given deployment has actually been using, on any install.
--
-- DESCRIPTIONS ARE DELIBERATELY NULL. A description is what the fold prompt reads
-- as framing and the second signal for proposals, so inventing one would put words
-- into the corpus that no one wrote. They are owed from the operator, and a NULL
-- says "not yet supplied" where a guessed sentence would say "supplied, and wrong".
--
-- The seed does NOT merge near-duplicate spellings. Registering what exists is a
-- separate act from deciding two names are one project; that judgement belongs to
-- the operator and to normalize_projects.py, and doing it silently here would
-- destroy the evidence that the drift happened.
--
-- Idempotent: ON CONFLICT DO NOTHING, and re-running seeds only names that appeared
-- since.

-- Trigram similarity backs the proposals a rejected save returns. It is the FIRST
-- proposal signal on purpose: it needs no embedder, so registration can never be
-- taken down by an embedding outage the way a vector-only lookup would be.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS projects (
    name        text PRIMARY KEY,
    description text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text,
    -- The parked-project sentinel is RESERVED, and reserving it in the schema
    -- rather than in a code path means no future writer can register it by
    -- accident. A sentinel inside the project set would be counted by the
    -- insight gate's ">= 2 distinct projects" rule and would fold as though it
    -- were a subject.
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
