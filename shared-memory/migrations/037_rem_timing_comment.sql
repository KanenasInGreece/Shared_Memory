-- Migration 037: re-issue technical_docs.rem_timing's COMMENT ON COLUMN,
-- because the writer's shape has grown since migration 019 wrote it.
--
-- WHY THIS EXISTS. Migration 019 (decisions 570/571) commented rem_timing
-- with the shape {service_ms,wall_ms,contention_ms,poll_ms,model,backend,
-- batch_size,prompt_chars,ts}. Since 0.9.60 the writer (dream_telemetry.py's
-- call_timing_summary, ~243-305, called from rem_loop.py and persisted by
-- its _write_rem_timing) also writes completion_tokens and tok_s_wall
-- (fact:1621): an OpenAI-compatible fallback pair, read from
-- the response's `usage.completion_tokens`, for backends that return no
-- llama.cpp `timings` block at all (external, non-llama.cpp providers) — so
-- the comment describing the column's contract went stale the moment those
-- two keys started landing in it. Per ruling decision:1624 (F5), the fix is
-- a NEW migration that re-issues the comment with the current shape, never
-- an in-place edit of 019 — 019 is history and stays exactly as it ran.
--
-- Current shape (JSONB, one object per REM'd fact; a key is null when its
-- source input was unavailable for that call). Checked against the writer
-- (dream_telemetry.py) rather than assumed from the key names:
--   service_ms         llama.cpp `timings`-only: pure inference (prompt_ms +
--                       predicted_ms) = MODEL + HARDWARE, load-invariant.
--   wall_ms            client-observed call time (service + contention + network).
--   contention_ms      max(0, wall_ms - service_ms) — queue behind a busy
--                       backend = CAPACITY signal.
--   poll_ms            created_at -> REM pickup: daemon cadence.
--   model, backend,    the CALL'S OWN ENVELOPE, not llama.cpp `timings`
--   batch_size,        output: `model` is read from the response's own
--   prompt_chars       top-level `model` field; `backend`/`batch_size`/
--                      `prompt_chars` are supplied by the CALLER (the gateway
--                      pool route, the fold batch, the grounding-prompt
--                      size) — none of the four come from inside `timings`.
--   completion_tokens  (fact:1621) usage.completion_tokens from an OpenAI-
--                       compatible response — the fallback input for a
--                       backend that sends no llama.cpp `timings` block.
--   tok_s_wall         (fact:1621) completion_tokens / wall_s — an EFFECTIVE
--                       rate that includes TTFT and network time, unlike
--                       service_ms's pure-inference rate. This is the
--                       fallback rate for a backend whose response carries
--                       no `timings` block; it is filled from `usage.
--                       completion_tokens` whenever the response carries
--                       THAT (unconditionally, independent of whether
--                       `timings` is also present) — so it MAY also be
--                       present beside a non-null service_ms/contention_ms
--                       on a response that happens to carry both blocks.
--                       It is not exclusive with them.
--   ts                 epoch seconds when the call completed — stamped by
--                       this function itself (time.time()), not read from
--                       the response.
--
-- service_ms and contention_ms are NULL for any backend that does not return
-- llama.cpp's `timings` block; completion_tokens/tok_s_wall is what such a
-- call reports instead. They are not a strict either/or pair, though — see
-- tok_s_wall above.
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
  'Durable REM per-call latency summary (decisions 570/571; fact:1621): {service_ms,wall_ms,contention_ms,poll_ms,model,backend,batch_size,prompt_chars,completion_tokens,tok_s_wall,ts}. service_ms/contention_ms=model/hardware+capacity, both NULL for backends without llama.cpp timings. model/backend/batch_size/prompt_chars/ts are the calls own envelope, never llama.cpp timings output. completion_tokens/tok_s_wall are the OpenAI-compatible fallback (tok_s_wall=completion_tokens/wall_s, an EFFECTIVE rate including TTFT+network) for a backend with no timings block, but are filled whenever usage.completion_tokens is present regardless -- may also appear beside a non-null service_ms. Survives neo4j_outbox deletion on NREM consolidation.';

COMMIT;
