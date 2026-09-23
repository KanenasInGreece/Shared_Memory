-- Migration 025: the new_artifact notify trigger was created by hand on the
-- original deployment and never shipped, so every other install's daemon
-- listens on a channel nobody sends on. The poll still consolidates eventually;
-- this restores the prompt wake. AFTER INSERT only — an update would wake a
-- cycle for a repair of work already folded. The payload is the row id because
-- a notification is capped at 8000 bytes.

CREATE OR REPLACE FUNCTION notify_new_artifact()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  PERFORM pg_notify('new_artifact', json_build_object('pg_id', NEW.id)::text);
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_notify_new_artifact ON technical_docs;

CREATE TRIGGER trg_notify_new_artifact
    AFTER INSERT ON technical_docs
    FOR EACH ROW
    EXECUTE FUNCTION notify_new_artifact();
