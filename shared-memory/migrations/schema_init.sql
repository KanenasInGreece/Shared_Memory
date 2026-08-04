-- schema_init.sql — full schema for a fresh install.
--
-- AUTO-GENERATED from the migration chain — do NOT edit by hand. The
-- generator applies every NNN_*.sql migration to a throwaway database and
-- introspects the result, so this file is equivalent to running apply.py
-- on an empty database by construction.
--
-- USE THIS for new installs: creates the complete schema in one shot.
-- Idempotent (IF NOT EXISTS throughout).
--
-- Upgrading an existing install? Use apply.py — it only runs pending migrations.
--
-- Regenerate after every new migration:
--   uv run --with psycopg2-binary python shared-memory/migrations/generate_schema_init.py
--
-- EMBEDDING DIMENSION: vector columns default to 1024-dim for BGE-M3. To use
-- a different model, change vector(1024) in 000_base_schema.sql, then
-- regenerate. The invariant is that ALL agents share ONE model via the
-- gateway — not the specific dimension.
--
-- Also run neo4j_init.cypher to initialise the Neo4j constraint set.
--
-- Usage:
--   psql -U postgres agent_data < shared-memory/migrations/schema_init.sql

BEGIN;

-- ─── Extensions ────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE EXTENSION IF NOT EXISTS vector;

-- ─── alias_adjudications ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alias_adjudications (
    id               BIGSERIAL PRIMARY KEY,
    name_a           TEXT NOT NULL,
    name_b           TEXT NOT NULL,
    verdict          TEXT NOT NULL,
    method           TEXT NOT NULL,
    confidence       REAL,
    cosine           REAL,
    lexical_jaccard  REAL,
    shared_facts     INTEGER,
    domain_disjoint  BOOLEAN,
    rationale        TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS alias_adjudications_name_a_name_b_key ON public.alias_adjudications USING btree (name_a, name_b);
CREATE INDEX IF NOT EXISTS alias_adjudications_verdict_idx ON public.alias_adjudications USING btree (verdict);

-- ─── aliases ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS aliases (
    id               BIGSERIAL PRIMARY KEY,
    name             TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT aliases_not_blank CHECK ((btrim(name) <> ''::text)),
    CONSTRAINT aliases_sentinel_reserved CHECK ((name <> 'general_discussion'::text))
);

CREATE UNIQUE INDEX IF NOT EXISTS aliases_name_key ON public.aliases USING btree (name);

-- ─── community_summaries ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS community_summaries (
    id               SERIAL PRIMARY KEY,
    content          TEXT NOT NULL,
    metadata         JSONB,
    embedding        vector(1024),
    agent_id         TEXT NOT NULL DEFAULT 'legacy'::text,
    scope            TEXT NOT NULL DEFAULT 'global'::text,
    visibility       TEXT NOT NULL DEFAULT 'global'::text,
    source_pg_ids    INT4[],
    summary_history  JSONB NOT NULL DEFAULT '[]'::jsonb,
    superseded       BOOLEAN NOT NULL DEFAULT false,
    created_at       TIMESTAMPTZ DEFAULT now(),
    updated_at       TIMESTAMPTZ DEFAULT now(),
    run_id           BIGINT
);

CREATE INDEX IF NOT EXISTS community_summaries_active_idx ON public.community_summaries USING btree (id) WHERE (NOT superseded);
CREATE INDEX IF NOT EXISTS community_summaries_agent_id_idx ON public.community_summaries USING btree (agent_id);
CREATE INDEX IF NOT EXISTS community_summaries_embedding_idx ON public.community_summaries USING hnsw (embedding vector_cosine_ops);
CREATE UNIQUE INDEX IF NOT EXISTS community_summaries_entity_domain_unique ON public.community_summaries USING btree (((metadata ->> 'entity'::text)), ((metadata ->> 'domain'::text))) WHERE (COALESCE((metadata ->> 'kind'::text), 'thematic'::text) <> 'insight'::text);
CREATE INDEX IF NOT EXISTS community_summaries_scope_idx ON public.community_summaries USING btree (scope);
CREATE INDEX IF NOT EXISTS community_summaries_updated_at_idx ON public.community_summaries USING btree (updated_at);
CREATE INDEX IF NOT EXISTS community_summaries_visibility_idx ON public.community_summaries USING btree (visibility);

-- ─── consolidation_runs ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS consolidation_runs (
    id               BIGSERIAL PRIMARY KEY,
    cycle_type       TEXT NOT NULL,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ,
    outcome          TEXT,
    folds_attempted  INTEGER NOT NULL DEFAULT 0,
    folds_succeeded  INTEGER NOT NULL DEFAULT 0,
    folds_failed     INTEGER NOT NULL DEFAULT 0,
    eligible_clusters INTEGER,
    eligible_oldest_age_seconds INTEGER,
    error_class      TEXT,
    error_msg        TEXT,
    extra            JSONB
);

CREATE INDEX IF NOT EXISTS consolidation_runs_inflight_idx ON public.consolidation_runs USING btree (started_at DESC) WHERE (finished_at IS NULL);
CREATE INDEX IF NOT EXISTS consolidation_runs_type_started_idx ON public.consolidation_runs USING btree (cycle_type, started_at DESC);

-- ─── entity_embeddings ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS entity_embeddings (
    name             TEXT PRIMARY KEY,
    embedding        vector(1024) NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS entity_embeddings_embedding_idx ON public.entity_embeddings USING hnsw (embedding vector_cosine_ops);

-- ─── neo4j_outbox ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS neo4j_outbox (
    id               BIGSERIAL PRIMARY KEY,
    pg_id            BIGINT NOT NULL,
    cypher_params    JSONB NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'::text,
    retries          INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ DEFAULT now(),
    applied_at       TIMESTAMPTZ,
    next_attempt_at  TIMESTAMPTZ,
    rem_reviewed_at  TIMESTAMPTZ,
    consolidated_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS neo4j_outbox_pending_id_idx ON public.neo4j_outbox USING btree (id) WHERE (status = 'pending'::text);
CREATE INDEX IF NOT EXISTS neo4j_outbox_pending_idx ON public.neo4j_outbox USING btree (status) WHERE (status = 'pending'::text);

-- ─── project_aliases ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS project_aliases (
    id               BIGSERIAL PRIMARY KEY,
    alias_id         BIGINT NOT NULL,
    project          TEXT NOT NULL,
    active           BOOLEAN NOT NULL DEFAULT true,
    reason           TEXT,
    created_by       TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_at    TIMESTAMPTZ,
    CONSTRAINT project_aliases_superseded_consistent CHECK (((active AND (superseded_at IS NULL)) OR ((NOT active) AND (superseded_at IS NOT NULL))))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_project_aliases_one_active ON public.project_aliases USING btree (alias_id) WHERE active;
CREATE INDEX IF NOT EXISTS idx_project_aliases_project ON public.project_aliases USING btree (project);

-- ─── project_promotions ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS project_promotions (
    id               BIGSERIAL PRIMARY KEY,
    pg_id            BIGINT NOT NULL,
    from_project     TEXT,
    to_project       TEXT NOT NULL,
    method           TEXT NOT NULL,
    actor            TEXT NOT NULL,
    note             TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT project_promotions_from_parked CHECK (((from_project IS NULL) OR (from_project = 'general_discussion'::text))),
    CONSTRAINT project_promotions_to_real CHECK (((btrim(to_project) <> ''::text) AND (to_project <> 'general_discussion'::text)))
);

CREATE INDEX IF NOT EXISTS idx_project_promotions_created_at ON public.project_promotions USING btree (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_project_promotions_pg_id ON public.project_promotions USING btree (pg_id);

-- ─── projects ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS projects (
    name             TEXT PRIMARY KEY,
    description      TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by       TEXT,
    CONSTRAINT projects_sentinel_reserved CHECK ((name <> 'general_discussion'::text))
);

CREATE INDEX IF NOT EXISTS idx_projects_name_trgm ON public.projects USING gin (name gin_trgm_ops);

-- ─── relation_adjudications ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS relation_adjudications (
    id               BIGSERIAL PRIMARY KEY,
    family           TEXT NOT NULL,
    src_name         TEXT,
    tgt_name         TEXT,
    src_pg_id        BIGINT,
    tgt_pg_id        BIGINT,
    rel_type         TEXT NOT NULL,
    verdict          TEXT NOT NULL,
    method           TEXT NOT NULL,
    confidence       REAL,
    support          TEXT,
    signals          JSONB,
    rationale        TEXT,
    model            TEXT,
    run_id           TEXT,
    operator_label   TEXT,
    operator_labeled_at TIMESTAMPTZ,
    promoted_at      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT relation_adjudications_check CHECK ((((family = 'entity_relation'::text) AND (src_name IS NOT NULL) AND (tgt_name IS NOT NULL) AND (src_pg_id IS NULL) AND (tgt_pg_id IS NULL)) OR ((family = 'evidential'::text) AND (src_pg_id IS NOT NULL) AND (tgt_pg_id IS NOT NULL) AND (src_name IS NULL) AND (tgt_name IS NULL)))),
    CONSTRAINT relation_adjudications_confidence_check CHECK (((confidence IS NULL) OR ((confidence >= (0.0)::double precision) AND (confidence <= (1.0)::double precision)))),
    CONSTRAINT relation_adjudications_family_check CHECK ((family = ANY (ARRAY['entity_relation'::text, 'evidential'::text]))),
    CONSTRAINT relation_adjudications_method_check CHECK ((method = ANY (ARRAY['llm_sweep'::text, 'rem_k3'::text, 'operator'::text]))),
    CONSTRAINT relation_adjudications_operator_label_check CHECK (((operator_label IS NULL) OR (operator_label = ANY (ARRAY['correct'::text, 'incorrect'::text])))),
    CONSTRAINT relation_adjudications_support_check CHECK (((support IS NULL) OR (support = ANY (ARRAY['text_only'::text, 'graph_evidence'::text])))),
    CONSTRAINT relation_adjudications_verdict_check CHECK ((verdict = ANY (ARRAY['accept'::text, 'reject'::text])))
);

CREATE UNIQUE INDEX IF NOT EXISTS relation_adjudications_entity_uniq ON public.relation_adjudications USING btree (family, src_name, tgt_name, rel_type) WHERE (family = 'entity_relation'::text);
CREATE UNIQUE INDEX IF NOT EXISTS relation_adjudications_record_uniq ON public.relation_adjudications USING btree (family, src_pg_id, tgt_pg_id, rel_type) WHERE (family = 'evidential'::text);
CREATE INDEX IF NOT EXISTS relation_adjudications_review_idx ON public.relation_adjudications USING btree (family, operator_label, created_at);

-- ─── technical_docs ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS technical_docs (
    id               SERIAL PRIMARY KEY,
    content          TEXT NOT NULL,
    metadata         JSONB,
    embedding        vector(1024),
    content_hash     TEXT,
    agent_id         TEXT NOT NULL DEFAULT 'legacy'::text,
    scope            TEXT NOT NULL DEFAULT 'global'::text,
    visibility       TEXT NOT NULL DEFAULT 'global'::text,
    superseded       BOOLEAN NOT NULL DEFAULT false,
    superseded_by    INTEGER,
    created_at       TIMESTAMPTZ DEFAULT now(),
    rem_timing       JSONB
);

CREATE INDEX IF NOT EXISTS technical_docs_agent_id_idx ON public.technical_docs USING btree (agent_id);
CREATE UNIQUE INDEX IF NOT EXISTS technical_docs_content_hash_key ON public.technical_docs USING btree (content_hash);
CREATE INDEX IF NOT EXISTS technical_docs_created_at_idx ON public.technical_docs USING btree (created_at);
CREATE INDEX IF NOT EXISTS technical_docs_embedding_idx ON public.technical_docs USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS technical_docs_scope_idx ON public.technical_docs USING btree (scope);
CREATE INDEX IF NOT EXISTS technical_docs_superseded_by_idx ON public.technical_docs USING btree (superseded_by) WHERE (superseded_by IS NOT NULL);
CREATE INDEX IF NOT EXISTS technical_docs_visibility_idx ON public.technical_docs USING btree (visibility);

-- ─── Foreign keys ──────────────────────────────────────────────────────────
-- Added after every table exists: a referencing table can sort before its
-- target, so these cannot be inline column constraints.

DO $$ BEGIN
    ALTER TABLE project_aliases ADD CONSTRAINT project_aliases_alias_id_fkey FOREIGN KEY (alias_id) REFERENCES aliases(id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE project_aliases ADD CONSTRAINT project_aliases_project_fkey FOREIGN KEY (project) REFERENCES projects(name);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE project_promotions ADD CONSTRAINT project_promotions_to_project_fkey FOREIGN KEY (to_project) REFERENCES projects(name);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE technical_docs ADD CONSTRAINT technical_docs_superseded_by_fkey FOREIGN KEY (superseded_by) REFERENCES technical_docs(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ─── Functions ─────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.assert_alias_namespaces_disjoint()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_TABLE_NAME = 'project_aliases' THEN
        IF NEW.active AND EXISTS (
            SELECT 1 FROM projects p
              JOIN aliases a ON a.id = NEW.alias_id
             WHERE p.name = a.name
        ) THEN
            RAISE EXCEPTION
                'alias % is also a registered project — an alias and a canonical '
                'name must never be the same string (A1)',
                (SELECT name FROM aliases WHERE id = NEW.alias_id);
        END IF;
    ELSE  -- projects
        IF EXISTS (
            SELECT 1 FROM project_aliases pa
              JOIN aliases a ON a.id = pa.alias_id
             WHERE pa.active AND a.name = NEW.name
        ) THEN
            RAISE EXCEPTION
                'project % is already an active alias for another project — '
                'register the canonical name instead (A1)', NEW.name;
        END IF;
    END IF;
    RETURN NEW;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.notify_new_artifact()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
  -- The payload carries the row id so the daemon can act without re-querying
  -- for what just changed. Postgres caps a notification payload at 8000 bytes,
  -- which is why this sends an identifier and never the content.
  PERFORM pg_notify('new_artifact', json_build_object('pg_id', NEW.id)::text);
  RETURN NEW;
END;
$function$
;

-- ─── Triggers ──────────────────────────────────────────────────────────────
-- Created after the functions they call. DROP-then-CREATE because Postgres
-- has no CREATE TRIGGER IF NOT EXISTS and this file promises idempotency.

DROP TRIGGER IF EXISTS trg_project_aliases_disjoint ON project_aliases;
CREATE TRIGGER trg_project_aliases_disjoint BEFORE INSERT OR UPDATE ON public.project_aliases FOR EACH ROW EXECUTE FUNCTION assert_alias_namespaces_disjoint();

DROP TRIGGER IF EXISTS trg_projects_disjoint ON projects;
CREATE TRIGGER trg_projects_disjoint BEFORE INSERT OR UPDATE ON public.projects FOR EACH ROW EXECUTE FUNCTION assert_alias_namespaces_disjoint();

DROP TRIGGER IF EXISTS trg_notify_new_artifact ON technical_docs;
CREATE TRIGGER trg_notify_new_artifact AFTER INSERT ON public.technical_docs FOR EACH ROW EXECUTE FUNCTION notify_new_artifact();

COMMIT;
