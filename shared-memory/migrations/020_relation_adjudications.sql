-- Migration 020: relation adjudication ledger (REM rebuild — decisions 718/726/727)
--
-- One ledger backing BOTH machine-minted relation families:
--
--   entity_relation — typed Entity→Entity edges (DEPENDS_ON/PART_OF/…) minted by
--     the evidence sweep (relation_sweep.py) and by per-record REM enrichment.
--     Endpoints are entity NAMES (Entity identity is name-keyed).
--
--   evidential — record→record evidential proposals (e.g. Decision INFORMED_BY
--     Fact) proposed by REM below the consumption threshold and re-scored by the
--     adjudication pass. Endpoints are record PG_IDs. GROUNDED_IN is never
--     machine-minted, so it never appears in this family.
--
-- The ledger is the calibration substrate (decision 726 §5): every machine
-- verdict lands here with its quantitative signals, the operator labels rows
-- via the review-edges flow (operator_label), and per-family reliability curves
-- are computed FROM these labels — a family's thresholds act only once it is
-- calibrated. It is also the audit trail and the don't-re-ask idempotency
-- cache, mirroring alias_adjudications (migration 014).
--
-- One row per (family, endpoints, rel_type): the evidential ladder re-SCORES a
-- proposal in place (method rem_k3 → llm_sweep; the earlier vote share is
-- preserved inside signals), so the row always shows the edge's CURRENT verdict
-- while signals retain the rung history.
--
-- IDEMPOTENT: apply.py re-runs the whole chain; every statement is a no-op once
-- the objects exist.

BEGIN;

CREATE TABLE IF NOT EXISTS relation_adjudications (
    id                  BIGSERIAL PRIMARY KEY,
    family              TEXT NOT NULL CHECK (family IN ('entity_relation', 'evidential')),
    -- entity_relation endpoints (name-keyed; canonical: as stored on the edge, src→tgt directed)
    src_name            TEXT,
    tgt_name            TEXT,
    -- evidential endpoints (pg_id-keyed records)
    src_pg_id           BIGINT,
    tgt_pg_id           BIGINT,
    rel_type            TEXT NOT NULL,
    verdict             TEXT NOT NULL CHECK (verdict IN ('accept', 'reject')),
    method              TEXT NOT NULL CHECK (method IN ('llm_sweep', 'rem_k3', 'operator')),
    confidence          REAL CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    support             TEXT CHECK (support IS NULL OR support IN ('text_only', 'graph_evidence')),
    signals             JSONB,          -- co-occurrence count, sub-labels, vote share, external labels, …
    rationale           TEXT,           -- short LLM justification (audit)
    model               TEXT,           -- adjudicating model identifier
    run_id              TEXT,           -- sweep / REM run correlation id
    -- calibration surface: the operator is the oracle (decision 727 — per-family curves)
    operator_label      TEXT CHECK (operator_label IS NULL OR operator_label IN ('correct', 'incorrect')),
    operator_labeled_at TIMESTAMPTZ,
    promoted_at         TIMESTAMPTZ,    -- operator promotion → edge asserted_by='operator'
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- exactly one endpoint encoding per family
    CHECK (
        (family = 'entity_relation' AND src_name  IS NOT NULL AND tgt_name  IS NOT NULL
                                    AND src_pg_id IS NULL     AND tgt_pg_id IS NULL)
     OR (family = 'evidential'      AND src_pg_id IS NOT NULL AND tgt_pg_id IS NOT NULL
                                    AND src_name  IS NULL     AND tgt_name  IS NULL)
    )
);

-- Don't-re-ask idempotency: one CURRENT row per directed edge per family.
CREATE UNIQUE INDEX IF NOT EXISTS relation_adjudications_entity_uniq
    ON relation_adjudications (family, src_name, tgt_name, rel_type)
    WHERE family = 'entity_relation';

CREATE UNIQUE INDEX IF NOT EXISTS relation_adjudications_record_uniq
    ON relation_adjudications (family, src_pg_id, tgt_pg_id, rel_type)
    WHERE family = 'evidential';

-- Review-flow + calibration reads: unlabeled sample per family; label counts.
CREATE INDEX IF NOT EXISTS relation_adjudications_review_idx
    ON relation_adjudications (family, operator_label, created_at);

COMMIT;
