-- One row per dream item the gateway or a daemon refused to store.
-- Retried only when the spawn value that matches its reason has grown or
-- changed (a paid reply ends written or backlogged; fact:3396
-- measured the cut-off replies this table keeps out of the graph).
CREATE TABLE IF NOT EXISTS llm_backlog (
    item_kind        TEXT NOT NULL,
    item_key         TEXT NOT NULL,
    reason           TEXT NOT NULL,
    largest_ctx      INTEGER,
    largest_reply    INTEGER,
    pool_id          TEXT,
    boot_id          TEXT,
    specimen         TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (item_kind, item_key)
);
