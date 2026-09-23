-- Migration 014 (ADR-017): entity names are embedded once (BGE-M3, 1024) so an
-- alias sweep is an ANN lookup, not an all-pairs re-embed. alias_adjudications
-- is the verdict ledger and the don't-re-ask cache.

BEGIN;

CREATE TABLE IF NOT EXISTS entity_embeddings (
    name       TEXT PRIMARY KEY,
    embedding  vector(1024) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS entity_embeddings_embedding_idx
    ON entity_embeddings USING hnsw (embedding vector_cosine_ops);

-- name_a < name_b is the pair's canonical order; nothing in the table enforces it.
-- verdict is 'alias' or 'distinct'; method is 'normalized_exact' or 'llm'.
-- cosine is name-similarity at judgement time, lexical_jaccard is token overlap,
-- shared_facts counts facts mentioning both, and domain_disjoint flags an over-merge.
CREATE TABLE IF NOT EXISTS alias_adjudications (
    id              BIGSERIAL PRIMARY KEY,
    name_a          TEXT NOT NULL,
    name_b          TEXT NOT NULL,
    verdict         TEXT NOT NULL,
    method          TEXT NOT NULL,
    confidence      REAL,
    cosine          REAL,
    lexical_jaccard REAL,
    shared_facts    INT,
    domain_disjoint BOOLEAN,
    rationale       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name_a, name_b)
);

CREATE INDEX IF NOT EXISTS alias_adjudications_verdict_idx
    ON alias_adjudications (verdict);

COMMIT;
