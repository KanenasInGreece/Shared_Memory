-- Migration 026: each alternative gets its own vector so two decisions that
-- weighed the same option can be found when their headline embeddings differ.
-- Postgres only — a graph node per free-text option is the noise the project
-- axis removed. embedding NULL means pending: the row is written in the save
-- transaction and is the queue, so a crash leaves it for the next sweep.
-- attempts and next_attempt_at mirror the outbox so one bad row cannot spin.
-- No backfill. Seeding history is a data operation, and a bulk first sweep
-- would hide whether the live write path works. The reconciler can seed later.

CREATE TABLE IF NOT EXISTS decision_alternatives (
    id             bigserial PRIMARY KEY,

    -- CASCADE: an alternative means nothing without its decision. This is the
    -- FK shape the schema generator has dropped before.
    decision_pg_id bigint      NOT NULL REFERENCES technical_docs (id) ON DELETE CASCADE,

    -- 0-based position in the decision's array. The reconciler tells an edit
    -- from a reorder by (ordinal, text), and only a real change is re-embedded.
    ordinal        integer     NOT NULL,
    text           text        NOT NULL,

    -- NULL means pending, never a terminal "no vector".
    embedding      vector(1024),
    embedded_at    timestamptz,

    attempts       integer     NOT NULL DEFAULT 0,
    last_error     text,
    next_attempt_at timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT decision_alternatives_text_not_blank CHECK (btrim(text) <> ''),
    CONSTRAINT decision_alternatives_ordinal_nonneg CHECK (ordinal >= 0),

    -- Pending is defined by both columns, not whichever query remembered to test.
    CONSTRAINT decision_alternatives_embedded_consistent
        CHECK ((embedding IS NULL) = (embedded_at IS NULL))
);

-- The reconciler's conflict target. A re-save must not stack duplicate rows.
CREATE UNIQUE INDEX IF NOT EXISTS decision_alternatives_decision_ordinal_idx
    ON decision_alternatives (decision_pg_id, ordinal);

CREATE INDEX IF NOT EXISTS decision_alternatives_decision_idx
    ON decision_alternatives (decision_pg_id);

-- The populator's queue. Partial so a sweep tracks the backlog, not the corpus.
CREATE INDEX IF NOT EXISTS decision_alternatives_pending_idx
    ON decision_alternatives (next_attempt_at NULLS FIRST) WHERE embedding IS NULL;

CREATE INDEX IF NOT EXISTS decision_alternatives_embedding_idx
    ON decision_alternatives USING hnsw (embedding vector_cosine_ops);
