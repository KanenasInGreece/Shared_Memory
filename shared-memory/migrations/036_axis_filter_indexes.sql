-- Migration 036: indexes for the axis filters (--project / --domain on
-- POST /memory/search), and the reason they were missing.
--
-- WHY THIS EXISTS. `handle_search`'s axis filter binds the resolved set of
-- spellings straight against `metadata->>'project'` (and, for domains,
-- `metadata->'domains'`) — see project_axis.py / domain_axis.py and migration
-- 035's `expand_axis_spellings`. Neither expression carried an index, so a
-- selective filter forced a full scan of `technical_docs`, and past a corpus
-- size where HNSW's own candidate handoff stops covering the filtered rows,
-- the query returned ZERO matches rather than merely running slowly — the
-- same query on the same data answers correctly below that size and silently
-- wrongly above it. Measured, throwaway copy, pgvector 0.8.2:
--
--   | rows    | majority filter, HNSW only | + this expr index | minority filter (1.8%) HNSW-only / +index / +index+iterative_scan |
--   |---------|-----------------------------|--------------------|----------------------------------------------------------------------|
--   | 14,840  | 117 ms Seq Scan             | 6.8 ms             | 29 / 7.6 / 7.4 ms                                                     |
--   | 74,200  | 519 ms Seq Scan             | 16 ms              | 128 / 31 / 28 ms                                                      |
--   | 296,800 | 29 ms                       | 28 ms              | 0 rows / 0 rows / 22 rows in 217 ms                                   |
--
-- The expression index fixes the Seq-Scan regression that shows up from
-- ~15k rows. It does NOT fix the 296,800-row empty-result case on its own —
-- that needs `hnsw.iterative_scan` (coordinator.py, pgvector >= 0.8), which
-- is why this migration and that session setting are decision:1584's two
-- halves together, grounded on the measurement above (fact:1583).
--
-- ⛔ WHY THE INDEX KEYS ON `metadata->>'project'`, NEVER `normalized_key`.
-- `normalized_key` (migration 035) is the REGISTRY's column — it lives on
-- `projects` / `project_domains`, one row per registered name. The search
-- filter does not look anything up by it at query time: `_resolve_search_
-- filters` already expands the supplied value to the full set of live
-- spellings (canonical + every alias/variant that keys the same) in Python,
-- and binds that literal SET against `metadata->>'project' = ANY($n)` —
-- see migration 035's own tests for that predicate shape. So the column an
-- index must speed up is the one the predicate actually compares against
-- literal text: the stored expression on `technical_docs`, not the
-- registry's key column, which this table does not even carry.
--
-- Domains are stored as a JSONB array (`metadata->'domains'`), never a
-- scalar, so a GIN index on the raw JSONB path is the analogous fix for the
-- containment/overlap comparisons `_axis_filter_predicate` builds for that
-- field — unmeasured on this deployment (no minority-domain-filter run was
-- taken), added by the same reasoning as the project index and named as
-- unmeasured rather than implied by the table above (fact:1338).
--
-- Studied against 035_axis_normalized_keys.sql (why a stored/derived
-- expression is indexed rather than looked up) and 010_hnsw_embedding_
-- indexes.sql (idempotent index creation in this chain) for house style.
--
-- IDEMPOTENT: both statements are IF NOT EXISTS; a re-run touches nothing.

BEGIN;

CREATE INDEX IF NOT EXISTS technical_docs_project_expr_idx
    ON technical_docs ((metadata->>'project'));

CREATE INDEX IF NOT EXISTS technical_docs_domains_gin_idx
    ON technical_docs USING gin ((metadata->'domains'));

COMMIT;
