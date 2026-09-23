-- Migration 013 (decision 381, refined by 384): superseded_by lets search
-- annotate stale_sources with a Postgres join. NULL means the row is live, or
-- retracted with no replacement; ON DELETE SET NULL avoids a dangling id.

ALTER TABLE technical_docs
    ADD COLUMN IF NOT EXISTS superseded_by integer
        REFERENCES technical_docs(id) ON DELETE SET NULL;

-- Only rows that actually point somewhere.
CREATE INDEX IF NOT EXISTS technical_docs_superseded_by_idx
    ON technical_docs (superseded_by)
    WHERE superseded_by IS NOT NULL;
