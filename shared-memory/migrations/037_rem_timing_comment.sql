-- Migration 037: re-issue technical_docs.rem_timing's COMMENT ON COLUMN,
-- because the writer's shape has grown since migration 019 wrote it.
--
-- WHY THIS EXISTS. Migration 019 (decisions 570/571) commented rem_timing
-- with the shape {service_ms,wall_ms,contention_ms,poll_ms,model,backend,
-- batch_size,prompt_chars,ts} — llama.cpp's own `timings` block, plus the
-- caller-filled poll_ms. Since 0.9.60 the writer (dream_telemetry.py's
-- record_llm_call ~293-305) also writes completion_tokens and tok_s_wall
-- (fact:1621): an OpenAI-compatible fallback pair for backends that return no
-- llama.cpp `timings` block at all (external, non-llama.cpp providers) — so
-- the comment describing the column's contract went stale the moment those
-- two keys started landing in it. Per ruling decision:1624 (F5), the fix is
-- a NEW migration that re-issues the comment with the current shape, never
-- an in-place edit of 019 — 019 is history and stays exactly as it ran.
--
-- Current shape (JSONB, one object per REM'd fact; a key is null when its
-- source input was unavailable for that call):
--   service_ms         llama.cpp `timings`-only: pure inference (prompt_ms +
--                       predicted_ms) = MODEL + HARDWARE, load-invariant.
--   wall_ms            client-observed call time (service + contention + network).
--   contention_ms      max(0, wall_ms - service_ms) — queue behind a busy
--                       backend = CAPACITY signal.
--   poll_ms            created_at -> REM pickup: daemon cadence.
--   model              LLM model tag from the response — model-evolution axis.
--   backend            X-SM-LLM-Backend the gateway pool routed to.
--   batch_size         facts sharing this one LLM call.
--   prompt_chars       grounding-prompt size (cross-model comparability).
--   completion_tokens  (fact:1621) usage.completion_tokens from an OpenAI-
--                       compatible response — the fallback input for a
--                       backend that sends no llama.cpp `timings` block.
--   tok_s_wall         (fact:1621) completion_tokens / wall_s — an EFFECTIVE
--                       rate that includes TTFT and network time, unlike
--                       service_ms's pure-inference rate. Present precisely
--                       where the server gives no timings, i.e. exactly
--                       where service_ms/contention_ms are NULL.
--   ts                 epoch seconds when the call completed.
--
-- service_ms and contention_ms are NULL for any backend that does not return
-- llama.cpp's `timings` block — completion_tokens/tok_s_wall is what such a
-- call reports instead, not a second measurement of the same thing.
--
-- schema_init.sql carries no COMMENT ON statements at all (a fresh install
-- gets the column but not this description) — this migration is how a
-- deployed system's information_schema stays truthful; the durable prose
-- description lives in Documentation/schema.md instead (updated alongside
-- this migration).
--
-- Idempotent: COMMENT ON COLUMN unconditionally replaces the prior comment
-- text (there is no "IF NOT EXISTS" form for comments) — safe to re-run.

BEGIN;

COMMENT ON COLUMN technical_docs.rem_timing IS
  'Durable REM per-call latency summary (decisions 570/571; fact:1621): {service_ms,wall_ms,contention_ms,poll_ms,model,backend,batch_size,prompt_chars,completion_tokens,tok_s_wall,ts}. service_ms=model/hardware, contention_ms=capacity (both NULL for backends without llama.cpp timings). completion_tokens/tok_s_wall are the OpenAI-compatible fallback: tok_s_wall is an EFFECTIVE rate (completion_tokens/wall_s) including TTFT+network, present precisely where service_ms/contention_ms are NULL. Survives neo4j_outbox deletion on NREM consolidation.';

COMMIT;
