-- Migration 034: three columns still VARCHAR on an upgraded database become
-- TEXT, which a fresh install already has because the schema generator drops
-- the length. The change is metadata-only. A view or rule over one of these
-- columns makes Postgres refuse the type change, and this file then stops
-- with nothing half-applied.

BEGIN;

ALTER TABLE entity_registry ALTER COLUMN name          TYPE TEXT;
ALTER TABLE entity_registry ALTER COLUMN registered_by TYPE TEXT;
ALTER TABLE technical_docs  ALTER COLUMN content_hash  TYPE TEXT;

COMMIT;
