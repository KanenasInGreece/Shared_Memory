-- 034 — align legacy VARCHAR(n) columns with the TEXT a fresh install gets.
--
-- WHAT WAS WRONG. generate_schema_init.py renders every `character varying`
-- column as TEXT, dropping the length — so a fresh install built from
-- schema_init.sql has always been LOOSER than an upgraded one: the migrated
-- database refuses a value the fresh database accepts. Invisible until
-- verify_schema_init.py learned to compare column types (033's review), at
-- which point the three legacy columns still carrying VARCHAR on upgraded
-- installs surfaced immediately.
--
-- WHAT THIS DOES. Converts those columns to TEXT, the type every fresh
-- install already has and the type the rest of this schema standardised on
-- (033 ruled TEXT for exactly this reason: a bounded column that narrows its
-- own unbounded sources turns a legal value into an abort).
--
-- VARCHAR(n) → TEXT is binary-coercible: a metadata-only change, no table
-- rewrite, no index rebuild. Idempotent — re-running sets the same type.
--
-- ⚠ If YOUR deployment added a VIEW or RULE over any of these three columns,
-- Postgres refuses to alter the column type and this migration halts (cleanly
-- and atomically — nothing half-applied). Drop the view, run the migration,
-- recreate the view.

BEGIN;

ALTER TABLE entity_registry ALTER COLUMN name          TYPE TEXT;
ALTER TABLE entity_registry ALTER COLUMN registered_by TYPE TEXT;
ALTER TABLE technical_docs  ALTER COLUMN content_hash  TYPE TEXT;

COMMIT;
