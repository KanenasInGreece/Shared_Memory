-- Migration 036: index metadata->>'project' and metadata->'domains', the
-- expressions search filters on. Past the point where HNSW covers the
-- filtered rows, a selective filter returned zero rows rather than a slow
-- correct answer (measured on pgvector 0.8.2: a seq scan from about 15k
-- rows; at 296,800 rows the index alone still returned nothing). That empty
-- result also needs hnsw.iterative_scan (decision:1584, fact:1583).
-- Do not index normalized_key: it is the registry's column, and the predicate
-- binds the already-expanded spelling set against the stored expression.
-- The domains GIN index was not measured on a minority-domain filter (fact:1338).

BEGIN;

CREATE INDEX IF NOT EXISTS technical_docs_project_expr_idx
    ON technical_docs ((metadata->>'project'));

CREATE INDEX IF NOT EXISTS technical_docs_domains_gin_idx
    ON technical_docs USING gin ((metadata->'domains'));

COMMIT;
