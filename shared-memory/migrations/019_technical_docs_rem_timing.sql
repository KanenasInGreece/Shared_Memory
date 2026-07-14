-- Migration 019: technical_docs.rem_timing — durable REM latency components (decisions 570/571)
--
-- REM per-call timing lived only implicitly (wall_s in the DREAM_METRICS_PATH JSONL) and
-- rem_reviewed_at on neo4j_outbox — the outbox row is DELETED when NREM consolidates the fact
-- (coordinator: DELETE FROM neo4j_outbox), so both the components and the end-to-end total
-- evaporate. This column persists a compact per-call timing summary ON the durable fact row so
-- it survives outbox deletion and becomes a longitudinal model-evolution / hardware series.
--
-- Shape (JSONB, one object per REM'd fact; keys null when the backend omits llama.cpp timings):
--   service_ms     pure inference on the backend (prompt_ms + predicted_ms) = MODEL + HARDWARE,
--                  load-invariant. The honest per-call latency (decision 568/570).
--   wall_ms        client-observed call time (service + contention + network).
--   contention_ms  max(0, wall_ms - service_ms) — queue behind a busy backend = CAPACITY signal
--                  (falls toward 0 as the dual-GPU pool absorbs load; the parallelisation proof).
--   poll_ms        created_at -> REM pickup: daemon cadence (informational; null on the single path).
--   model          LLM model tag from the response — the model-evolution axis (decision 571).
--   backend        X-SM-LLM-Backend the gateway pool routed to.
--   batch_size     facts sharing this one LLM call (per-fact cost = service_ms / batch_size).
--   prompt_chars   grounding-prompt size — REQUIRED for cross-model comparability (adaptive_ceiling
--                  scales with it), else a model-vs-model service_ms comparison confounds with size.
--   ts             epoch seconds when the call completed.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS — safe to re-run every apply.py.
ALTER TABLE technical_docs ADD COLUMN IF NOT EXISTS rem_timing JSONB;

COMMENT ON COLUMN technical_docs.rem_timing IS
  'Durable REM per-call latency summary (decisions 570/571): {service_ms,wall_ms,contention_ms,poll_ms,model,backend,batch_size,prompt_chars,ts}. service_ms=model/hardware, contention_ms=capacity. Survives neo4j_outbox deletion on NREM consolidation.';
