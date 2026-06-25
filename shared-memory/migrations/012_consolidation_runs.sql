-- Migration 012: consolidation_runs — the dream-cycle liveness/coverage ledger (ADR-018)
--
-- Until now a consolidation/insight fold's outcome was LOGGED, not STATED: a
-- crash in run_insight_cycle surfaced only as an hourly ERROR line in journald,
-- which is how `insight = 0` ran ~12 days unnoticed (the _fold_insight projects=
-- kwarg crash). Everything else in /memory/telemetry is derived from live DB
-- state; a crashed cycle leaves nothing derivable. This table is the one piece
-- of PERSISTED state that turns a fold outcome into queryable state.
--
-- One row per cycle (NOT per fold — per-fold quality rows are family C, later):
--   cycle_type    'insight' | 'fact_consolidation'
--   started_at    set at cycle entry; finished_at NULL ⇒ in-flight (not stalled)
--   outcome       'completed' | 'crashed' | 'deferred'
--   folds_*       attempted/succeeded/failed within the cycle (succeeded>0 row =
--                 a real consolidation; drives last_success_age)
--   eligible_*    coverage census captured at gate-time, BEFORE folding, so a
--                 crash mid-fold still records what was eligible (PR-2/family B;
--                 eligible_oldest_age_seconds NULL-safe — facts predating the
--                 outbox migration have no neo4j_outbox.created_at to anchor on)
--   error_*       populated on 'crashed'
--   extra         defer reason; room for family C (max_cosine, fold shape)
--
-- Self-pruning: the daemon DELETEs rows past CONSOLIDATION_RUNS_RETENTION_DAYS at
-- startup. Writes are ~hourly (one per sweep) so volume is trivial.
--
-- The coordinator reads/rolls this up into /memory/telemetry and a cached
-- /health.consolidation snapshot. The daemon is the sole writer.
--
-- Idempotent: CREATE TABLE / INDEX IF NOT EXISTS — safe to re-run every apply.py.

BEGIN;

CREATE TABLE IF NOT EXISTS consolidation_runs (
    id                          BIGSERIAL PRIMARY KEY,
    cycle_type                  TEXT        NOT NULL,
    started_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at                 TIMESTAMPTZ,
    outcome                     TEXT,
    folds_attempted             INTEGER     NOT NULL DEFAULT 0,
    folds_succeeded             INTEGER     NOT NULL DEFAULT 0,
    folds_failed                INTEGER     NOT NULL DEFAULT 0,
    eligible_clusters           INTEGER,
    eligible_oldest_age_seconds INTEGER,
    error_class                 TEXT,
    error_msg                   TEXT,
    extra                       JSONB
);

-- Liveness reads are "latest row per cycle_type" and "latest success per
-- cycle_type" — both served by a descending (cycle_type, started_at) scan.
CREATE INDEX IF NOT EXISTS consolidation_runs_type_started_idx
    ON consolidation_runs (cycle_type, started_at DESC);

-- In-flight probe (finished_at IS NULL) and the retention prune both filter on
-- finished_at; a partial index keeps the in-flight lookup O(open rows).
CREATE INDEX IF NOT EXISTS consolidation_runs_inflight_idx
    ON consolidation_runs (started_at DESC)
    WHERE finished_at IS NULL;

COMMIT;
