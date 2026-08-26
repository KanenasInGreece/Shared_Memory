"""Tests for dream_telemetry.record_llm_call (ADR-021 measure-first instrument)."""
import importlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def _fresh(monkeypatch, path=None):
    """Import dream_telemetry with DREAM_METRICS_PATH set/unset at import time."""
    if path is None:
        monkeypatch.delenv("DREAM_METRICS_PATH", raising=False)
    else:
        monkeypatch.setenv("DREAM_METRICS_PATH", path)
    import dream_telemetry
    importlib.reload(dream_telemetry)
    return dream_telemetry


_RESP = {
    "timings": {"prompt_n": 1840, "predicted_n": 312, "prompt_ms": 5000.0,
                "predicted_ms": 87000.0, "predicted_per_second": 3.586},
    "usage": {"prompt_tokens": 1841, "completion_tokens": 312},
}


def test_extracts_timings(monkeypatch):
    dt = _fresh(monkeypatch)
    rec = dt.record_llm_call("REM", _RESP, backend="http://localhost:5000",
                             wall_s=90.0, ceiling_s=600.0)
    assert rec["phase"] == "REM"
    assert rec["backend"] == "http://localhost:5000"
    assert rec["prompt_n"] == 1840
    assert rec["predicted_n"] == 312
    assert rec["tok_s"] == 3.59          # rounded
    assert rec["ceiling_hit"] is False   # 90 < 600


def test_ceiling_hit_flagged(monkeypatch):
    dt = _fresh(monkeypatch)
    rec = dt.record_llm_call("NREM", _RESP, wall_s=605.0, ceiling_s=600.0)
    assert rec["ceiling_hit"] is True


def test_falls_back_to_usage_when_no_timings(monkeypatch):
    dt = _fresh(monkeypatch)
    rec = dt.record_llm_call("REM", {"usage": {"prompt_tokens": 50, "completion_tokens": 7}})
    assert rec["prompt_n"] == 50
    assert rec["predicted_n"] == 7
    assert rec["tok_s"] is None          # no speed without timings


def test_failure_record_never_raises_on_none(monkeypatch):
    dt = _fresh(monkeypatch)
    rec = dt.record_llm_call("NREM", None, ok=False, note="http_503")
    assert rec["ok"] is False
    assert rec["note"] == "http_503"
    assert rec["tok_s"] is None


def test_adaptive_ceiling_floor_and_scaling(monkeypatch):
    dt = _fresh(monkeypatch)
    # small prompt → floor
    assert dt.adaptive_ceiling(1000, 0) == 600.0
    # large prompt dominates: 80_000 chars / 100 = 800
    assert dt.adaptive_ceiling(80_000, 0) == 800.0
    # unit count dominates: 79 facts * 15 = 1185
    assert dt.adaptive_ceiling(4400, 79) == 1185.0


def test_adaptive_ceiling_output_bound_dominates(monkeypatch):
    """Decode time tracks the OUTPUT bound, so max_tokens must be able to win.

    The input-only ceiling was blind to it: a widened NREM retry at 16384 tokens
    needs ~1638s at the shipped 10 tok/s floor but got the 600s floor, so the
    generation the retry exists to allow was killed by its own timeout — and a
    timeout is not counted as a truncation, so the capacity failure went unseen.
    """
    dt = _fresh(monkeypatch)
    # 16384 / 10 tok/s = 1638.4s, beating the 600s floor and both input terms
    assert dt.adaptive_ceiling(1000, 0, max_tokens=16384) == 1638.4
    # a small bound must NOT drag the ceiling below the floor
    assert dt.adaptive_ceiling(1000, 0, max_tokens=250) == 600.0
    # input terms still win when they are the larger cost
    assert dt.adaptive_ceiling(400_000, 0, max_tokens=8192) == 4000.0


def test_adaptive_ceiling_omitting_max_tokens_is_unchanged(monkeypatch):
    """The new parameter is additive — every pre-existing call site keeps its
    exact previous ceiling, so REM/relation_sweep are untouched by this change."""
    dt = _fresh(monkeypatch)
    for prompt_chars, units in ((1000, 0), (80_000, 0), (4400, 79)):
        assert (dt.adaptive_ceiling(prompt_chars, units)
                == dt.adaptive_ceiling(prompt_chars, units, max_tokens=0))


def test_llm_min_tok_s_is_env_tunable(monkeypatch):
    """Throughput is purely a property of the operator's hardware — a slower rig
    must be able to buy more wall-clock without editing code."""
    monkeypatch.setenv("LLM_MIN_TOK_S", "5")
    dt = _fresh(monkeypatch)
    assert dt.adaptive_ceiling(1000, 0, max_tokens=16384) == 3276.8
    # a zero/absurd setting must not raise ZeroDivisionError, just disable the term
    monkeypatch.setenv("LLM_MIN_TOK_S", "0")
    dt = _fresh(monkeypatch)
    assert dt.adaptive_ceiling(1000, 0, max_tokens=16384) == 600.0


def test_record_grounding_mint_rate(monkeypatch):
    dt = _fresh(monkeypatch)
    rec = dt.record_grounding(grounding_n=1500, referenced=10, matched=7, minted=3, pg_id=42)
    assert rec["kind"] == "rem_grounding"
    assert rec["mint_rate"] == 0.3
    assert rec["grounding_n"] == 1500 and rec["pg_id"] == 42
    # no references → mint_rate None, never divides by zero
    assert dt.record_grounding(1500, 0, 0, 0)["mint_rate"] is None


def test_record_llm_call_captures_model(monkeypatch):
    dt = _fresh(monkeypatch)
    rec = dt.record_llm_call("REM", {**_RESP, "model": "gemma-4-12b"}, wall_s=10.0)
    assert rec["model"] == "gemma-4-12b"
    # absent model → None, never raises
    assert dt.record_llm_call("REM", _RESP, wall_s=10.0)["model"] is None


def test_call_timing_summary_splits_service_and_contention(monkeypatch):
    dt = _fresh(monkeypatch)
    # service = prompt_ms + predicted_ms = 5000 + 87000 = 92000ms; wall = 100s = 100000ms
    # → contention = 100000 - 92000 = 8000ms (the one-backend busy-wait lands HERE)
    resp = {**_RESP, "model": "gemma-4-12b"}
    t = dt.call_timing_summary(resp, 100.0, backend="http://localhost:4000",
                               batch_size=5, prompt_chars=22000)
    assert t["service_ms"] == 92000.0
    assert t["wall_ms"] == 100000.0
    assert t["contention_ms"] == 8000.0
    assert t["model"] == "gemma-4-12b"
    assert t["backend"] == "http://localhost:4000"
    assert t["batch_size"] == 5 and t["prompt_chars"] == 22000
    assert t["poll_ms"] is None          # per-fact; filled by the caller


def test_call_timing_summary_contention_never_negative(monkeypatch):
    dt = _fresh(monkeypatch)
    # wall < service (clock/measurement skew) → contention floored at 0, not negative
    t = dt.call_timing_summary(_RESP, 10.0)   # wall 10s < service 92s
    assert t["contention_ms"] == 0.0


def test_call_timing_summary_missing_timings_yields_none(monkeypatch):
    dt = _fresh(monkeypatch)
    t = dt.call_timing_summary({"usage": {}}, None)
    assert t["service_ms"] is None and t["wall_ms"] is None
    assert t["contention_ms"] is None      # can't split without both
    assert t["model"] is None


def test_call_timing_summary_openai_style_uses_usage_and_wall(monkeypatch):
    # fact:1621 — an OpenAI-compatible external backend returns no llama.cpp
    # `timings` block at all, only `usage.completion_tokens`. service_ms and
    # contention_ms stay honestly None; wall_ms and the new tok_s_wall carry
    # the signal for this backend instead of the row vanishing.
    dt = _fresh(monkeypatch)
    resp = {"model": "ext-model", "usage": {"completion_tokens": 266}}
    t = dt.call_timing_summary(resp, 2.1, backend="https://api.example.com")
    assert t["service_ms"] is None
    assert t["contention_ms"] is None
    assert t["wall_ms"] == 2100.0
    assert t["completion_tokens"] == 266
    assert t["tok_s_wall"] == 126.67
    assert t["model"] == "ext-model"
    assert t["backend"] == "https://api.example.com"


def test_call_timing_summary_no_usage_yields_none_new_keys(monkeypatch):
    # No usage block at all, or completion_tokens 0/absent, or wall_s None —
    # the new keys must degrade to None without raising, never divide by zero.
    dt = _fresh(monkeypatch)
    t1 = dt.call_timing_summary({"model": "m"}, None)
    assert t1["completion_tokens"] is None
    assert t1["tok_s_wall"] is None

    t2 = dt.call_timing_summary({"usage": {"completion_tokens": 0}}, 5.0)
    assert t2["completion_tokens"] == 0
    assert t2["tok_s_wall"] is None

    t3 = dt.call_timing_summary({"usage": {"completion_tokens": 10}}, None)
    assert t3["completion_tokens"] == 10
    assert t3["tok_s_wall"] is None


def test_call_timing_summary_accepts_float_completion_tokens(monkeypatch):
    # Some OpenAI-compatible providers send usage.completion_tokens as a float.
    dt = _fresh(monkeypatch)
    resp = {"model": "ext-model", "usage": {"completion_tokens": 266.0}}
    t = dt.call_timing_summary(resp, 2.1)
    assert t["completion_tokens"] == 266
    assert isinstance(t["completion_tokens"], int)
    assert t["tok_s_wall"] == 126.67


def test_call_timing_summary_rejects_bool_completion_tokens(monkeypatch):
    # bool is a subclass of int in Python — True/False are not token counts
    # and must not be laundered into completion_tokens=1/0.
    dt = _fresh(monkeypatch)
    resp = {"model": "ext-model", "usage": {"completion_tokens": True}}
    t = dt.call_timing_summary(resp, 2.1)
    assert t["completion_tokens"] is None
    assert t["tok_s_wall"] is None


def test_call_timing_summary_rejects_nan_completion_tokens(monkeypatch):
    # json.loads accepts bare NaN as a non-standard JSON extension some
    # providers emit. int(float('nan')) raises ValueError — this must not
    # propagate out of call_timing_summary (the caller's batch try/except
    # would discard an otherwise-good LLM batch on that exception).
    dt = _fresh(monkeypatch)
    resp = {"model": "ext-model", "usage": {"completion_tokens": float("nan")}}
    t = dt.call_timing_summary(resp, 2.1)   # must not raise
    assert t["completion_tokens"] is None
    assert t["tok_s_wall"] is None


def test_call_timing_summary_rejects_inf_completion_tokens(monkeypatch):
    # int(float('inf')) raises OverflowError — same "never raises" contract.
    dt = _fresh(monkeypatch)
    resp = {"model": "ext-model", "usage": {"completion_tokens": float("inf")}}
    t = dt.call_timing_summary(resp, 2.1)   # must not raise
    assert t["completion_tokens"] is None
    assert t["tok_s_wall"] is None


def test_call_timing_summary_wall_s_bool_true_yields_none_tok_s_wall(monkeypatch):
    # bool is a subclass of int/float-comparable in Python — wall_s=True must
    # not be treated as wall_s=1.0 (the not-isinstance(wall_s, bool) guard).
    dt = _fresh(monkeypatch)
    resp = {"model": "ext-model", "usage": {"completion_tokens": 266}}
    t = dt.call_timing_summary(resp, True)
    assert t["completion_tokens"] == 266
    assert t["tok_s_wall"] is None


def test_writes_jsonl_when_path_set(monkeypatch, tmp_path):
    metrics = tmp_path / "dream-metrics.jsonl"
    dt = _fresh(monkeypatch, str(metrics))
    dt.record_llm_call("REM", _RESP, backend="b1", wall_s=10.0, ceiling_s=600.0)
    dt.record_llm_call("NREM", _RESP, backend="b2", wall_s=20.0, ceiling_s=600.0)
    lines = metrics.read_text().strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["phase"] == "REM" and first["backend"] == "b1"


# ── embed_ceiling: the embedder's context is the invariant ────────────────────

def test_embed_ceiling_scales_with_input_and_holds_a_floor(monkeypatch):
    dt = _fresh(monkeypatch)
    # Small inputs sit on the floor — connection setup dominates, not compute.
    assert dt.embed_ceiling(0) == dt.EMBED_TIMEOUT_FLOOR_S
    assert dt.embed_ceiling(500) == dt.EMBED_TIMEOUT_FLOOR_S
    # Large inputs scale: tokens / throughput floor * safety factor.
    big = dt.embed_ceiling(18_000)
    assert big == (18_000 / dt.EMBED_CHARS_PER_TOKEN) / dt.EMBED_MIN_TOK_S \
        * dt.EMBED_SAFETY_FACTOR
    assert big > dt.EMBED_TIMEOUT_FLOOR_S


def test_embed_ceiling_is_monotone_and_bounded_by_the_context(monkeypatch):
    """The ceiling never exceeds the full-context time, because callers clamp
    input to EMBED_MAX_CHARS. An input ten times the clamp costs the same."""
    dt = _fresh(monkeypatch)
    vals = [dt.embed_ceiling(n) for n in range(0, 120_000, 2_000)]
    assert vals == sorted(vals), "ceiling must never shrink as input grows"
    full = dt.embed_ceiling(dt.EMBED_MAX_CHARS)
    assert dt.embed_ceiling(dt.EMBED_MAX_CHARS * 10) == full
    expected = (dt.EMBED_MAX_CONTEXT_TOKENS / dt.EMBED_MIN_TOK_S
                * dt.EMBED_SAFETY_FACTOR)
    assert abs(full - expected) < 1e-6


def test_embed_ceiling_covers_the_measured_cost_at_full_context(monkeypatch):
    """Regression guard tied to measurement, not taste. BGE-M3 on CPU was timed
    at 50.3 s for 7414 tokens, fitting wall = 1.92e-3*n + 6.48e-7*n^2 to within
    0.52 s — about 59 s at the 8192-token context. The shipped defaults must
    leave headroom over that, or the ceiling reintroduces the bug it fixes."""
    dt = _fresh(monkeypatch)
    n = dt.EMBED_MAX_CONTEXT_TOKENS
    measured_worst = 1.92e-3 * n + 6.48e-7 * n ** 2
    assert dt.embed_ceiling(dt.EMBED_MAX_CHARS) > measured_worst
    # ...and the OLD hardcoded 20s did not, which is why folds were lost.
    assert measured_worst > 20.0


def test_embed_max_chars_derives_from_the_context(monkeypatch):
    monkeypatch.setenv("EMBED_MAX_CONTEXT_TOKENS", "4096")
    monkeypatch.setenv("EMBED_CHARS_PER_TOKEN", "3.0")
    monkeypatch.delenv("EMBED_MAX_CHARS", raising=False)
    dt = _fresh(monkeypatch)
    assert dt.EMBED_MAX_CHARS == 12_288


def test_embed_ceiling_knobs_are_env_tunable(monkeypatch):
    monkeypatch.setenv("EMBED_MIN_TOK_S", "400")       # a GPU-backed embedder
    monkeypatch.setenv("EMBED_TIMEOUT_FLOOR_S", "5")
    monkeypatch.setenv("EMBED_SAFETY_FACTOR", "2.0")
    dt = _fresh(monkeypatch)
    assert dt.embed_ceiling(dt.EMBED_MAX_CHARS) == \
        dt.EMBED_MAX_CONTEXT_TOKENS / 400 * 2.0
    assert dt.embed_ceiling(1) == 5.0


def test_embed_ceiling_degrades_safely_on_nonsense_config(monkeypatch):
    monkeypatch.setenv("EMBED_MIN_TOK_S", "0")
    dt = _fresh(monkeypatch)
    assert dt.embed_ceiling(50_000) == dt.EMBED_TIMEOUT_FLOOR_S


def test_adaptive_ceiling_llm_side_is_untouched(monkeypatch):
    """The embedder work must not have moved the LLM ceiling — its timings stay
    exactly as configured, no safety factor applied."""
    dt = _fresh(monkeypatch)
    assert dt.adaptive_ceiling(1000, 0, max_tokens=16384) == 16384 / dt.LLM_MIN_TOK_S
