-- Migration 039: drop the two axis trigram indexes nothing can use.
--
-- Migration 022 (projects) and 028 (project_domains) each built a
-- `gin_trgm_ops` index over the registry's `name` column, to serve the
-- proposal and confusable lookups — the queries that answer "which registered
-- name did the caller probably mean?" when a save names one that is not on
-- file.
--
-- They have never served one, and could not have. Those queries filter on
-- `similarity(name, $1) >= <floor>` and order by that similarity. A GIN
-- trigram index answers the LIKE / `%` similarity OPERATOR, whose threshold is
-- a session GUC the planner can read; it cannot answer a function call
-- compared against a literal floor, because there is no operator for it to
-- match. So the planner has always chosen a sequential scan.
--
-- MEASURED with EXPLAIN (ANALYZE, BUFFERS) on the live registry (2026-08-28),
-- with both indexes still present:
--
--   PROJECT_PROPOSALS_SQL      0.543 ms   Seq Scan on projects         (38 rows)
--   CONFUSABLE_SQL             0.102 ms   Seq Scan on projects         (38 rows)
--   DOMAIN_PROPOSALS_SQL       0.064 ms   Seq Scan on project_domains  (20 rows)
--   DOMAIN_CONFUSABLE_SQL      0.014 ms   Seq Scan on project_domains  (20 rows)
--   ENTITY_CONFUSABLE_SQL      0.241 ms   Seq Scan on entity_vocabulary
--                                                                    (161 rows)
--
-- ⭐ AND THE INSTRUMENT THAT SETTLES IT, because a plan can be argued with and
-- a counter cannot: `pg_stat_user_indexes.idx_scan` reads **0** for BOTH
-- indexes, against `pg_stat_database.stats_reset = NULL` — the statistics for
-- this database have never been reset, so that zero covers every scan the
-- collector has ever seen. They were not merely unused today; they have never
-- once been used.
--
-- Small row counts, sub-millisecond scans, not one index hit ever. The indexes
-- cost write amplification on every registration and a GIN pending list to
-- maintain (40 kB and 24 kB of it), and buy nothing back. They go.
--
-- ⚠ THE `pg_trgm` EXTENSION STAYS. `similarity()` is the function those
-- queries call, and it comes from that extension — dropping it would break
-- every proposal lookup. What is being removed is the index, not the
-- capability.
--
-- ⚠ AND THIS IS NOT A LICENCE TO SKIP THE INDEX ON A BIGGER TABLE. It is a
-- statement about THIS predicate: a similarity() >= floor filter is not
-- indexable by gin_trgm_ops at any table size. Should either registry grow to
-- where a scan matters, the fix is the `%` operator with
-- `pg_trgm.similarity_threshold`, or a different index — never these two back.
--
-- IDEMPOTENT: apply.py re-runs the whole chain; every statement is a no-op
-- once the objects are gone.

BEGIN;

DROP INDEX IF EXISTS idx_projects_name_trgm;
DROP INDEX IF EXISTS idx_project_domains_name_trgm;

COMMIT;
