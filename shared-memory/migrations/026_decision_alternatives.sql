-- 026 — the alternatives a decision considered, one row and one vector each.
--
-- A decision already stores the options it weighed, as a JSON array under
-- `metadata.decision.alternatives`. That is the right home for them and this
-- migration does not move it. What the array cannot do is answer the question
-- the alternatives are actually interesting for: WHICH DECISIONS CONSIDERED THE
-- SAME THING? Two decisions that both weighed "one vector per record versus one
-- per fragment" are related whatever their headline says, and nothing in the
-- system can see that today, because a decision has exactly one embedding and it
-- is dominated by the decision's own text.
--
-- So each alternative becomes a row with its own vector, and `decision_pg_id`
-- carries the similarity back to the pair of DECISIONS — which is the answer
-- wanted, not a list of similar sentences.
--
-- POSTGRES ONLY. No node, no entity, no graph half. A node per alternative
-- would mint mostly-singleton nodes named with free prose, which is precisely
-- the noise class the project-axis work spent three releases removing. The
-- graph already carries a projected copy of `d.alternatives` that no Cypher
-- filters or orders on; this table does not add a second one.
--
-- THE EMBEDDING IS FILLED ASYNCHRONOUSLY, AND THAT IS WHY IT IS NULLABLE.
-- Embedding N alternatives on the save path would put N network calls in front
-- of every decision write to build a secondary index — while the decision's own
-- vector, the one that makes the record findable at all, is already written
-- synchronously under the hard-embedding mandate. A missing alternative vector
-- costs a grouping that can be recomputed; a missing decision vector costs the
-- record. They do not deserve the same guarantee.
--
-- What the async choice DOES owe is that nothing is ever stranded, and that
-- obligation is discharged by shape rather than by a promise: the row is written
-- in the save's own transaction with `embedding IS NULL`, and the populator
-- selects exactly those rows. THE TABLE IS THE QUEUE. A crash, a restart, or a
-- reboot between the write and the embed leaves a pending row that the next
-- sweep finds, because the pending state was never held in a process.
--
-- `attempts`/`next_attempt_at`/`last_error` mirror `neo4j_outbox`: an input the
-- embedder keeps rejecting must back off and then stop, or one bad row spins
-- forever and every sweep pays for it.

CREATE TABLE IF NOT EXISTS decision_alternatives (
    id             bigserial PRIMARY KEY,

    -- CASCADE because an alternative has no meaning without its decision. This
    -- is the FK shape `generate_schema_init.py` has silently dropped before
    -- (v0.8.36), which is why `verify_schema_init.py` runs after this migration.
    decision_pg_id bigint      NOT NULL REFERENCES technical_docs (id) ON DELETE CASCADE,

    -- Position in the decision's own array, 0-based. It is part of the identity
    -- rather than decoration: reconciliation compares (ordinal, text) so an
    -- edited alternative can be told from a reordered one, and only what
    -- actually changed is re-embedded.
    ordinal        integer     NOT NULL,
    text           text        NOT NULL,

    -- NULL means PENDING, never "this one has no vector". Nothing else may
    -- write NULL here, and nothing may read the column as a terminal state.
    embedding      vector(1024),
    embedded_at    timestamptz,

    attempts       integer     NOT NULL DEFAULT 0,
    last_error     text,
    next_attempt_at timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT decision_alternatives_text_not_blank CHECK (btrim(text) <> ''),
    CONSTRAINT decision_alternatives_ordinal_nonneg CHECK (ordinal >= 0),

    -- An embedded row must say when, and a pending row must not claim to have
    -- been embedded. Without this the pending set is defined by whichever of the
    -- two columns a given query happened to test.
    CONSTRAINT decision_alternatives_embedded_consistent
        CHECK ((embedding IS NULL) = (embedded_at IS NULL))
);

-- One row per (decision, position). The reconciler's ON CONFLICT target, and
-- the reason a re-save cannot accumulate duplicate alternatives.
CREATE UNIQUE INDEX IF NOT EXISTS decision_alternatives_decision_ordinal_idx
    ON decision_alternatives (decision_pg_id, ordinal);

CREATE INDEX IF NOT EXISTS decision_alternatives_decision_idx
    ON decision_alternatives (decision_pg_id);

-- The populator's work queue. Partial, because the pending set is a vanishing
-- fraction of the table in steady state and a full-table scan every sweep would
-- grow with the corpus rather than with the backlog.
CREATE INDEX IF NOT EXISTS decision_alternatives_pending_idx
    ON decision_alternatives (next_attempt_at NULLS FIRST) WHERE embedding IS NULL;

CREATE INDEX IF NOT EXISTS decision_alternatives_embedding_idx
    ON decision_alternatives USING hnsw (embedding vector_cosine_ops);

-- NO BACKFILL HERE, DELIBERATELY. This migration creates the table and stops.
--
-- Two reasons, and the second is the one that matters on a deployment other
-- than the one this was written on:
--
--   * A BACKFILL IS A DATA OPERATION, NOT SCHEMA. Seeding rows from records that
--     already exist is calibrated on a corpus — how many decisions it touches
--     and what it costs to embed them depends entirely on whose database it
--     runs against. Schema travels; data operations do not.
--   * IT WOULD HIDE THE FIRST DEFECT IT CAUSED. Filling the table at migration
--     time means the populator's first sweep is a bulk run, and bulk work looks
--     nothing like the steady state it has to be verified in. The write path is
--     proven on a real save first, against a table that starts empty and gains
--     rows only from live decisions; only then is history seeded.
--
-- An existing deployment upgrading to this version therefore has a table that
-- fills forward from the next decision saved, and can seed its history whenever
-- it chooses — the reconciler makes that safe to run at any time, because it
-- converges on the decision's own array rather than appending to what it finds.
