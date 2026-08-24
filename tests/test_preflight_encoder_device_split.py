"""preflight.sh — the encoder double-start guard (per-service encoder split).

Catches, BEFORE `docker compose up`, the CPU and GPU variant of the SAME
encoder binding the SAME port (retriever-api / retriever-api-gpu both
:8070; reranker-api / reranker-api-gpu both :8071). Compose itself fails
loudly on the second bind, but only mid-`up`, after Postgres/Neo4j are
already starting.

M4 ruling (PR #308 review, operator-adjudicated 2026-08-24): there is no
EMBEDDER_DEVICE/RERANKER_DEVICE var, and therefore no drift check against
it -- the double-start guard below is the section's only verdict.

H1 (PR #308 review): `read_env`'s raw `grep | cut` output used to be
compared directly against the string "0", so a value that renders
CORRECTLY through compose -- `EMBEDDER_CPU_REPLICAS="0"` (quoted), `=0 #
keep off` (inline comment), a CRLF-saved .env -- tripped a false
double-start ✗ and preflight's exit 1. `read_env` now normalises (strips a
trailing CR, a trailing inline comment, one layer of matched quotes)
before comparing.

⚠ NO LIVE INFRASTRUCTURE. Runs the real script via subprocess against a
throwaway repo tree with a synthetic shared-memory/.env, matching the
precedent in tests/test_preflight_required_tooling.py. Most assertions are
on STDOUT markers only (never the exit code — other, unrelated hard
checks in this environment, e.g. docker/uv presence, would make exit-code
assertions depend on the test host). The H1 regression tests are the
exception: they plant both encoder GGUF files under a synthetic
LLM_MODELS_DIR first (`_fake_repo_with_models`), which removes the one
OTHER check in this section that can independently fail, so $? becomes a
direct, meaningful proof of what this guard alone decided.
"""
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PREFLIGHT = REPO_ROOT / "shared-memory" / "scripts" / "preflight.sh"


def _fake_repo(tmp_path, env_lines: list[str]) -> Path:
    root = tmp_path / "repo"
    (root / "shared-memory" / "scripts").mkdir(parents=True)
    shutil.copy(PREFLIGHT, root / "shared-memory" / "scripts" / "preflight.sh")
    (root / "shared-memory" / ".env").write_text("".join(f"{l}\n" for l in env_lines))
    return root


def _fake_repo_with_models(tmp_path, env_lines: list[str]) -> Path:
    """Same as _fake_repo, but also plants both encoder GGUF files at the
    compose defaults' subpaths under a synthetic LLM_MODELS_DIR, so the
    unrelated GGUF-presence check (bad() on missing files, which fires by
    default since CPU_ENCODER_REPLICAS defaults to 1) cannot ALSO
    contribute to the exit code -- isolating the double-start guard's own
    contribution to $?."""
    root = tmp_path / "repo"
    (root / "shared-memory" / "scripts").mkdir(parents=True)
    shutil.copy(PREFLIGHT, root / "shared-memory" / "scripts" / "preflight.sh")
    models = tmp_path / "models"
    (models / "gpustack" / "bge-m3-GGUF").mkdir(parents=True)
    (models / "gpustack" / "bge-reranker-v2-m3-GGUF").mkdir(parents=True)
    (models / "gpustack" / "bge-m3-GGUF" / "bge-m3-Q8_0.gguf").write_text("x")
    (models / "gpustack" / "bge-reranker-v2-m3-GGUF" / "bge-reranker-v2-m3-Q8_0.gguf").write_text("x")
    lines = list(env_lines) + [f"LLM_MODELS_DIR={models}"]
    (root / "shared-memory" / ".env").write_text("".join(f"{l}\n" for l in lines))
    return root


def _run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(root / "shared-memory" / "scripts" / "preflight.sh")],
        capture_output=True, text=True, timeout=30,
    )


def test_no_double_start_no_encoder_vars_is_silent_on_this_section(tmp_path):
    """Today's default: nothing set beyond the two required passwords."""
    root = _fake_repo(tmp_path, ["NEO4J_PASSWORD=x", "PG_PASSWORD=x"])
    proc = _run(root)
    out = proc.stdout + proc.stderr
    assert "double-start" not in out


def test_embedder_double_start_reported_loudly(tmp_path):
    root = _fake_repo(tmp_path, [
        "NEO4J_PASSWORD=x", "PG_PASSWORD=x",
        "EMBEDDER_CPU_REPLICAS=1", "EMBEDDER_GPU_REPLICAS=1",
    ])
    proc = _run(root)
    out = proc.stdout + proc.stderr
    assert "embedder would double-start" in out
    assert ":8070" in out
    assert "reranker would double-start" not in out


def test_reranker_double_start_reported_loudly(tmp_path):
    root = _fake_repo(tmp_path, [
        "NEO4J_PASSWORD=x", "PG_PASSWORD=x",
        "RERANKER_CPU_REPLICAS=1", "RERANKER_GPU_REPLICAS=1",
    ])
    proc = _run(root)
    out = proc.stdout + proc.stderr
    assert "reranker would double-start" in out
    assert ":8071" in out
    assert "embedder would double-start" not in out


def test_pairwise_gpu_switch_does_not_false_positive_the_double_start_guard(tmp_path):
    """The ordinary pair-wise switch (both encoders move to GPU together)
    must NOT trip the double-start guard — CPU_ENCODER_REPLICAS=0 zeroes
    both CPU sides."""
    root = _fake_repo(tmp_path, [
        "NEO4J_PASSWORD=x", "PG_PASSWORD=x",
        "CPU_ENCODER_REPLICAS=0", "GPU_ENCODER_REPLICAS=1",
    ])
    proc = _run(root)
    out = proc.stdout + proc.stderr
    assert "double-start" not in out


# ── H1 regression: three real spellings that render correctly through
# compose but used to false-positive the guard, plus the genuine half-set
# that must still be caught ────────────────────────────────────────────────

def test_h1_quoted_zero_does_not_double_start_and_exits_clean(tmp_path):
    root = _fake_repo_with_models(tmp_path, [
        "NEO4J_PASSWORD=x", "PG_PASSWORD=x",
        'EMBEDDER_CPU_REPLICAS="0"', "EMBEDDER_GPU_REPLICAS=1",
    ])
    proc = _run(root)
    out = proc.stdout + proc.stderr
    assert "double-start" not in out
    assert proc.returncode == 0, out


def test_h1_inline_comment_does_not_double_start_and_exits_clean(tmp_path):
    root = _fake_repo_with_models(tmp_path, [
        "NEO4J_PASSWORD=x", "PG_PASSWORD=x",
        "EMBEDDER_CPU_REPLICAS=0 # keep off", "EMBEDDER_GPU_REPLICAS=1",
    ])
    proc = _run(root)
    out = proc.stdout + proc.stderr
    assert "double-start" not in out
    assert proc.returncode == 0, out


def test_h1_crlf_env_does_not_double_start_and_exits_clean(tmp_path):
    root = _fake_repo_with_models(tmp_path, [
        "NEO4J_PASSWORD=x", "PG_PASSWORD=x",
        "EMBEDDER_CPU_REPLICAS=0", "EMBEDDER_GPU_REPLICAS=1",
    ])
    # _fake_repo_with_models writes plain \n; convert the WHOLE file to CRLF
    # after the fact, matching a real CRLF-saved .env rather than a
    # hand-crafted single line.
    env_path = root / "shared-memory" / ".env"
    text = env_path.read_text()
    env_path.write_bytes(text.replace("\n", "\r\n").encode())
    proc = _run(root)
    out = proc.stdout + proc.stderr
    assert "double-start" not in out
    assert proc.returncode == 0, out


def test_h1_genuine_half_set_still_exits_1(tmp_path):
    """The regression guard for H1's fix: a REAL half-set (no CPU replicas
    var at all, so it silently falls back to the pair-wise default 1) must
    still be caught -- normalising the read must not swallow genuine
    double-starts along with the false positives."""
    root = _fake_repo_with_models(tmp_path, [
        "NEO4J_PASSWORD=x", "PG_PASSWORD=x",
        "EMBEDDER_GPU_REPLICAS=1",
    ])
    proc = _run(root)
    out = proc.stdout + proc.stderr
    assert "embedder would double-start" in out
    assert proc.returncode == 1, out


def test_h1_non_integer_value_warns_and_skips_rather_than_hard_fails(tmp_path):
    """An unparseable replica value cannot be safely compared -- warn and
    skip this encoder's verdict rather than mis-flag it as a double-start
    (compose itself already fails loudly on a bad `replicas:` value at
    `up`/`config` time)."""
    root = _fake_repo_with_models(tmp_path, [
        "NEO4J_PASSWORD=x", "PG_PASSWORD=x",
        "EMBEDDER_CPU_REPLICAS=notanumber", "EMBEDDER_GPU_REPLICAS=1",
    ])
    proc = _run(root)
    out = proc.stdout + proc.stderr
    assert "not a plain integer" in out
    assert "embedder would double-start" not in out
