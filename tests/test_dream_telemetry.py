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


def test_record_grounding_mint_rate(monkeypatch):
    dt = _fresh(monkeypatch)
    rec = dt.record_grounding(grounding_n=1500, referenced=10, matched=7, minted=3, pg_id=42)
    assert rec["kind"] == "rem_grounding"
    assert rec["mint_rate"] == 0.3
    assert rec["grounding_n"] == 1500 and rec["pg_id"] == 42
    # no references → mint_rate None, never divides by zero
    assert dt.record_grounding(1500, 0, 0, 0)["mint_rate"] is None


def test_writes_jsonl_when_path_set(monkeypatch, tmp_path):
    metrics = tmp_path / "dream-metrics.jsonl"
    dt = _fresh(monkeypatch, str(metrics))
    dt.record_llm_call("REM", _RESP, backend="b1", wall_s=10.0, ceiling_s=600.0)
    dt.record_llm_call("NREM", _RESP, backend="b2", wall_s=20.0, ceiling_s=600.0)
    lines = metrics.read_text().strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["phase"] == "REM" and first["backend"] == "b1"
