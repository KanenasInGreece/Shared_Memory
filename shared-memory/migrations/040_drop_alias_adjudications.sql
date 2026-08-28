-- Migration 040: drop the alias adjudication ledger.
--
-- Migration 014 created `alias_adjudications` as the verdict ledger of the
-- ADR-017 entity alias layer: the sweep embedded entity names, generated
-- cosine-near candidate PAIRS, asked an LLM whether each pair named the same
-- thing, and wrote the verdict here — as an audit trail (method, score,
-- confidence, rationale) and as a don't-re-ask idempotency cache.
--
-- Nothing has written to it since v0.8.60. Its one remaining reader was the
-- `alias` block of the `/memory/telemetry` rollup, which counted its rows and
-- split them by verdict — reporting a frozen census of a retired layer as
-- though it were current state. That block is removed in the same release, so
-- the table now has no writer and no reader at all.
--
-- ⛔ `aliases` IS A DIFFERENT TABLE AND STAYS. It is the live axis-alias
-- identity table: `project_aliases.alias_id` and `domain_aliases.alias_id`
-- both carry a FOREIGN KEY to it, and every retired project or domain
-- spelling this deployment resolves on save goes through it. Only the ADR-017
-- adjudication LEDGER leaves. `entity_embeddings`, 014's other table, stays
-- too — it is the entity-name embedding store.
--
-- Dropping the table also drops the objects 014 built ON it — the BIGSERIAL
-- primary key's sequence, the verdict index, and the index backing the
-- `UNIQUE (name_a, name_b)` constraint. The sequence and the standalone index
-- are named explicitly here so a reader of this file can see exactly what
-- leaves, and so a partially-applied 014 still converges; the unique
-- constraint's index cannot be dropped on its own (Postgres refuses to DROP
-- INDEX on a constraint-owned index) and goes with the table. 014 created no
-- trigger and no function on this table.
--
-- IDEMPOTENT: apply.py re-runs the whole chain; every statement is a no-op
-- once the objects are gone.

BEGIN;

DROP INDEX IF EXISTS alias_adjudications_verdict_idx;

DROP TABLE IF EXISTS alias_adjudications;

DROP SEQUENCE IF EXISTS alias_adjudications_id_seq;

COMMIT;
