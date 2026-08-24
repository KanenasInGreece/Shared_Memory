"""install_framework.sh — Q3b (AGENTS.md), the per-service encoder device
split interview.

Runs the REAL shipped script end-to-end via subprocess, feeding scripted
stdin answers exactly the way AGENTS.md's Phase 1 drives it for an
agent-run install, against a throwaway install tree (never the real repo
or a real $HOME). The two device prompts are UNCONDITIONAL (always asked,
same fixed-length answer sequence every time, regardless of the
LLM_MODELS_DIR answer) -- see tests/test_change_group_contracts.py's
prompt-count guard, which is exactly what forces this shape: a script whose
prompt count depends on an earlier answer's value breaks AGENTS.md's single
fixed printf that pipes all the answers in one shot.

Covers:
  - accepting the "cpu" default for both (blank/Enter) writes NOTHING new --
    the existing pair-wise CPU_ENCODER_REPLICAS/GPU_ENCODER_REPLICAS in the
    template keep deciding exactly as before this question existed
  - answering embedder=gpu, reranker=cpu writes the six lines LIVE
    (uncommented) at the end of the file, distinct from the commented
    template placeholders
  - the appended lines are never inserted INTO the commented template
    section (grep must find exactly one live EMBEDDER_DEVICE= line)
  - an unrecognised answer (neither "cpu" nor "gpu") is treated as cpu with
    a warning, never silently accepted as-is or fatal
"""
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
INSTALL_SH = REPO_ROOT / "shared-memory" / "scripts" / "install_framework.sh"
ENV_EXAMPLE = REPO_ROOT / "shared-memory" / ".env.example"


def _fake_install(tmp_path):
    root = tmp_path / "repo"
    (root / "shared-memory" / "scripts").mkdir(parents=True)
    (root / "shared-memory" / "ops").mkdir(parents=True)
    shutil.copy(INSTALL_SH, root / "shared-memory" / "scripts" / "install_framework.sh")
    shutil.copy(ENV_EXAMPLE, root / "shared-memory" / ".env.example")
    return root


def _run(root, answers: list[str], home) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = str(home)
    stdin_text = "".join(f"{a}\n" for a in answers)
    return subprocess.run(
        ["bash", str(root / "shared-memory" / "scripts" / "install_framework.sh")],
        input=stdin_text, capture_output=True, text=True, timeout=60, env=env,
        cwd=str(root),
    )


def _base_answers(models_dir: str, embedder="", reranker="") -> list[str]:
    """The seven answers every install needs: two dirs, models dir, two
    encoder-device answers (blank accepts the "cpu" default), two passwords
    (>8 chars, no '/')."""
    return [
        "",                            # NEO4J_HOST_DIR -- accept default
        "",                            # PG_DATA_DIR -- accept default
        models_dir,                   # LLM_MODELS_DIR
        embedder,                     # EMBEDDER_DEVICE
        reranker,                     # RERANKER_DEVICE
        "supersecret_neo4j_pw_123",   # NEO4J_PASSWORD
        "supersecret_pg_pw_123",      # PG_PASSWORD
    ]


def test_accepting_cpu_default_for_both_writes_nothing_new(tmp_path):
    root = _fake_install(tmp_path)
    home = tmp_path / "home"
    answers = _base_answers(str(tmp_path / "models")) + [
        "n",   # skip systemd service install
        "n",   # skip reasoning-LLM backend config
    ]
    proc = _run(root, answers, home)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    env_text = (root / "shared-memory" / ".env").read_text()
    live = [l for l in env_text.splitlines() if not l.strip().startswith("#")]
    assert not any(l.startswith("EMBEDDER_DEVICE=") for l in live)
    assert not any(l.startswith("RERANKER_DEVICE=") for l in live)


def test_embedder_gpu_reranker_cpu_split_written_live(tmp_path):
    root = _fake_install(tmp_path)
    home = tmp_path / "home"
    answers = _base_answers(str(tmp_path / "models"), embedder="gpu", reranker="cpu") + [
        "n",   # skip systemd service install
        "n",   # skip reasoning-LLM backend config
    ]
    proc = _run(root, answers, home)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "EMBEDDER_DEVICE=gpu RERANKER_DEVICE=cpu" in proc.stdout

    env_text = (root / "shared-memory" / ".env").read_text()
    live = [l for l in env_text.splitlines() if not l.strip().startswith("#")]
    assert "EMBEDDER_DEVICE=gpu" in live
    assert "EMBEDDER_CPU_REPLICAS=0" in live
    assert "EMBEDDER_GPU_REPLICAS=1" in live
    assert "RERANKER_DEVICE=cpu" in live
    # RERANKER_CPU_REPLICAS/RERANKER_GPU_REPLICAS are NOT written when the
    # reranker stayed at its "cpu" default -- the nested compose default
    # (RERANKER_CPU_REPLICAS:-CPU_ENCODER_REPLICAS:-1) already resolves to 1
    # with nothing written, so there is nothing to add for this encoder.
    assert not any(l.startswith("RERANKER_CPU_REPLICAS=") for l in live)
    assert not any(l.startswith("RERANKER_GPU_REPLICAS=") for l in live)
    # Exactly one LIVE line per key -- the append must not duplicate into
    # the commented template section higher up the same file.
    assert sum(1 for l in live if l.startswith("EMBEDDER_DEVICE=")) == 1


def test_both_gpu_writes_all_four_replica_lines(tmp_path):
    root = _fake_install(tmp_path)
    home = tmp_path / "home"
    answers = _base_answers(str(tmp_path / "models"), embedder="gpu", reranker="gpu") + [
        "n", "n",
    ]
    proc = _run(root, answers, home)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    env_text = (root / "shared-memory" / ".env").read_text()
    live = [l for l in env_text.splitlines() if not l.strip().startswith("#")]
    assert "EMBEDDER_GPU_REPLICAS=1" in live
    assert "RERANKER_GPU_REPLICAS=1" in live


def test_unrecognised_answer_treated_as_cpu_with_a_warning(tmp_path):
    root = _fake_install(tmp_path)
    home = tmp_path / "home"
    answers = _base_answers(str(tmp_path / "models"), embedder="banana", reranker="cpu") + [
        "n", "n",
    ]
    proc = _run(root, answers, home)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout + proc.stderr
    assert "unrecognised embedder device 'banana'" in out
    env_text = (root / "shared-memory" / ".env").read_text()
    live = [l for l in env_text.splitlines() if not l.strip().startswith("#")]
    # Coerced to cpu -- never a literal "banana" written, and (both effectively
    # cpu) nothing appended at all.
    assert not any(l.startswith("EMBEDDER_DEVICE=") for l in live)


def test_blank_models_dir_still_asks_the_device_questions(tmp_path):
    """Q2's 'existing endpoint' branch (blank LLM_MODELS_DIR) does NOT skip
    the device prompts -- they must stay unconditional so the fixed-length
    piped-answer sequence never depends on an earlier answer's value.

    `read -p`'s prompt text itself is suppressed under piped (non-tty)
    stdin (bash only displays it on a real terminal), so this checks for
    the guidance line printed via plain `echo` right before the two
    prompts -- present only if that code path ran -- and that the run still
    consumed exactly 7 leading answers (proc.returncode == 0 with a correct
    account of them; a consumed-too-few/too-many run would desync the
    trailing "n"/"n" answers and this would still exit 0 by coincidence, so
    the real proof is the .env content in the sibling tests)."""
    root = _fake_install(tmp_path)
    home = tmp_path / "home"
    answers = _base_answers("") + [
        "n", "n",
    ]
    proc = _run(root, answers, home)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout + proc.stderr
    assert "embedder fits comfortably" in out
