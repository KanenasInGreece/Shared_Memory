"""Tests for postflight.sh Check A9 (encoder window contract, decision:2540).

Extracts A9_GRADE_WINDOW from postflight.sh verbatim and verifies grading,
messages naming required phrases (--max-model-len, -c, EMBED_MAX_CONTEXT_TOKENS,
leftover EMBED_MAX_CHARS), and postflight flow.
"""
import json
import os
import re
import subprocess
import pytest
from pathlib import Path

POSTFLIGHT = (Path(__file__).parent.parent / "shared-memory" / "scripts"
              / "postflight.sh")


def _extract_marked_block(begin: str, end: str) -> str:
    text = POSTFLIGHT.read_text()
    pattern = re.escape(begin) + r".*?\n(.*?)\n[ \t]*" + re.escape(end)
    m = re.search(pattern, text, re.S)
    assert m, (
        f"could not find a {begin!r} ... {end!r} block in {POSTFLIGHT} -- "
        f"the extraction markers moved or were removed"
    )
    return m.group(1)


def _run_a9_grade_window(payload: dict | str) -> tuple[str, str, str]:
    block = _extract_marked_block(
        "# >>> A9_GRADE_WINDOW",
        "# <<< A9_GRADE_WINDOW",
    )
    stdin_data = payload if isinstance(payload, str) else json.dumps(payload)
    script = f"{block}\n\na9_grade_window"
    proc = subprocess.run(
        ["bash", "-c", script],
        input=stdin_data,
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0, f"a9_grade_window failed with {proc.returncode}: {proc.stderr}"
    out = proc.stdout.strip()
    parts = out.split("|", 2)
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]


def test_a9_grade_window_pass_matching_advertised():
    payload = {
        "encoder_window": {
            "required_tokens": 8192,
            "embed_max_chars": 24570,
            "embedder": {"advertised_tokens": 8192, "source": "v1_models", "full_payload_ok": True},
            "reranker": {"advertised_tokens": 8192, "source": "v1_models", "full_payload_ok": True},
        }
    }
    verdict, detail, warn = _run_a9_grade_window(payload)
    assert verdict == "OK"
    assert "embed full_payload_ok: true" in detail
    assert "8192" in detail
    assert warn == ""


def test_a9_grade_window_pass_null_advertised():
    # Null advertised allowed if embed empirical true
    payload = {
        "encoder_window": {
            "required_tokens": 8192,
            "embed_max_chars": 24570,
            "embedder": {"advertised_tokens": None, "source": None, "full_payload_ok": True},
            "reranker": {"advertised_tokens": None, "source": None, "full_payload_ok": True},
        }
    }
    verdict, detail, warn = _run_a9_grade_window(payload)
    assert verdict == "OK"
    assert "embed full_payload_ok: true" in detail


def test_a9_grade_window_rerank_empirical_warn_only_unless_short():
    payload = {
        "encoder_window": {
            "required_tokens": 8192,
            "embed_max_chars": 24570,
            "embedder": {"advertised_tokens": 8192, "source": "v1_models", "full_payload_ok": True},
            "reranker": {"advertised_tokens": 8192, "source": "v1_models", "full_payload_ok": False},
        }
    }
    verdict, detail, warn = _run_a9_grade_window(payload)
    assert verdict == "OK"
    assert "reranker full payload probe failed" in warn


def test_a9_grade_window_fail_embed_short():
    payload = {
        "encoder_window": {
            "required_tokens": 8192,
            "embed_max_chars": 24570,
            "embedder": {"advertised_tokens": 512, "source": "v1_models", "full_payload_ok": False},
            "reranker": {"advertised_tokens": 8192, "source": "v1_models", "full_payload_ok": True},
        }
    }
    verdict, detail, warn = _run_a9_grade_window(payload)
    assert verdict == "FAIL_WINDOW_SHORT"
    assert "--max-model-len" in detail
    assert "-c" in detail
    assert "EMBED_MAX_CONTEXT_TOKENS" in detail
    assert "EMBED_MAX_CHARS" in detail


def test_a9_grade_window_fail_rerank_short():
    payload = {
        "encoder_window": {
            "required_tokens": 8192,
            "embed_max_chars": 24570,
            "embedder": {"advertised_tokens": 8192, "source": "v1_models", "full_payload_ok": True},
            "reranker": {"advertised_tokens": 512, "source": "v1_models", "full_payload_ok": False},
        }
    }
    verdict, detail, warn = _run_a9_grade_window(payload)
    assert verdict == "FAIL_WINDOW_SHORT"
    assert "reranker" in detail
    assert "--max-model-len" in detail
    assert "-c" in detail


def test_a9_grade_window_fail_embed_overrun():
    payload = {
        "encoder_window": {
            "required_tokens": 8192,
            "embed_max_chars": 24570,
            "embedder": {"advertised_tokens": 8192, "source": "v1_models", "full_payload_ok": False},
            "reranker": {"advertised_tokens": 8192, "source": "v1_models", "full_payload_ok": True},
        }
    }
    verdict, detail, warn = _run_a9_grade_window(payload)
    assert verdict == "FAIL_EMBED_OVERRUN"
    assert "--max-model-len" in detail
    assert "-c" in detail
    assert "EMBED_MAX_CONTEXT_TOKENS" in detail
    assert "EMBED_MAX_CHARS" in detail


def test_a9_grade_window_still_null():
    payload = {
        "encoder_window": {
            "required_tokens": 8192,
            "embed_max_chars": 24570,
            "embedder": {"advertised_tokens": 8192, "source": "v1_models", "full_payload_ok": None},
            "reranker": {"advertised_tokens": 8192, "source": "v1_models", "full_payload_ok": None},
        }
    }
    verdict, detail, warn = _run_a9_grade_window(payload)
    assert verdict == "STILL_NULL"


def test_a9_grade_window_still_null_when_reranker_unprobed():
    payload = {
        "encoder_window": {
            "required_tokens": 8192,
            "embed_max_chars": 24570,
            "embedder": {"advertised_tokens": 8192, "source": "v1_models", "full_payload_ok": True},
            "reranker": {"advertised_tokens": None, "source": None, "full_payload_ok": None},
        }
    }
    verdict, detail, warn = _run_a9_grade_window(payload)
    assert verdict == "STILL_NULL"


def test_postflight_exit_loops_include_a9():
    text = POSTFLIGHT.read_text()
    # Check that summary loop checks A9
    matches = re.findall(r"for a in ([^;]+); do", text)
    summary_loops = [m for m in matches if "A1" in m]
    assert len(summary_loops) >= 2
    for loop in summary_loops:
        assert "A9" in loop.split(), f"A9 missing from exit loop: {loop}"


def test_a9_premarked_on_missing_token():
    text = POSTFLIGHT.read_text()
    assert "afail[A9]=1" in text


def test_a9_derived_ceiling_used():
    text = POSTFLIGHT.read_text()
    assert "embed_ceiling" in text
    assert "rerank_ceiling" in text
    # A 30 literal must still fail
    a9_section = text[text.find("A9 — encoder window contract:"):]
    assert "ceiling_s=30" not in a9_section
    assert "30s" not in a9_section.split("\n")[0]


def test_a9_encoder_down_skips():
    text = POSTFLIGHT.read_text()
    assert "A9 skipped — embedder backend is down" in text


