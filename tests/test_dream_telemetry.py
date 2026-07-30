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


def test_writes_jsonl_when_path_set(monkeypatch, tmp_path):
    metrics = tmp_path / "dream-metrics.jsonl"
    dt = _fresh(monkeypatch, str(metrics))
    dt.record_llm_call("REM", _RESP, backend="b1", wall_s=10.0, ceiling_s=600.0)
    dt.record_llm_call("NREM", _RESP, backend="b2", wall_s=20.0, ceiling_s=600.0)
    lines = metrics.read_text().strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["phase"] == "REM" and first["backend"] == "b1"
