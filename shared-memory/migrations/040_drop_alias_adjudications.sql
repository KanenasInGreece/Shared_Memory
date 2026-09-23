-- Migration 040: alias_adjudications has had no writer since v0.8.60, and its
-- last reader (the telemetry census) leaves in the same release. The aliases
-- table stays: project and domain alias foreign keys point at it.
-- entity_embeddings stays too; it is the entity-name embedding store. The
-- unique constraint's index goes with the table; Postgres will not drop a
-- constraint-owned index on its own.

BEGIN;

DROP INDEX IF EXISTS alias_adjudications_verdict_idx;

DROP TABLE IF EXISTS alias_adjudications;

DROP SEQUENCE IF EXISTS alias_adjudications_id_seq;

COMMIT;
