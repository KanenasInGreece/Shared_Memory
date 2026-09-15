"""Test that install_framework.sh on overwrite mode does not destroy custom keys (A1).

When shared-memory/.env already exists and the operator confirms overwrite,
install_framework.sh must update the six keys in-place in the existing file
rather than rendering from .env.example, so keys like AGENT_TOKENS and
DREAM_TEMPERATURE survive.
"""
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "shared-memory" / "scripts" / "install_framework.sh"
ENV_EXAMPLE = REPO_ROOT / "shared-memory" / ".env.example"
DEFAULTS_PY = REPO_ROOT / "shared-memory" / "scripts" / "framework_defaults.py"


def _setup_repo(tmp_path):
    root = tmp_path / "repo"
    (root / "shared-memory" / "scripts").mkdir(parents=True)
    (root / "shared-memory" / "ops").mkdir(parents=True)
    shutil.copy(INSTALL_SH, root / "shared-memory" / "scripts" / "install_framework.sh")
    shutil.copy(ENV_EXAMPLE, root / "shared-memory" / ".env.example")
    shutil.copy(DEFAULTS_PY, root / "shared-memory" / "scripts" / "framework_defaults.py")
    return root


def test_overwrite_mode_preserves_custom_keys(tmp_path):
    root = _setup_repo(tmp_path)
    env_file = root / "shared-memory" / ".env"
    
    # Pre-existing .env with dummy paths and passwords (no password generation)
    # plus the extra keys that must survive overwrite.
    initial_content = (
        "NEO4J_HOST_DIR=/tmp/dummy_old_neo4j\n"
        "PG_DATA_DIR=/tmp/dummy_old_pg\n"
        "LLM_MODELS_DIR=/tmp/dummy_old_models\n"
        "NEO4J_PASSWORD=dummy_old_neo4j_pw_123\n"
        "PG_PASSWORD=dummy_old_pg_pw_123\n"
        "LLAMA_CPU_THREADS=2\n"
        "AGENT_TOKENS=keep-me\n"
        "DREAM_TEMPERATURE=0.1\n"
    )
    env_file.write_text(initial_content)

    answers = [
        "y",                          # Overwrite? [y/N]
        "/tmp/dummy_new_neo4j",       # NEO4J_HOST_DIR
        "/tmp/dummy_new_pg",          # PG_DATA_DIR
        "/tmp/dummy_new_models",      # LLM_MODELS_DIR
        "",                           # EMBEDDER device (default cpu)
        "",                           # RERANKER device (default cpu)
        "",                           # GPU_RENDER_GID
        "dummy_new_neo4j_pw_123",     # NEO4J_PASSWORD
        "dummy_new_pg_pw_123",        # PG_PASSWORD
        "n",                          # skip systemd
        "n",                          # skip llm backends
    ]
    stdin_text = "".join(f"{a}\n" for a in answers)
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")

    proc = subprocess.run(
        ["bash", str(root / "shared-memory" / "scripts" / "install_framework.sh")],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(root),
    )
    assert proc.returncode == 0, f"install_framework.sh failed: {proc.stderr}\nOutput: {proc.stdout}"

    resulting_env = env_file.read_text()
    assert "AGENT_TOKENS=keep-me" in resulting_env, "AGENT_TOKENS was destroyed on overwrite"
    assert "DREAM_TEMPERATURE=0.1" in resulting_env, "DREAM_TEMPERATURE was destroyed on overwrite"
    assert "NEO4J_HOST_DIR=/tmp/dummy_new_neo4j" in resulting_env
    assert "PG_DATA_DIR=/tmp/dummy_new_pg" in resulting_env
    assert "LLM_MODELS_DIR=/tmp/dummy_new_models" in resulting_env
    assert "NEO4J_PASSWORD=dummy_new_neo4j_pw_123" in resulting_env
    assert "PG_PASSWORD=dummy_new_pg_pw_123" in resulting_env
