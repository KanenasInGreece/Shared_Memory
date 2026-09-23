-- Migration 037: re-issue the rem_timing column comment. Migration 019's text
-- predates completion_tokens and tok_s_wall (fact:1621), the OpenAI-compatible
-- fallback when a backend sends no llama.cpp timings block. decision:1624 —
-- a new migration re-issues the comment; 019 stays as it ran. schema_init
-- carries no COMMENT ON, so this file is what a deployed catalog stays true to.
-- COMMENT ON replaces the previous text; there is no IF NOT EXISTS form.

BEGIN;

COMMENT ON COLUMN technical_docs.rem_timing IS
  'Durable REM per-call latency summary (decisions 570/571; fact:1621): {service_ms,wall_ms,contention_ms,poll_ms,model,backend,batch_size,prompt_chars,completion_tokens,tok_s_wall,ts}. service_ms/contention_ms=model/hardware+capacity, both NULL for backends without llama.cpp timings. model/backend/batch_size/prompt_chars/ts are the calls own envelope, never llama.cpp timings output. completion_tokens/tok_s_wall are the OpenAI-compatible fallback (tok_s_wall=completion_tokens/wall_s, an EFFECTIVE rate including TTFT+network) for a backend with no timings block, but are filled whenever usage.completion_tokens is present regardless -- may also appear beside a non-null service_ms. Survives neo4j_outbox deletion on NREM consolidation.';

COMMIT;
