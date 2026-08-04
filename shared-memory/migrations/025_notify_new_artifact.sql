-- 025 — the save→consolidation wake-up signal, which has never been in the schema.
--
-- The consolidation daemon opens a Postgres LISTEN on the `new_artifact`
-- channel and waits there for saves to arrive. Something has to send on that
-- channel, and that something is a trigger on `technical_docs`.
--
-- THE TRIGGER WAS NEVER SHIPPED. It exists on the deployment this framework was
-- developed on, where it was created by hand early on, and nowhere else. No
-- migration creates it, so it is absent from the migration chain; and
-- `schema_init.sql` is rendered by applying that chain to a scratch database,
-- so it is absent from the fresh-install path too. Every deployment other than
-- the original therefore runs a daemon that listens on a channel nobody sends
-- on.
--
-- WHAT THAT ACTUALLY COSTS. Not silence: the listener wakes on a one-second
-- poll and carries its own idle and hard-backstop thresholds, so consolidation
-- still runs eventually. What is lost is the prompt path — a save no longer
-- announces itself, and the cycle only starts when the backstop fires. The
-- system looks like it is working, slowly, for a reason nothing reports.
--
-- WHY IT WENT UNNOTICED FOR SO LONG. The object exists on the machine where
-- anyone would look for it. Introspecting the live database finds it and
-- concludes the schema is fine; only building a database from the shipped files
-- and comparing shows the gap. That is what `verify_schema_init.py` now does,
-- and this migration is the first thing it found.
--
-- Idempotent, and a no-op on the original deployment: CREATE OR REPLACE for the
-- function, DROP IF EXISTS before the trigger (Postgres has no CREATE TRIGGER
-- IF NOT EXISTS).

CREATE OR REPLACE FUNCTION notify_new_artifact()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  -- The payload carries the row id so the daemon can act without re-querying
  -- for what just changed. Postgres caps a notification payload at 8000 bytes,
  -- which is why this sends an identifier and never the content.
  PERFORM pg_notify('new_artifact', json_build_object('pg_id', NEW.id)::text);
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_notify_new_artifact ON technical_docs;

-- AFTER INSERT, and deliberately not AFTER UPDATE. The channel means "a new
-- artifact arrived", and consolidation is keyed on arrival. Firing on UPDATE
-- would also make every metadata repair or backfill look like a fresh save and
-- wake the cycle for work it has already done.
CREATE TRIGGER trg_notify_new_artifact
    AFTER INSERT ON technical_docs
    FOR EACH ROW
    EXECUTE FUNCTION notify_new_artifact();
