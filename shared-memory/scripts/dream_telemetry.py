"""Dream-cycle LLM telemetry, plus the embed/rerank timeout ceilings and
pair-prefix helpers this module also owns. Telemetry logs a structured line
always (and JSONL to ``DREAM_METRICS_PATH`` when set) from llama.cpp's
``timings`` and the gateway's backend header — observability only, nothing
here aborts or reroutes. Embed and rerank ceilings scale with payload size.
The chat call has no daemon timeout: ceiling_s stays null.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time

from framework_defaults import FRAMEWORK_DEFAULTS
from log_hygiene import append_secure

logger = logging.getLogger("DreamTelemetry")


def _finite_number(value):
    """A finite int or float, or None for bool, overflow, NaN, and Infinity."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value):
            return None
    except (OverflowError, ValueError):
        return None
    return value


def _model_name(resp: dict):
    """The reply model when it is a string. Anything else is absent."""
    model = resp.get("model")
    return model if isinstance(model, str) else None

DREAM_METRICS_PATH = os.environ.get("DREAM_METRICS_PATH", "").strip() or None

# Throughput falls as the input grows, so a constant timeout kills the large calls and looks fine on small ones. Anchor this floor on the slowest observation, not the mean.
EMBED_MIN_TOK_S = float(os.environ.get("EMBED_MIN_TOK_S", "100"))
# BGE-M3 refuses past its window instead of truncating, so the longest call is a known size. Tunable only because another embedder may differ.
EMBED_MAX_CONTEXT_TOKENS = int(os.environ.get(
    "EMBED_MAX_CONTEXT_TOKENS", str(FRAMEWORK_DEFAULTS["EMBED_MAX_CONTEXT_TOKENS"]["default"])))
# Below measured English prose so denser text truncates early and the timeout is over-estimated. Both mistakes stay on the safe side.
EMBED_CHARS_PER_TOKEN = float(os.environ.get(
    "EMBED_CHARS_PER_TOKEN", str(FRAMEWORK_DEFAULTS["EMBED_CHARS_PER_TOKEN"]["default"])))
# Special-token reserve (BOS/EOS/CLS/SEP) so raw text sent to the tokenizer
# never pushes the final sequence over the model window. Valid range: [0, EMBED_MAX_CONTEXT_TOKENS).
def _parse_special_token_reserve(max_ctx: int) -> int:
    raw = os.environ.get(
        "EMBED_SPECIAL_TOKEN_RESERVE",
        str(FRAMEWORK_DEFAULTS["EMBED_SPECIAL_TOKEN_RESERVE"]["default"]))
    try:
        val = int(raw)
        if 0 <= val < max_ctx:
            return val
    except (ValueError, TypeError):
        pass
    return FRAMEWORK_DEFAULTS["EMBED_SPECIAL_TOKEN_RESERVE"]["default"]


EMBED_SPECIAL_TOKEN_RESERVE = _parse_special_token_reserve(EMBED_MAX_CONTEXT_TOKENS)

# Longest input ever SENT to the embedder — derived from the context above, not
# a magic number. The FULL text is always kept in Tier 1 and still returned by
# search; only the vector is computed from the leading slice.
# Default: (8192 - 2) * 3.0 = 24570 chars (leaving 2 tokens for specials).
EMBED_MAX_CHARS = int(os.environ.get(
    "EMBED_MAX_CHARS", str(int((EMBED_MAX_CONTEXT_TOKENS - EMBED_SPECIAL_TOKEN_RESERVE) * EMBED_CHARS_PER_TOKEN))))
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


# Cost tracks total text, not document count. A fixed 5s timeout hid a 64s rerank behind a plausible unranked score. Throughput drops as the payload grows, so this floor is the slowest observation, not the mean.
RERANK_MIN_CHARS_S = float(os.environ.get("RERANK_MIN_CHARS_S", "800"))
# Defaults to the embedding window. Ranking a narrower slice demotes a record for lacking the text it was selected for. Lowering it is the latency lever; that is a deliberate loss of ranking, not a free speedup.
RERANK_MAX_DOC_CHARS = int(os.environ.get(
    "RERANK_MAX_DOC_CHARS", str(EMBED_MAX_CHARS)))
# Same role as EMBED_SAFETY_FACTOR — headroom over the derived time, because
# throughput falls with size and a ceiling fitted exactly to the floor leaves
# nothing for the slowest run at the largest payload.
RERANK_SAFETY_FACTOR = float(os.environ.get("RERANK_SAFETY_FACTOR", "1.5"))
# Floor for small payloads — connection setup, and queueing behind another
# request, because the reranker serialises across its slots.
RERANK_TIMEOUT_FLOOR_S = float(os.environ.get("RERANK_TIMEOUT_FLOOR_S", "10"))

special_reserve_chars = int(EMBED_SPECIAL_TOKEN_RESERVE * EMBED_CHARS_PER_TOKEN)
pair_budget = max(0, RERANK_MAX_DOC_CHARS - special_reserve_chars)


def as_text(v) -> str:
    """Safely return v if it is a string, else empty string. Never raises."""
    return v if isinstance(v, str) else ""


def prefix_rerank_query(query) -> str:
    """Rank the leading slice of query within the pair budget."""
    return as_text(query)[:pair_budget]


def prefix_rerank_doc(query, doc) -> str:
    """Rank the leading slice of doc that fits in the pair budget after query."""
    q = prefix_rerank_query(query)
    return as_text(doc)[: max(0, pair_budget - len(q))]


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
    defaults (24,570-char cap) 20 x 24570 / 800 * 1.5 ≈ 921 s; a realistic
    20 x 2000-char payload measured ~30 s true cost on the reference CPU
    deployment. Pure → unit-testable without a reranker."""
    if RERANK_MIN_CHARS_S <= 0:
        return RERANK_TIMEOUT_FLOOR_S
    total = sum(len(clamp_rerank_doc(d)) for d in (docs or []))
    return max(RERANK_TIMEOUT_FLOOR_S,
               total / RERANK_MIN_CHARS_S * RERANK_SAFETY_FACTOR)


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
    resp = resp_json if isinstance(resp_json, dict) else {}
    t = resp.get("timings")
    t = t if isinstance(t, dict) else {}
    pm, dm = t.get("prompt_ms"), t.get("predicted_ms")
    pm_n, dm_n = _finite_number(pm), _finite_number(dm)
    service_ms = None
    if pm_n is not None and dm_n is not None:
        try:
            total = float(pm_n) + float(dm_n)
        except (OverflowError, ValueError):
            total = None
        if total is not None and math.isfinite(total):
            service_ms = round(total, 1)
    wall_num = _finite_number(wall_s)
    wall_ms = None
    if wall_num is not None:
        try:
            scaled = float(wall_num) * 1000.0
            if math.isfinite(scaled):
                wall_ms = round(scaled, 1)
        except (OverflowError, ValueError):
            wall_ms = None
    contention_ms = (round(max(0.0, wall_ms - service_ms), 1)
                     if wall_ms is not None and service_ms is not None else None)
    usage = resp.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    ct_n = _finite_number(usage.get("completion_tokens"))
    # Overflow and NaN are absent, so a good batch is not discarded and JSON stays strict.
    try:
        completion_tokens = int(ct_n) if ct_n is not None else None
    except (OverflowError, ValueError):
        completion_tokens = None
    wall_for_rate = _finite_number(wall_s)
    try:
        tok_s_wall = (round(completion_tokens / wall_for_rate, 2)
                      if isinstance(completion_tokens, int) and completion_tokens > 0
                      and wall_for_rate is not None and wall_for_rate > 0 else None)
    except (OverflowError, ValueError):
        tok_s_wall = None
    return {
        "service_ms": service_ms,
        "wall_ms": wall_ms,
        "contention_ms": contention_ms,
        "poll_ms": None,               # per-fact; filled by the caller from created_at
        "model": _model_name(resp),
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

    `prompt_chars` is the caller's own char-count of the prompt it built, additive
    and optional — it pairs with `prompt_n` so chars/token can be re-measured from
    history."""
    resp = resp_json if isinstance(resp_json, dict) else {}
    raw_timings = resp.get("timings")
    t = raw_timings if isinstance(raw_timings, dict) else {}
    raw_usage = resp.get("usage")
    usage = raw_usage if isinstance(raw_usage, dict) else {}

    def _num(value):
        # A bool is an int in Python. Overflow, NaN, and Infinity are absent.
        return _finite_number(value)

    tok_s = _num(t.get("predicted_per_second"))
    prompt_raw = t.get("prompt_n")
    if prompt_raw is None:
        prompt_raw = usage.get("prompt_tokens")
    predicted_raw = t.get("predicted_n")
    if predicted_raw is None:
        predicted_raw = usage.get("completion_tokens")
    wall_num = _num(wall_s)
    ceiling_num = _num(ceiling_s)
    rec = {
        "ts": round(time.time(), 3),
        "phase": phase,
        "backend": backend,
        "model": _model_name(resp),
        "prompt_n": _num(prompt_raw),
        "predicted_n": _num(predicted_raw),
        "tok_s": round(tok_s, 2) if tok_s is not None else None,
        "prompt_ms": _num(t.get("prompt_ms")),
        "predicted_ms": _num(t.get("predicted_ms")),
        "wall_s": round(wall_num, 1) if wall_num is not None else None,
        "ceiling_s": round(ceiling_num, 1) if ceiling_num is not None else None,
        "ceiling_hit": bool(
            wall_num is not None and ceiling_num is not None and wall_num >= ceiling_num
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
