-- Migration 013: fact supersession — superseded_by pointer (decision 381, refined by 384)
--
-- technical_docs.superseded (migration 009) already exists and the Tier-1 search
-- already excludes superseded rows (coordinator.py:1493). Until now only a
-- 'reversed' retrospective set that flag, and only on DECISION rows. Decision 381
-- extends the same soft-supersede lifecycle to plain facts via `save --supersedes`;
-- decision 384 makes propagation LAZY — resolved at retrieval, not eagerly at write.
--
-- This migration adds the one piece of state the read-time check needs: a pointer
-- from the superseded row to its successor, so search can annotate
-- stale_sources:[{old:X, superseded_by:X'}] as a pure Postgres join with no Neo4j
-- hop. NULL ⇒ the row is live, OR it was retracted with no replacement (superseded
-- =true, superseded_by=NULL). The SUPERSEDED_BY Neo4j edge (written via the outbox)
-- remains the graph-side mirror for multi-hop lineage traversal.
--
-- superseded_by is a self-referential FK kept ON DELETE SET NULL so deleting a
-- successor never orphans the pointer into a dangling id.
--
-- Idempotent: safe to re-run.

ALTER TABLE technical_docs
    ADD COLUMN IF NOT EXISTS superseded_by integer
        REFERENCES technical_docs(id) ON DELETE SET NULL;

-- Partial index: the read-time check only ever filters on superseded rows, and
-- supersessions are rare relative to the table, so index just the live pointers.
CREATE INDEX IF NOT EXISTS technical_docs_superseded_by_idx
    ON technical_docs (superseded_by)
    WHERE superseded_by IS NOT NULL;
