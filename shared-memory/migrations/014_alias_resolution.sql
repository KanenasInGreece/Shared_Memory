-- Migration 014: entity-resolution / alias layer (ADR-017 "A") writer support
--
-- Two tables backing the alias-writer sweep:
--
--   entity_embeddings   — the SCALING choice for candidate generation. Entity
--     names are embedded ONCE (BGE-M3, 1024-dim, via the gateway) and stored
--     with an HNSW index, so a sweep upserts only NEW entities and finds
--     cosine-near candidates with an indexed ANN query (O(new . log N)) instead
--     of re-embedding all N names and doing an O(N^2) matrix each run. This
--     reuses the technical_docs pgvector+HNSW pattern (migration 010) and keeps
--     vectors in Postgres (Neo4j stays the structure tier).
--
--   alias_adjudications — the per-pair verdict ledger. It is both the AUDIT
--     trail (method/score/confidence/rationale per decision, revocable) and the
--     idempotency cache: a sweep skips pairs already adjudicated, so the LLM is
--     never re-asked about a pair it has already judged.
--
-- IDEMPOTENT: apply.py re-runs the whole chain; every statement is a no-op once
-- the objects exist.

BEGIN;

-- ── entity-name embedding store (embed-once, incremental) ─────────────────────
CREATE TABLE IF NOT EXISTS entity_embeddings (
    name       TEXT PRIMARY KEY,              -- Entity identity (coordinator MERGEs by name)
    embedding  vector(1024) NOT NULL,         -- BGE-M3 name embedding (gateway 1024-dim contract)
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS entity_embeddings_embedding_idx
    ON entity_embeddings USING hnsw (embedding vector_cosine_ops);

-- ── alias adjudication ledger (audit + don't-re-ask idempotency cache) ────────
CREATE TABLE IF NOT EXISTS alias_adjudications (
    id              BIGSERIAL PRIMARY KEY,
    name_a          TEXT NOT NULL,            -- canonical order: name_a < name_b
    name_b          TEXT NOT NULL,
    verdict         TEXT NOT NULL,            -- 'alias' | 'distinct'
    method          TEXT NOT NULL,            -- 'normalized_exact' | 'llm'
    confidence      REAL,                     -- 0..1 (llm) or 1.0 (normalized_exact)
    cosine          REAL,                     -- name-cosine at adjudication time
    lexical_jaccard REAL,                     -- token Jaccard of the two names
    shared_facts    INT,                      -- graph confirmer: # facts mentioning both
    domain_disjoint BOOLEAN,                  -- over-merge warning: mentioning-fact domains disjoint
    rationale       TEXT,                     -- short LLM justification (audit)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name_a, name_b)
);

CREATE INDEX IF NOT EXISTS alias_adjudications_verdict_idx
    ON alias_adjudications (verdict);

COMMIT;
