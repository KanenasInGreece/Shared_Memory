-- Migration 012 (ADR-018): one row per consolidation cycle so a crash is
-- queryable state, not only a journal line. finished_at NULL means in flight.
-- eligible_* is taken before the fold, so a mid-cycle crash still records what
-- was eligible; facts older than the outbox have no created_at to anchor
-- eligible_oldest_age_seconds. The daemon is the only writer and prunes by age.

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

-- Latest row and latest success per cycle_type.
CREATE INDEX IF NOT EXISTS consolidation_runs_type_started_idx
    ON consolidation_runs (cycle_type, started_at DESC);

-- In-flight probe and the retention prune both filter on finished_at.
CREATE INDEX IF NOT EXISTS consolidation_runs_inflight_idx
    ON consolidation_runs (started_at DESC)
    WHERE finished_at IS NULL;

COMMIT;
