-- Migration 023: a ledger for one-way project promotions. Without it, a
-- promoted metadata field looks like a value supplied at first write, and a
-- wrong promotion cannot be undone on the supported path. The CHECKs require
-- a parked source and a real registered target; Python is not the only gate.

CREATE TABLE IF NOT EXISTS project_promotions (
    id           bigserial PRIMARY KEY,
    pg_id        bigint      NOT NULL,
    -- NULL means no project; the sentinel means explicitly parked. Both are parked.
    from_project text,
    to_project   text        NOT NULL REFERENCES projects (name),
    -- Free text: a new basis should not need a migration.
    method       text        NOT NULL,
    actor        text        NOT NULL,
    -- Evidence for the one-way write, such as the judgement pg_ids it came from.
    note         text,
    created_at   timestamptz NOT NULL DEFAULT now(),

    -- A real-to-real move is not a promotion this ledger may record.
    CONSTRAINT project_promotions_from_parked
        CHECK (from_project IS NULL OR from_project = 'general_discussion'),
    -- Promoting to the sentinel is parking, not a promotion.
    CONSTRAINT project_promotions_to_real
        CHECK (btrim(to_project) <> '' AND to_project <> 'general_discussion')
);

CREATE INDEX IF NOT EXISTS idx_project_promotions_pg_id
    ON project_promotions (pg_id);

CREATE INDEX IF NOT EXISTS idx_project_promotions_created_at
    ON project_promotions (created_at DESC);
