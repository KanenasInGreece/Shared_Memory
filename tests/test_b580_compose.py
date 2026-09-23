"""The Arc B580 file is packaged and is not the default encoder image.

Failure this names: an install that sees a B580 is pointed at llama.cpp, or
the default compose file silently becomes the vLLM image.
"""
import json
import os
import subprocess

import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT = os.path.join(ROOT, "shared-memory", "ops", "postgres_neo4j_limits.yaml")
B580 = os.path.join(ROOT, "shared-memory", "ops", "intel-arc-b580.yaml")


def _render(path, tmp_path):
    env = {
        "NEO4J_HOST_DIR": str(tmp_path / "neo4j"),
        "PG_DATA_DIR": str(tmp_path / "pg"),
        "NEO4J_PASSWORD": "x",
        "PG_PASSWORD": "x",
        "GPU_RENDER_GID": "105",
        "GPU_VIDEO_GID": "39",
        "B580_EMBED_MODEL_DIR": str(tmp_path / "bge-m3"),
        "B580_RERANK_MODEL_DIR": str(tmp_path / "bge-reranker"),
    }
    env_file = tmp_path / "compose.env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    proc = subprocess.run(
        ["docker", "compose", "-f", path, "--env-file", str(env_file),
         "config", "--format", "json"],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(subprocess.call(["docker", "compose", "version"],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL) != 0,
                    reason="docker compose is not available")
def test_b580_file_is_vllm_with_the_shim_and_the_default_is_not(tmp_path):
    default = _render(DEFAULT, tmp_path)
    for name in ("retriever-api", "reranker-api", "retriever-api-gpu", "reranker-api-gpu"):
        assert "intel/vllm" not in default["services"][name]["image"]
    assert "b580-rerank-shim" not in default["services"]

    doc = _render(B580, tmp_path)
    assert doc["services"]["b580-embed"]["image"] == "intel/vllm:0.21.0-xpu"
    assert doc["services"]["b580-rerank"]["image"] == "intel/vllm:0.21.0-xpu"
    assert "8192" in doc["services"]["b580-embed"]["command"]
    assert "8192" in doc["services"]["b580-rerank"]["command"]
    shim = doc["services"]["b580-rerank-shim"]
    assert "rerank_shim.py" in " ".join(shim["command"])
    assert shim["environment"]["SHIM_VLLM_URL"] == "http://b580-rerank:8000"
    ports = {
        "b580-embed": ("8091", 8000),
        "b580-rerank": ("8090", 8000),
        "b580-rerank-shim": ("8092", 8092),
    }
    for name, (published, target) in ports.items():
        got = doc["services"][name]["ports"]
        assert got[0]["host_ip"] == "127.0.0.1"
        assert got[0]["published"] == published
        assert got[0]["target"] == target
