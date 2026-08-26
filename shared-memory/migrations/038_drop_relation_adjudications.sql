-- Migration 038: drop the relation adjudication ledger.
--
-- Migration 020 created `relation_adjudications` as the calibration substrate
-- for machine-minted relation edges: every machine verdict landed here with its
-- quantitative signals, the operator labelled a stratified sample, and
-- per-family reliability curves decided whether that family's edges could feed
-- synthesis.
--
-- Nothing mints a machine-asserted edge any more, so nothing is scored,
-- calibrated, reviewed or labelled: the ledger has no writer and no reader.
-- Synthesis consumes the operator's first-write edges (and legacy unstamped
-- ones) directly.
--
-- Dropping the table also drops the objects 020 built ON it — the BIGSERIAL
-- primary key's sequence and the three indexes below — but each is named
-- explicitly here so a reader of this file can see exactly what leaves, and so
-- a partially-applied 020 still converges. 020 created no trigger and no
-- function.
--
-- IDEMPOTENT: apply.py re-runs the whole chain; every statement is a no-op once
-- the objects are gone.

BEGIN;

DROP INDEX IF EXISTS relation_adjudications_entity_uniq;
DROP INDEX IF EXISTS relation_adjudications_record_uniq;
DROP INDEX IF EXISTS relation_adjudications_review_idx;

DROP TABLE IF EXISTS relation_adjudications;

DROP SEQUENCE IF EXISTS relation_adjudications_id_seq;

COMMIT;
