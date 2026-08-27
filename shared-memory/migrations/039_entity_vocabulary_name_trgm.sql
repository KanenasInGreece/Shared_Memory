-- Migration 039: a trigram index on the entity vocabulary's canonical names.
--
-- WHY. From v0.9.69 the save-time entity gate refuses a `new_entities` mint
-- whose name is CONFUSABLE with a name the vocabulary already holds, unless the
-- caller confirms it is genuinely distinct — the same rule the project registry
-- has enforced since 022 and the domain registry since 028. A refusal is only
-- usable if it can say WHICH existing names it is worried about, and that
-- proposal query is a trigram similarity scan over `entity_vocabulary.name`.
--
-- Without this index the scan is sequential. `entity_vocabulary` is small today,
-- so the index is not what makes the query possible — it is what keeps the
-- query's cost from growing with the vocabulary, on a path (`/memory/save`) that
-- is already writing to two stores. Exactly the reasoning 022 recorded for
-- `idx_projects_name_trgm` and 028 for `idx_project_domains_name_trgm`.
--
-- WHAT IS NOT INDEXED, and why. `entity_vocab_aliases.alias` is scanned by the
-- same proposal query without a trigram index of its own. An alias is written
-- only by a manual, operator-only curation act (decision:1380), so the table is
-- the smaller of the two by construction and gains a row only when a human adds
-- one. If that ever stops being true, the fix is another index here — and it
-- should be driven by a measurement of the alias table's size, not by symmetry.
--
-- `pg_trgm` is already installed (migration 022 creates the extension for
-- `idx_projects_name_trgm`); this migration does not re-create it, and would
-- fail loudly rather than silently if the chain were applied out of order.
--
-- IDEMPOTENT: apply.py re-runs the whole chain; `IF NOT EXISTS` makes this a
-- no-op once the index is present.

BEGIN;

CREATE INDEX IF NOT EXISTS idx_entity_vocabulary_name_trgm
    ON entity_vocabulary USING gin (name gin_trgm_ops);

COMMIT;
