-- 023 — the promotions ledger.
--
-- A record whose project could not be established at first write is PARKED: it
-- saves, searches and enriches normally, and simply never folds as a subject.
-- Establishing that project later is a state transition, and this table is its
-- durable record — who asked, on what basis, and what the value was before.
--
-- WHY A LEDGER AT ALL. The transition is one-way by design: the writer refuses a
-- record that already carries a real project, because a second writer on a
-- gating property is how a value silently changes meaning. One-way means a wrong
-- promotion cannot be undone through the supported path, so the evidence for
-- every promotion has to outlive the write itself. Without this table the only
-- trace would be a metadata field that looks exactly like a value supplied at
-- first write — indistinguishable from a record that was never parked at all.
--
-- THE CHECKS ARE THE INVARIANT, IN THE SCHEMA. An irreversible step must assert
-- its own assumptions rather than trust the caller to have checked: that is what
-- saved this schema when a migration was re-run against a shape it was never
-- written for. So the transition's two halves are constraints here, not just
-- conditions in Python — the source must be a parked value, and the target must
-- be a real, registered project.

CREATE TABLE IF NOT EXISTS project_promotions (
    id           bigserial PRIMARY KEY,
    pg_id        bigint      NOT NULL,
    -- NULL when the record carried no project at all; the sentinel when it was
    -- explicitly parked. Both are "parked"; keeping them distinct preserves
    -- which of the two a record actually came from.
    from_project text,
    to_project   text        NOT NULL REFERENCES projects (name),
    -- How the value was established (grounding inheritance, operator
    -- confirmation, reconciliation, …). Free text rather than an enum: a new
    -- basis for a promotion should not need a migration, and the ledger's job is
    -- to record what happened, not to bound it in advance.
    method       text        NOT NULL,
    -- Who asked. The agent identity for an automated caller, the operator's
    -- identity for a confirmed repair.
    actor        text        NOT NULL,
    -- Free-text evidence: for grounding inheritance, the judgement pg_ids the
    -- value came from. This is the part that makes a one-way write auditable
    -- after the fact.
    note         text,
    created_at   timestamptz NOT NULL DEFAULT now(),

    -- The source of a promotion is always a PARKED value. A row claiming a real
    -- project was promoted to another real project is a rule violation, and the
    -- database refuses to record one.
    CONSTRAINT project_promotions_from_parked
        CHECK (from_project IS NULL OR from_project = 'general_discussion'),
    -- The target is always a REAL project: never blank, and never the sentinel.
    -- Promoting to the sentinel is not a promotion, it is parking, and the
    -- foreign key above already makes an unregistered name impossible.
    CONSTRAINT project_promotions_to_real
        CHECK (btrim(to_project) <> '' AND to_project <> 'general_discussion')
);

CREATE INDEX IF NOT EXISTS idx_project_promotions_pg_id
    ON project_promotions (pg_id);

CREATE INDEX IF NOT EXISTS idx_project_promotions_created_at
    ON project_promotions (created_at DESC);
