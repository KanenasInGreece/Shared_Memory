"""Per-call dream-cycle LLM telemetry — measure-first instrumentation for the
adaptive-timer work (ADR-021).

llama.cpp returns a ``timings`` object on every completion (prompt_n, predicted_n,
predicted_per_second, …) and the gateway stamps an ``X-SM-LLM-Backend`` response
header naming the serving backend. Recording both per call gives us, with zero
extra LLM traffic:

  * observed tok/s per backend  → validate the pool weights + detect model drift
  * prompt/predicted token counts → validate context-fill (chunk work, ADR future)
  * wall time vs allocated ceiling → validate the adaptive hard ceiling; a
    ``ceiling_hit`` flags a generation that ran to the bound (candidate true hang)

This is observability only — nothing here aborts or reroutes. The data tells us
whether a gateway slot-liveness watcher (ADR-021 task 14b) is actually needed, or
whether the adaptive ceiling alone suffices.

Writes a structured log line always, and a JSONL record to ``DREAM_METRICS_PATH``
when set (one object per line; queryable, no schema/migration).
"""
from __future__ import annotations

import json
import logging
import os
import time

from log_hygiene import append_secure

logger = logging.getLogger("DreamTelemetry")

DREAM_METRICS_PATH = os.environ.get("DREAM_METRICS_PATH", "").strip() or None

# Adaptive hard-ceiling floor (seconds). The fixed *_LLM_TIMEOUT magic numbers are
# replaced by adaptive_ceiling() below; only this floor remains, because a tiny
# prompt should still be allowed a slow cold-start generation. Advisor-validated
# shape: max(floor, prompt_chars/100, units*15) — scales with prompt size and the
# work unit count (NREM cluster facts), so a big job is never killed for being big.
CEILING_FLOOR_S = float(os.environ.get("LLM_CEILING_FLOOR", "600"))

# Slowest generation rate the ceiling is willing to sit through, tokens/second.
# The prompt/unit terms above size the ceiling on the INPUT, but decode time is
# driven by the OUTPUT bound — so a raised max_tokens silently outgrew the
# ceiling and turned "this cluster needs a longer narrative" into an opaque
# timeout, which (unlike a truncation) is not even counted as a capacity
# failure. This term makes the ceiling scale with what actually costs the time.
#
# DELIBERATELY BELOW OBSERVED THROUGHPUT, NOT AT IT: a ceiling sized on the
# average kills every slower-than-average run. Measured here across 16 NREM
# calls: min 11.29, mean 13.93, max 15.10 tok/s (Qwen3-14B Q4_K_M on one Arc
# card, single slot). 10 leaves margin under the observed floor. It is an env
# knob because this is the one constant that is purely a property of somebody
# else's hardware — a faster rig should raise it, a CPU-only one must lower it.
#
# Keep the product (max_tokens / this) under the slot-arbiter budget
# (NREM_FORCED_SLOT_WAIT, default 1800s) or a long fold outlasts REM's
# willingness to yield the shared slot and the two daemons start fighting.
LLM_MIN_TOK_S = float(os.environ.get("LLM_MIN_TOK_S", "10"))


def adaptive_ceiling(prompt_chars: int, units: int = 0,
                     max_tokens: int = 0) -> float:
    """Per-call hard ceiling in seconds, scaled to the work. `prompt_chars` = len
    of the prompt sent; `units` = count of work items (NREM cluster size; 0 for REM
    per-fact); `max_tokens` = the OUTPUT bound requested, which dominates decode
    time — pass the WIDEST bound a call site may retry at, not the first one it
    tries. Omitting it keeps the pre-existing input-only behaviour exactly.
    Replaces the fixed timeout so valid long generations are not killed."""
    return max(CEILING_FLOOR_S, prompt_chars / 100.0, units * 15.0,
               max_tokens / LLM_MIN_TOK_S if LLM_MIN_TOK_S > 0 else 0.0)


def record_grounding(grounding_n: int, referenced: int, matched: int,
                     minted: int, *, pg_id: int | None = None,
                     shown: int | None = None, mode: str | None = None) -> dict:
    """Record REM grounding effectiveness per record (Task 15, measure-first).

    `grounding_n` is the ACCEPT set (every name the link gate will resolve),
    `shown` the SHOW set (candidates this record's prompt actually listed), and
    `mode` how the show set was chosen — "knn" (semantic recall) or "fallback"
    (alphabetical slice; the embedder or the entity store was unavailable).

    `minted` is retained as the on-disk key but its meaning changed with the
    link-only gate: a referenced name absent from the accept set is now DROPPED,
    not created. It is therefore emitted as `unresolved` / `unresolved_rate` too,
    and read as LOST LINKS — a fact whose mention of a real entity never became
    an edge, so that entity's cluster stopped growing toward the fold threshold.
    Lower is better, and it is the metric a grounding change is judged on.

    `mode` is what keeps that judgement honest: a spike in unresolved_rate under
    "fallback" is an embedder outage, not a regression in recall. Never raises.
    """
    rate = round(minted / referenced, 3) if referenced else None
    rec = {
        "ts": round(time.time(), 3), "kind": "rem_grounding", "pg_id": pg_id,
        "grounding_n": grounding_n, "referenced": referenced,
        "matched": matched, "minted": minted, "mint_rate": rate,
        # Same two numbers under the names that now describe them.
        "unresolved": minted, "unresolved_rate": rate,
        "shown": shown, "mode": mode,
    }
    try:
        logger.info("rem-grounding pg_id=%s accept_n=%d shown=%s mode=%s referenced=%d "
                    "matched=%d unresolved=%d unresolved_rate=%s",
                    pg_id, grounding_n, shown, mode, referenced, matched, minted, rate)
        if DREAM_METRICS_PATH:
            append_secure(DREAM_METRICS_PATH, json.dumps(rec))
    except Exception as exc:
        logger.warning("dream grounding record failed: %s", exc)
    return rec


def call_timing_summary(
    resp_json: dict | None,
    wall_s: float | None,
    *,
    backend: str | None = None,
    batch_size: int | None = None,
    prompt_chars: int | None = None,
) -> dict:
    """Compact per-call REM timing for DURABLE persistence on the fact row (decisions
    570/571) — distinct from record_llm_call, which streams to the JSONL metrics file.

    Splits the one client-observed number into its honest parts:
      service_ms    = llama.cpp prompt_ms + predicted_ms — pure inference = MODEL + HARDWARE,
                      load-invariant (the anchor metric, decision 568).
      wall_ms       = wall_s * 1000 — what the caller experienced (service + contention + net).
      contention_ms = max(0, wall_ms - service_ms) — time queued behind a busy backend = the
                      CAPACITY signal that falls toward 0 as the pool absorbs load. This is the
                      one-backend example resolved: the second REM's busy-wait lands HERE, never
                      folded into service_ms (fact 569).
    model tags the model-evolution axis; batch_size/prompt_chars carry the workload context that
    makes cross-model service_ms comparable. All best-effort — any missing input yields None, and
    the caller persists whatever is present. Never raises."""
    t = (resp_json or {}).get("timings") or {}
    pm, dm = t.get("prompt_ms"), t.get("predicted_ms")
    service_ms = (round(pm + dm, 1)
                  if isinstance(pm, (int, float)) and isinstance(dm, (int, float)) else None)
    wall_ms = round(wall_s * 1000.0, 1) if wall_s is not None else None
    contention_ms = (round(max(0.0, wall_ms - service_ms), 1)
                     if wall_ms is not None and service_ms is not None else None)
    return {
        "service_ms": service_ms,
        "wall_ms": wall_ms,
        "contention_ms": contention_ms,
        "poll_ms": None,               # per-fact; filled by the caller from created_at
        "model": (resp_json or {}).get("model"),
        "backend": backend,
        "batch_size": batch_size,
        "prompt_chars": prompt_chars,
        "ts": round(time.time(), 3),
    }


def record_llm_call(
    phase: str,
    resp_json: dict | None,
    *,
    backend: str | None = None,
    wall_s: float | None = None,
    ceiling_s: float | None = None,
    ok: bool = True,
    note: str | None = None,
) -> dict:
    """Record one dream-cycle LLM call. Never raises (telemetry must not break a
    daemon cycle). Returns the record dict for callers that want to inspect it."""
    t = (resp_json or {}).get("timings") or {}
    usage = (resp_json or {}).get("usage") or {}
    tok_s = t.get("predicted_per_second")
    rec = {
        "ts": round(time.time(), 3),
        "phase": phase,
        "backend": backend,
        "model": (resp_json or {}).get("model"),
        "prompt_n": t.get("prompt_n") if t.get("prompt_n") is not None else usage.get("prompt_tokens"),
        "predicted_n": t.get("predicted_n") if t.get("predicted_n") is not None else usage.get("completion_tokens"),
        "tok_s": round(tok_s, 2) if isinstance(tok_s, (int, float)) else None,
        "prompt_ms": t.get("prompt_ms"),
        "predicted_ms": t.get("predicted_ms"),
        "wall_s": round(wall_s, 1) if wall_s is not None else None,
        "ceiling_s": round(ceiling_s, 1) if ceiling_s is not None else None,
        "ceiling_hit": bool(
            wall_s is not None and ceiling_s is not None and wall_s >= ceiling_s
        ),
        "ok": ok,
        "note": note,
    }
    try:
        logger.info(
            "dream-llm %s tok_s=%s prompt_n=%s predicted_n=%s wall=%ss ceiling=%ss backend=%s%s",
            phase, rec["tok_s"], rec["prompt_n"], rec["predicted_n"],
            rec["wall_s"], rec["ceiling_s"], backend or "?",
            "" if ok else f" FAIL({note})",
        )
        if DREAM_METRICS_PATH:
            append_secure(DREAM_METRICS_PATH, json.dumps(rec))
    except Exception as exc:  # telemetry is best-effort, never fatal
        logger.warning("dream telemetry record failed: %s", exc)
    return rec
