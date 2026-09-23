-- Migration 019 (decisions 570/571): rem_timing stays on the fact after the
-- outbox row is deleted at consolidation. prompt_chars is what makes service_ms
-- comparable across models; poll_ms (daemon cadence) is null on the single-call path.
ALTER TABLE technical_docs ADD COLUMN IF NOT EXISTS rem_timing JSONB;

COMMENT ON COLUMN technical_docs.rem_timing IS
  'Durable REM per-call latency summary (decisions 570/571): {service_ms,wall_ms,contention_ms,poll_ms,model,backend,batch_size,prompt_chars,ts}. service_ms=model/hardware, contention_ms=capacity. Survives neo4j_outbox deletion on NREM consolidation.';
