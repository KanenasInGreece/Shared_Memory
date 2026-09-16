"""Tests for postflight.sh Check A9 (encoder window contract, decision:2540).

Extracts A9_GRADE_WINDOW from postflight.sh verbatim and verifies grading,
messages naming required phrases (--max-model-len, -c, EMBED_MAX_CONTEXT_TOKENS,
leftover EMBED_MAX_CHARS), and postflight flow.
"""
import json
import os
import re
import shlex
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
    assert verdict == "STILL_NULL_EMBED"


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
    assert verdict == "STILL_NULL_RERANK"


A9_SECTION_START = "# ── A9 — encoder window contract"
SUMMARY_SECTION_START = "# ── Summary"


def _extract_a9_section() -> str:
    text = POSTFLIGHT.read_text()
    start = text.find(A9_SECTION_START)
    end = text.find(SUMMARY_SECTION_START)
    assert start != -1, f"could not find {A9_SECTION_START!r} in {POSTFLIGHT}"
    assert end != -1 and end > start, (
        f"could not find {SUMMARY_SECTION_START!r} after the A9 header in {POSTFLIGHT}"
    )
    return text[start:end]


def run_a9_live(*, health_full: dict | str, auth_on="0", token_missing="0",
                gateway_down="0", agent_token="tok"):
    a9_grade_window_def = _extract_marked_block(
        "# >>> A9_GRADE_WINDOW",
        "# <<< A9_GRADE_WINDOW",
    )
    a9_section = _extract_a9_section()
    health_str = health_full if isinstance(health_full, str) else json.dumps(health_full)
    script_dir = str(POSTFLIGHT.parent)
    lines = [
        "set -uo pipefail",
        f"SCRIPT_DIR={shlex.quote(script_dir)}",
        f"GATEWAY_URL='http://127.0.0.1:8888'",
        f"auth_on={shlex.quote(auth_on)}",
        f"token_missing={shlex.quote(token_missing)}",
        f"gateway_down={shlex.quote(gateway_down)}",
        f"AGENT_TOKEN={shlex.quote(agent_token)}",
        f"health_full={shlex.quote(health_str)}",
        "declare -A afail",
        "red()   { printf '\\033[31m%s\\033[0m\\n' \"$*\"; }",
        "grn()   { printf '\\033[32m%s\\033[0m\\n' \"$*\"; }",
        "ylw()   { printf '\\033[33m%s\\033[0m\\n' \"$*\"; }",
        'ok()   { echo "OK: $*"; }',
        'warn() { echo "WARN: $*"; }',
        'bad()  { local a="$1"; shift; echo "BAD: $a $*"; afail["$a"]=1; }',
        'sleep() { SECONDS=$(( SECONDS + 100 )); }',
        'curl() { printf \'%s\' "$health_full"; }',
        a9_grade_window_def,
        a9_section,
        'echo "AFAIL_A9=${afail[A9]:-0}"',
    ]
    harness = "\n".join(lines)
    return subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_a9_postflight_timeout_rerank_still_null_warns_and_passes():
    payload = {
        "dependencies": {"embedder": {"state": "ok"}},
        "embedder": "ok",
        "encoder_window": {
            "required_tokens": 8192,
            "embed_max_chars": 24570,
            "embedder": {"advertised_tokens": 8192, "source": "v1_models", "full_payload_ok": True},
            "reranker": {"advertised_tokens": None, "source": None, "full_payload_ok": None},
        },
    }
    result = run_a9_live(health_full=payload)
    assert result.returncode == 0, f"script failed: {result.stderr}"
    assert "WARN: A9 reranker probe" in result.stdout
    assert "skip-null" in result.stdout
    assert "OK: A9 encoder window contract verified" in result.stdout
    assert "BAD: A9" not in result.stdout
    assert "AFAIL_A9=0" in result.stdout


def test_a9_postflight_timeout_embed_still_null_fails():
    payload = {
        "dependencies": {"embedder": {"state": "ok"}},
        "embedder": "ok",
        "encoder_window": {
            "required_tokens": 8192,
            "embed_max_chars": 24570,
            "embedder": {"advertised_tokens": 8192, "source": "v1_models", "full_payload_ok": None},
            "reranker": {"advertised_tokens": 8192, "source": "v1_models", "full_payload_ok": True},
        },
    }
    result = run_a9_live(health_full=payload)
    assert result.returncode == 0, f"script failed: {result.stderr}"
    assert "BAD: A9 encoder window probe timed out" in result.stdout
    assert "AFAIL_A9=1" in result.stdout


def test_a9_postflight_rerank_advertised_short_fails():
    payload = {
        "dependencies": {"embedder": {"state": "ok"}},
        "embedder": "ok",
        "encoder_window": {
            "required_tokens": 8192,
            "embed_max_chars": 24570,
            "embedder": {"advertised_tokens": 8192, "source": "v1_models", "full_payload_ok": True},
            "reranker": {"advertised_tokens": 512, "source": "v1_models", "full_payload_ok": False},
        },
    }
    result = run_a9_live(health_full=payload)
    assert result.returncode == 0, f"script failed: {result.stderr}"
    assert "BAD: A9 reranker advertised window 512 < required 8192" in result.stdout
    assert "AFAIL_A9=1" in result.stdout


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
    a9 = _extract_a9_section()
    assert "embed_ceiling" in a9
    assert "rerank_ceiling" in a9
    assert "ceiling_s=30" not in a9
    assert "30s" not in a9.split("\n")[0]
    # Substring of embed_ceiling is not enough: pin the wait loop that
    # re-curls /health while the probe is still null, then skip-null
    # STILL_NULL_RERANK (ok, not bad A9). Mutation: drop curl from the
    # loop body → this fails.
    loop = re.search(r"while \[\[(.*?)\]\]; do\n(.*?)done", a9, re.S)
    assert loop, "A9 wait loop (while STILL_NULL_* re-grade) missing from postflight.sh"
    cond, body = loop.group(1), loop.group(2)
    assert "STILL_NULL_EMBED" in cond
    assert "STILL_NULL_RERANK" in cond
    assert 'verdict" == "STILL_NULL"' in cond or '"STILL_NULL"' in cond
    assert re.search(r"\bcurl\b", body), "wait loop must re-curl /health"
    assert "/health" in body
    rerank_arm = re.search(r"STILL_NULL_RERANK\)\s*(.*?);;", a9, re.S)
    assert rerank_arm, "STILL_NULL_RERANK case arm missing"
    arm = rerank_arm.group(1)
    assert re.search(r"\bok\b", arm), "STILL_NULL_RERANK after timeout must ok, not fail A9"
    assert "skip-null" in arm
    assert not re.search(r"\bbad A9\b", arm)
    embed_arm = re.search(r"STILL_NULL\|STILL_NULL_EMBED\)\s*(.*?);;", a9, re.S)
    assert embed_arm, "STILL_NULL_EMBED timeout arm missing"
    assert re.search(r"\bbad A9\b", embed_arm.group(1))


def test_a9_encoder_down_skips():
    text = POSTFLIGHT.read_text()
    assert "A9 skipped — embedder backend is down" in text


