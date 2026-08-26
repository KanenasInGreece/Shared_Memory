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
import math
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

# ── Embedding call sizing ────────────────────────────────────────────────────
# Same lesson as LLM_MIN_TOK_S above, on the other backend. Embedding cost is
# NOT constant and not even linear: measured on BGE-M3 (335M Q8_0, llama.cpp,
# CPU) throughput fell threefold from 438 tok/s at 236 tokens to 148 tok/s at
# 7414, fitting a linear term plus a quadratic attention term to within 0.52 s
# across the range. A CONSTANT timeout therefore under-provisions exactly the
# large inputs that need it most: the shipped 20 s covered only ~52% of the
# embedder's own 8192-token context, so a fold could synthesise a summary it
# could never vectorise, and lose the whole generation to a timeout.
#
# ANCHOR THE FLOOR AT MAX CONTEXT, NOT AT THE AVERAGE. Because the true cost is
# superlinear, a linear rule is safe only when its throughput floor is taken
# from the SLOWEST (largest-input) observation. Checked against the measurement:
# anchored on the worst observed 147.5 tok/s a linear rule predicts 56 s at 8192
# tokens against a true 59 s — under by 4 s, absorbed by the margin below;
# anchored on the mean 273 tok/s it predicts 30 s against the same 59 s, under
# by 29 s, and would fail every large call while looking correct on small ones.
# The default sits below the worst observation for that margin. A GPU-backed or
# faster embedder should raise it; a slower box must lower it.
EMBED_MIN_TOK_S = float(os.environ.get("EMBED_MIN_TOK_S", "100"))
# THE INVARIANT IS THE EMBEDDER'S CONTEXT. BGE-M3 has a hard 8192-token window
# and REFUSES anything larger outright (HTTP 500 "input is too large to
# process") rather than truncating it. So every caller clamps its input, and
# BECAUSE the input is clamped the longest embedding call this framework can
# ever make is a known, fixed quantity — the ceiling is computed from it here
# rather than guessed at each call site. Env-tunable only because a different
# deployment may run a different embedder; the derivation stays in code.
EMBED_MAX_CONTEXT_TOKENS = int(os.environ.get("EMBED_MAX_CONTEXT_TOKENS", "8192"))
# Conservative chars-per-token, used for BOTH the clamp and the token estimate.
# Held BELOW the ~4.3 measured for English prose because code, identifiers and
# non-English tokenize denser: a low value truncates earlier (never overruns the
# context) and over-estimates tokens (never under-sizes the timeout). Both
# errors are on the safe side, which is why one constant serves both uses.
EMBED_CHARS_PER_TOKEN = float(os.environ.get("EMBED_CHARS_PER_TOKEN", "3.0"))
# Longest input ever SENT to the embedder — derived from the context above, not
# a magic number. The FULL text is always kept in Tier 1 and still returned by
# search; only the vector is computed from the leading slice.
EMBED_MAX_CHARS = int(os.environ.get(
    "EMBED_MAX_CHARS", str(int(EMBED_MAX_CONTEXT_TOKENS * EMBED_CHARS_PER_TOKEN))))
# Margin over the derived time. Not the invariant — just headroom, because
# measured throughput falls as the input grows, so a ceiling fitted exactly to
# the floor has nothing left for the slowest run at the largest size.
EMBED_SAFETY_FACTOR = float(os.environ.get("EMBED_SAFETY_FACTOR", "1.5"))
# Floor for small inputs — connection setup, and queueing behind another
# request, because the embedder serialises.
EMBED_TIMEOUT_FLOOR_S = float(os.environ.get("EMBED_TIMEOUT_FLOOR_S", "20"))


def embed_ceiling(input_chars: int) -> float:
    """Per-request embedding timeout in seconds, derived from input SIZE.

    Bounded by construction: callers clamp input to ``EMBED_MAX_CHARS``, so the
    ceiling can never exceed the full-context time — at the shipped defaults
    8192 / 100 * 1.5 = 123 s, against a measured true cost of ~59 s at that
    size. Pure → unit-testable without an embedder."""
    if EMBED_CHARS_PER_TOKEN <= 0 or EMBED_MIN_TOK_S <= 0:
        return EMBED_TIMEOUT_FLOOR_S
    est_tokens = min(max(0, int(input_chars)), EMBED_MAX_CHARS) / EMBED_CHARS_PER_TOKEN
    return max(EMBED_TIMEOUT_FLOOR_S,
               est_tokens / EMBED_MIN_TOK_S * EMBED_SAFETY_FACTOR)


# ── Reranking call sizing ────────────────────────────────────────────────────
# The SAME lesson as the embedder above, on the third backend — and the one it
# was never applied to. The reranker scores each (query, document) pair, so its
# cost tracks the TOTAL text handed to it, not the number of documents. It ran
# for an unknown period behind a CONSTANT 5 s timeout while a real 20-candidate
# set measured 64 s, so every search silently fell back to unranked cosine order
# and the failure was invisible: the fallback emitted a plausible score and
# /health only ever pinged for liveness.
#
# ANCHOR THE FLOOR AT THE LARGEST PAYLOAD, exactly as for the embedder. Measured
# on BGE-Reranker-v2-m3 (568M Q8_0, llama.cpp, CPU, 4 threads): 1447 char/s on a
# 20k-char set falling to 887 char/s on a 129k-char one — throughput DROPS as the
# payload grows, so a floor taken from the average would under-provision the
# large calls that need it most. The default sits below the worst observation.
# A GPU-backed reranker should raise it; a slower box must lower it.
RERANK_MIN_CHARS_S = float(os.environ.get("RERANK_MIN_CHARS_S", "800"))
# Longest text ever SENT PER DOCUMENT. This is a relevance window, not a
# truncation of the record: the full text is always kept in Tier 1 and still
# returned by search — only the text the reranker SCORES is bounded.
#
# ⛔ IT DEFAULTS TO THE EMBEDDING WINDOW, AND THAT DEFAULT IS THE CORRECTNESS
# ONE. Retrieval SELECTS a candidate using the embedding of up to
# EMBED_MAX_CHARS; if ranking then sees a narrower slice, a record can be
# demoted for lacking the very text it was selected for — ranking undoing
# retrieval. Measured on the reference corpus, narrowing to 2000 chars kept only
# about half of reranking's improvement over plain vector order. So the two
# windows are DERIVED FROM ONE VALUE rather than set independently, and any
# divergence between them is a deliberate act with a cost, not a default.
#
# Lowering it is the dominant latency lever, far more than thread count: at a
# fixed 4 threads, narrowing a real 20-candidate set to 2000 chars took it from
# 64 s to 30 s, and char/s IMPROVES as documents shorten because the attention
# term is quadratic. Lower it when latency forces the trade — knowing what the
# trade is.
RERANK_MAX_DOC_CHARS = int(os.environ.get(
    "RERANK_MAX_DOC_CHARS", str(EMBED_MAX_CHARS)))
# Same role as EMBED_SAFETY_FACTOR — headroom over the derived time, because
# throughput falls with size and a ceiling fitted exactly to the floor leaves
# nothing for the slowest run at the largest payload.
RERANK_SAFETY_FACTOR = float(os.environ.get("RERANK_SAFETY_FACTOR", "1.5"))
# Floor for small payloads — connection setup, and queueing behind another
# request, because the reranker serialises across its slots.
RERANK_TIMEOUT_FLOOR_S = float(os.environ.get("RERANK_TIMEOUT_FLOOR_S", "10"))


def clamp_rerank_doc(text: str) -> str:
    """The slice of one record the reranker SCORES. Bounding this is what makes
    the ceiling below a known, fixed quantity rather than a guess — the same
    relationship EMBED_MAX_CHARS has with embed_ceiling. Pure."""
    return (text or "")[:RERANK_MAX_DOC_CHARS]


def rerank_ceiling(docs) -> float:
    """Per-request rerank timeout in seconds, derived from the TOTAL size of the
    payload against a throughput floor.

    Bounded by construction: every document is clamped to RERANK_MAX_DOC_CHARS,
    so the ceiling can never exceed the full-candidate-set time — at the shipped
    defaults 20 x 2000 / 800 * 1.5 = 75 s, against a measured true cost of ~30 s
    for that payload on the reference CPU deployment. Pure → unit-testable
    without a reranker."""
    if RERANK_MIN_CHARS_S <= 0:
        return RERANK_TIMEOUT_FLOOR_S
    total = sum(len(clamp_rerank_doc(d)) for d in (docs or []))
    return max(RERANK_TIMEOUT_FLOOR_S,
               total / RERANK_MIN_CHARS_S * RERANK_SAFETY_FACTOR)


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
    the caller persists whatever is present. Never raises.

    completion_tokens/tok_s_wall (fact:1621) are the OpenAI-compatible fallback for backends that
    return no ``timings`` block at all (external, non-llama.cpp providers): tok_s_wall is an
    EFFECTIVE rate — completion_tokens / wall_s — so it includes TTFT and network time, unlike
    service_ms's pure-inference rate. It is present precisely where the server gives no timings.
    completion_tokens accepts int or float usage.completion_tokens (some providers send a float);
    bool is explicitly rejected (bool is a subclass of int in Python — True/False are not token
    counts); NaN/±inf are also rejected (math.isfinite) rather than raising out of int()."""
    t = (resp_json or {}).get("timings") or {}
    pm, dm = t.get("prompt_ms"), t.get("predicted_ms")
    service_ms = (round(pm + dm, 1)
                  if isinstance(pm, (int, float)) and isinstance(dm, (int, float)) else None)
    wall_ms = round(wall_s * 1000.0, 1) if wall_s is not None else None
    contention_ms = (round(max(0.0, wall_ms - service_ms), 1)
                     if wall_ms is not None and service_ms is not None else None)
    usage = (resp_json or {}).get("usage") or {}
    ct = usage.get("completion_tokens")
    # json.loads accepts bare NaN/Infinity (a non-standard but real JSON extension
    # some providers emit) — int(float('nan')) raises ValueError and int(float('inf'))
    # raises OverflowError, either of which would violate this function's "never
    # raises" contract and let a good LLM batch get discarded by the caller's
    # try/except. math.isfinite() rejects both before int() ever sees them.
    completion_tokens = (int(ct) if isinstance(ct, (int, float)) and not isinstance(ct, bool)
                          and math.isfinite(ct) else None)
    tok_s_wall = (round(completion_tokens / wall_s, 2)
                  if isinstance(completion_tokens, int) and completion_tokens > 0
                  and isinstance(wall_s, (int, float)) and not isinstance(wall_s, bool)
                  and wall_s > 0 else None)
    return {
        "service_ms": service_ms,
        "wall_ms": wall_ms,
        "contention_ms": contention_ms,
        "poll_ms": None,               # per-fact; filled by the caller from created_at
        "model": (resp_json or {}).get("model"),
        "backend": backend,
        "batch_size": batch_size,
        "prompt_chars": prompt_chars,
        "completion_tokens": completion_tokens,
        "tok_s_wall": tok_s_wall,
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
    specimen: str | None = None,
    prompt_chars: int | None = None,
) -> dict:
    """Record one dream-cycle LLM call. Never raises (telemetry must not break a
    daemon cycle). Returns the record dict for callers that want to inspect it.

    `specimen` is an additive, optional pass-through (L0-a) — a bounded tail
    of a truncated completion body, already size-capped by the caller
    (REM_TRUNCATION_SPECIMEN_CHARS). It is written verbatim into the JSONL
    row when present and omitted (None) otherwise; this function does not
    bound or validate it. `note`/`ok` keep their existing meanings — a
    truncation classification (L0-b) is carried in `note`, it never flips `ok`
    (N4: additive keys only, no existing key changes meaning).

    `prompt_chars` (N-4, Model_Attributes_Routing_Plan_2026-08-18): the
    caller's own char-count of the prompt it built, additive and optional.
    N-1 found the originally-planned chars/token ratio measurement from
    dream-metrics history uncomputable because `prompt_n` (from llama.cpp's
    `timings`) and a char count never co-occurred in the same row — this
    field is what lets that pairing accumulate going forward, alongside the
    existing `prompt_n`, for a future from-history re-measurement of the
    gateway's own CHARS_PER_TOKEN_RATIO."""
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
        "specimen": specimen,
        "prompt_chars": prompt_chars,
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
