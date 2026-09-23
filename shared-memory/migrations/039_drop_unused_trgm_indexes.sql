-- Migration 039: drop the projects and project_domains trigram indexes.
-- Proposal queries filter similarity(name, $1) against a literal floor, and
-- gin_trgm_ops answers the % operator (a session GUC), not that function
-- call, so the planner seq-scans. idx_scan is 0 and stats_reset is NULL, so
-- they have never been used. pg_trgm stays: similarity() comes from it. A
-- larger table still cannot use these indexes for this predicate; the fix
-- would be the % operator or a different index.

BEGIN;

DROP INDEX IF EXISTS idx_projects_name_trgm;
DROP INDEX IF EXISTS idx_project_domains_name_trgm;

COMMIT;
