-- Migration 020 (decisions 718/726/727): one ledger for machine-minted
-- entity_relation edges (name endpoints) and evidential record edges (pg_id
-- endpoints). GROUNDED_IN is never machine-minted. A proposal is re-scored in
-- place; operator_label is the calibration oracle, and thresholds wait on it.

BEGIN;

CREATE TABLE IF NOT EXISTS relation_adjudications (
    id                  BIGSERIAL PRIMARY KEY,
    family              TEXT NOT NULL CHECK (family IN ('entity_relation', 'evidential')),
    -- entity_relation endpoints, directed as stored on the edge.
    src_name            TEXT,
    tgt_name            TEXT,
    src_pg_id           BIGINT,
    tgt_pg_id           BIGINT,
    rel_type            TEXT NOT NULL,
    verdict             TEXT NOT NULL CHECK (verdict IN ('accept', 'reject')),
    method              TEXT NOT NULL CHECK (method IN ('llm_sweep', 'rem_k3', 'operator')),
    confidence          REAL CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    support             TEXT CHECK (support IS NULL OR support IN ('text_only', 'graph_evidence')),
    -- Co-occurrence, vote share, and earlier rungs survive an in-place re-score.
    signals             JSONB,
    rationale           TEXT,
    model               TEXT,
    run_id              TEXT,
    operator_label      TEXT CHECK (operator_label IS NULL OR operator_label IN ('correct', 'incorrect')),
    operator_labeled_at TIMESTAMPTZ,
    -- When the operator promotes the row, the edge is asserted_by='operator'.
    promoted_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (family = 'entity_relation' AND src_name  IS NOT NULL AND tgt_name  IS NOT NULL
                                    AND src_pg_id IS NULL     AND tgt_pg_id IS NULL)
     OR (family = 'evidential'      AND src_pg_id IS NOT NULL AND tgt_pg_id IS NOT NULL
                                    AND src_name  IS NULL     AND tgt_name  IS NULL)
    )
);

-- One current row per directed edge. A second encoding would re-ask the model.
CREATE UNIQUE INDEX IF NOT EXISTS relation_adjudications_entity_uniq
    ON relation_adjudications (family, src_name, tgt_name, rel_type)
    WHERE family = 'entity_relation';

CREATE UNIQUE INDEX IF NOT EXISTS relation_adjudications_record_uniq
    ON relation_adjudications (family, src_pg_id, tgt_pg_id, rel_type)
    WHERE family = 'evidential';

-- Unlabeled sample and label counts, per family.
CREATE INDEX IF NOT EXISTS relation_adjudications_review_idx
    ON relation_adjudications (family, operator_label, created_at);

COMMIT;
