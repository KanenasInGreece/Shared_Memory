"""Per-service encoder device switch — the four EMBEDDER_CPU_REPLICAS /
EMBEDDER_GPU_REPLICAS / RERANKER_CPU_REPLICAS / RERANKER_GPU_REPLICAS
compose knobs.

(There is no separate EMBEDDER_DEVICE/RERANKER_DEVICE var — M4 ruling, PR
#308 review: a persisted derived value whose only consumer was a
drift-checker for the divergence its own existence created. These four
replica vars ARE the device choice.)

Compose has no conditionals, so the switch is expressed as a NESTED default:
each per-service replica var (EMBEDDER_CPU_REPLICAS, EMBEDDER_GPU_REPLICAS,
RERANKER_CPU_REPLICAS, RERANKER_GPU_REPLICAS) falls back to the existing
pair-wise var (CPU_ENCODER_REPLICAS, GPU_ENCODER_REPLICAS) when unset —
`${EMBEDDER_GPU_REPLICAS:-${GPU_ENCODER_REPLICAS:-0}}` — so an install that
never sets the per-service vars keeps behaving exactly as it did before this
existed.

This renders the REAL shipped compose file through `docker compose ...
config --format json` (never a hand-copied YAML fragment, so it cannot drift
from what an operator's `up` actually reads) across four cases:

  1. everything unset               → today's default (CPU pair on, GPU off)
  2. pair-wise GPU switch            → both encoders move together (existing
                                        behaviour, must be unaffected)
  3. embedder-only GPU                → the new split: retriever moves, the
                                        reranker's un-set vars still fall
                                        back to the (unset) pair-wise default
  4. reranker-only GPU                → mirror of case 3

Skips cleanly (not a failure) when `docker` / `docker compose` is not on
PATH — this is a rendering check against the real tool, not a reimplementation
of compose's substitution semantics, so it has nothing to assert without it.
"""
import json
import os
import shutil
import subprocess

import pytest

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
COMPOSE_FILE = os.path.join(REPO_ROOT, "shared-memory", "ops", "postgres_neo4j_limits.yaml")

_SERVICES = ("retriever-api", "reranker-api", "retriever-api-gpu", "reranker-api-gpu")


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_compose_available(),
    reason="docker / `docker compose` not available in this environment",
)


def _replicas(tmp_path, env: dict) -> dict:
    """Render the shipped compose file under `env` and return
    {service_name: replicas} for the four encoder services."""
    base = {
        # Required by the file (":?" defaults) — point them at throwaway
        # dirs so `config` (which never starts a container) can render.
        "NEO4J_HOST_DIR": str(tmp_path / "neo4j"),
        "PG_DATA_DIR": str(tmp_path / "pg"),
        "NEO4J_PASSWORD": "x",
        "PG_PASSWORD": "x",
    }
    base.update(env)
    env_file = tmp_path / "compose.env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in base.items()))

    proc = subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, "--env-file", str(env_file),
         "config", "--format", "json"],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, (
        f"docker compose config failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    )
    doc = json.loads(proc.stdout)
    return {name: doc["services"][name]["deploy"]["replicas"] for name in _SERVICES}


def test_unset_matches_todays_default(tmp_path):
    """No vars set at all — the pre-existing CPU pair is what a fresh
    install has always gotten."""
    r = _replicas(tmp_path, {})
    assert r == {
        "retriever-api": 1, "reranker-api": 1,
        "retriever-api-gpu": 0, "reranker-api-gpu": 0,
    }


def test_pairwise_gpu_switch_moves_both_encoders(tmp_path):
    """The EXISTING pair-wise switch must keep working unchanged."""
    r = _replicas(tmp_path, {"CPU_ENCODER_REPLICAS": "0", "GPU_ENCODER_REPLICAS": "1"})
    assert r == {
        "retriever-api": 0, "reranker-api": 0,
        "retriever-api-gpu": 1, "reranker-api-gpu": 1,
    }


def test_embedder_only_gpu_splits_the_pair(tmp_path):
    """The new per-service knob: embedder moves to GPU, reranker's own vars
    are untouched so it falls back to the (unset) pair-wise default — CPU."""
    r = _replicas(tmp_path, {"EMBEDDER_GPU_REPLICAS": "1", "EMBEDDER_CPU_REPLICAS": "0"})
    assert r == {
        "retriever-api": 0, "retriever-api-gpu": 1,
        "reranker-api": 1, "reranker-api-gpu": 0,
    }


def test_reranker_only_gpu_splits_the_pair(tmp_path):
    """Mirror of the embedder-only case — the reranker is the one measured
    NOT to fit an 8192-context window on a 4 GB card, so this direction
    matters less in practice but must still be reachable."""
    r = _replicas(tmp_path, {"RERANKER_GPU_REPLICAS": "1", "RERANKER_CPU_REPLICAS": "0"})
    assert r == {
        "reranker-api": 0, "reranker-api-gpu": 1,
        "retriever-api": 1, "retriever-api-gpu": 0,
    }


def test_per_service_var_overrides_pairwise_even_when_pairwise_disagrees(tmp_path):
    """A per-service var must win over the pair-wise one, not just supplement
    it — otherwise an operator who sets GPU_ENCODER_REPLICAS=0 for the pair
    and then EMBEDDER_GPU_REPLICAS=1 for one service would be silently
    overridden by the pair-wise default instead of getting the split."""
    r = _replicas(tmp_path, {
        "CPU_ENCODER_REPLICAS": "1", "GPU_ENCODER_REPLICAS": "0",
        "EMBEDDER_GPU_REPLICAS": "1", "EMBEDDER_CPU_REPLICAS": "0",
    })
    assert r["retriever-api"] == 0
    assert r["retriever-api-gpu"] == 1
    # reranker untouched by the per-service override — still follows the pair
    assert r["reranker-api"] == 1
    assert r["reranker-api-gpu"] == 0
