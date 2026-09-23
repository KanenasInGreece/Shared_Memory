-- Migration 038: nothing mints machine-asserted relation edges anymore, so
-- the calibration ledger from 020 has no writer and no reader. The sequence
-- and indexes are named so a partial 020 still converges. 020 created no
-- trigger or function.

BEGIN;

DROP INDEX IF EXISTS relation_adjudications_entity_uniq;
DROP INDEX IF EXISTS relation_adjudications_record_uniq;
DROP INDEX IF EXISTS relation_adjudications_review_idx;

DROP TABLE IF EXISTS relation_adjudications;

DROP SEQUENCE IF EXISTS relation_adjudications_id_seq;

COMMIT;
