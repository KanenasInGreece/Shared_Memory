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


def adaptive_ceiling(prompt_chars: int, units: int = 0) -> float:
    """Per-call hard ceiling in seconds, scaled to the work. `prompt_chars` = len
    of the prompt sent; `units` = count of work items (NREM cluster size; 0 for REM
    per-fact). Replaces the fixed timeout so valid long generations are not killed."""
    return max(CEILING_FLOOR_S, prompt_chars / 100.0, units * 15.0)


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
