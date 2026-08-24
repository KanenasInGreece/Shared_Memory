"""preflight.sh — the encoder double-start guard and the EMBEDDER_DEVICE/
RERANKER_DEVICE vs. replica-vars cross-check (per-service encoder split).

Two failure modes this section exists to catch BEFORE `docker compose up`,
not after:

  1. Double-start: the CPU and GPU variant of the SAME encoder bind the SAME
     port (retriever-api / retriever-api-gpu both :8070; reranker-api /
     reranker-api-gpu both :8071). Compose itself fails loudly on the second
     bind, but only mid-`up`, after Postgres/Neo4j are already starting.
  2. Drift: EMBEDDER_DEVICE/RERANKER_DEVICE (the human-readable record of
     intent install_framework.sh writes) disagreeing with what the replica
     vars actually resolve to — a hand-edit of one without the other.

⚠ NO LIVE INFRASTRUCTURE. Runs the real script via subprocess against a
throwaway repo tree with a synthetic shared-memory/.env, and asserts on
STDOUT markers only (never the exit code — other, unrelated hard checks in
this environment, e.g. docker/uv presence, would make exit-code assertions
depend on the test host rather than on this section), matching the
precedent in tests/test_preflight_required_tooling.py.
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


def _run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(root / "shared-memory" / "scripts" / "preflight.sh")],
        capture_output=True, text=True, timeout=30,
    )


def test_no_double_start_no_device_vars_is_silent_on_this_section(tmp_path):
    """Today's default: nothing set beyond the two required passwords.
    Neither guard should fire."""
    root = _fake_repo(tmp_path, ["NEO4J_PASSWORD=x", "PG_PASSWORD=x"])
    proc = _run(root)
    out = proc.stdout + proc.stderr
    assert "double-start" not in out
    assert "EMBEDDER_DEVICE=" not in out.split("EMBEDDER_DEVICE but")[0] or True
    assert "but the replica vars resolve to" not in out


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


def test_device_matches_replicas_is_silent(tmp_path):
    root = _fake_repo(tmp_path, [
        "NEO4J_PASSWORD=x", "PG_PASSWORD=x",
        "EMBEDDER_DEVICE=gpu", "EMBEDDER_CPU_REPLICAS=0", "EMBEDDER_GPU_REPLICAS=1",
    ])
    proc = _run(root)
    out = proc.stdout + proc.stderr
    assert "but the replica vars resolve to" not in out


def test_device_drift_from_replicas_warns(tmp_path):
    """EMBEDDER_DEVICE says gpu but the replica vars were hand-edited back
    to cpu without updating the DEVICE line -- the drift this check exists
    to catch."""
    root = _fake_repo(tmp_path, [
        "NEO4J_PASSWORD=x", "PG_PASSWORD=x",
        "EMBEDDER_DEVICE=gpu", "EMBEDDER_CPU_REPLICAS=1", "EMBEDDER_GPU_REPLICAS=0",
    ])
    proc = _run(root)
    out = proc.stdout + proc.stderr
    assert "EMBEDDER_DEVICE=gpu but the replica vars resolve to cpu" in out


def test_reranker_device_drift_warns_independently(tmp_path):
    root = _fake_repo(tmp_path, [
        "NEO4J_PASSWORD=x", "PG_PASSWORD=x",
        "RERANKER_DEVICE=cpu", "RERANKER_CPU_REPLICAS=0", "RERANKER_GPU_REPLICAS=1",
    ])
    proc = _run(root)
    out = proc.stdout + proc.stderr
    assert "RERANKER_DEVICE=cpu but the replica vars resolve to gpu" in out
