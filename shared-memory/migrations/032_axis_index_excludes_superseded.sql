-- 032 — axis unique index excludes superseded rows (Dreaming Cycle Plan to
-- v2, §6 C3.1, defect F0 — the resurrection gap).
--
-- WHAT WAS WRONG. Migration 029's partial unique index
--   community_summaries_axis_level_unique
-- on (entity, project, domain, level) WHERE kind <> 'insight' does NOT
-- exclude retired (superseded = true) rows. The thematic fold's
-- INSERT ... ON CONFLICT ... DO UPDATE (consolidation_loop.py's
-- _write_summary) therefore matches a lineage-RETIRED row on the SAME axis
-- key and updates it in place — content/metadata/source_pg_ids change but
-- `superseded`/`superseded_at`/`superseded_reason` are never touched, so the
-- row STAYS superseded and becomes permanently invisible to retrieval.
-- close_refold_ledger_rows's 'refolded' branch (`NOT cs.superseded`) can
-- then never close the ledger rows it opened either, because the row that
-- would cover them never returns to active.
--
-- THE FIX. Per plan §4.2 Path A step 4 — a NEW summary supersedes the old;
-- never resurrect one in place. Rebuild the index with an added
-- `AND NOT superseded` predicate. A retired row no longer participates in
-- the ON CONFLICT arbiter (its matching WHERE clause in
-- consolidation_loop.py's _write_summary is updated in the same change), so
-- the next fold on that axis key INSERTs a fresh ACTIVE row instead of
-- UPDATE-ing the retired one. The retired row remains, unmodified, as
-- history — this migration writes no data, it only rebuilds one index.
--
-- Idempotent: DROP INDEX IF EXISTS + CREATE UNIQUE INDEX IF NOT EXISTS, one
-- transaction. Safe to re-run.
--
-- NOT done here: no graph-side SUPERSEDES edge is drawn between the retired
-- and the new summary — that stays parked with the §0/RESUME Tier-3
-- supersession item, recorded as owed, per plan §6 C3.1 F0.

BEGIN;

DROP INDEX IF EXISTS community_summaries_axis_level_unique;

CREATE UNIQUE INDEX IF NOT EXISTS community_summaries_axis_level_unique
    ON community_summaries (
        (COALESCE(metadata->>'entity', '')),
        (COALESCE(metadata->>'project', '')),
        (COALESCE(metadata->>'domain', '')),
        (COALESCE(metadata->>'level', 'entity'))
    )
    WHERE COALESCE(metadata->>'kind', 'thematic') <> 'insight'
      AND NOT superseded;

COMMIT;
