"""Tests for compose encoder command pin and preflight zero-replicas warning (decision:2540).

Verifies:
1. Every encoder service in postgres_neo4j_limits.yaml pins `-c 8192 -b 8192 -ub 8192`
   as a literal (not an env var).
2. preflight.sh emits a warning when all encoder replicas are 0, reminding the operator
   that remote encoders must serve EMBED_MAX_CONTEXT_TOKENS via --max-model-len or -c,
   and that postflight A9 will gate this.
"""
import re
import shutil
import subprocess
import yaml
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).parent.parent
COMPOSE_FILE = REPO_ROOT / "shared-memory" / "ops" / "postgres_neo4j_limits.yaml"
PREFLIGHT = REPO_ROOT / "shared-memory" / "scripts" / "preflight.sh"


def test_compose_encoder_commands_pin_8192_literal():
    text = COMPOSE_FILE.read_text()
    data = yaml.safe_load(text)
    services = data.get("services", {})

    encoder_services = [
        "retriever-api",
        "reranker-api",
        "retriever-api-gpu",
        "reranker-api-gpu",
    ]

    for name in encoder_services:
        assert name in services, f"Encoder service {name} missing from compose file"
        cmd = services[name].get("command", "")
        if isinstance(cmd, list):
            cmd_str = " ".join(cmd)
        else:
            cmd_str = str(cmd)

        assert "-c 8192" in cmd_str, f"Service {name} does not pin -c 8192 in command: {cmd_str}"
        assert "-b 8192" in cmd_str, f"Service {name} does not pin -b 8192 in command: {cmd_str}"
        assert "-ub 8192" in cmd_str, f"Service {name} does not pin -ub 8192 in command: {cmd_str}"
        assert "-c ${" not in cmd_str, f"Service {name} parameterizes -c as an env var; must be locked literal"


def _run_preflight(tmp_path: Path, env_lines: list[str]) -> subprocess.CompletedProcess:
    root = tmp_path / "repo"
    (root / "shared-memory" / "scripts").mkdir(parents=True)
    shutil.copy(PREFLIGHT, root / "shared-memory" / "scripts" / "preflight.sh")
    shutil.copy(PREFLIGHT.parent / "read_env_key.py", root / "shared-memory" / "scripts" / "read_env_key.py")
    shutil.copy(PREFLIGHT.parent / "secure_env.py", root / "shared-memory" / "scripts" / "secure_env.py")
    (root / "shared-memory" / ".env").write_text("".join(f"{l}\n" for l in env_lines))
    return subprocess.run(
        ["bash", "shared-memory/scripts/preflight.sh"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_preflight_warns_when_all_encoder_replicas_zero(tmp_path):
    env_lines = [
        "CPU_ENCODER_REPLICAS=0",
        "GPU_ENCODER_REPLICAS=0",
        "EMBEDDER_CPU_REPLICAS=0",
        "EMBEDDER_GPU_REPLICAS=0",
        "RERANKER_CPU_REPLICAS=0",
        "RERANKER_GPU_REPLICAS=0",
        "EMBED_MAX_CONTEXT_TOKENS=8192",
    ]
    res = _run_preflight(tmp_path, env_lines)
    stdout = res.stdout

    # Must emit a warning
    assert "warn" in stdout.lower() or "⚠" in stdout
    # Must mention remote encoder context window requirements
    assert "--max-model-len" in stdout
    assert "-c" in stdout
    assert "EMBED_MAX_CONTEXT_TOKENS" in stdout or "8192" in stdout
    assert "A9" in stdout
