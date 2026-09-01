"""install_framework.sh — Q3b (AGENTS.md), the per-service encoder device
split interview.

Runs the REAL shipped script end-to-end via subprocess, feeding scripted
stdin answers exactly the way AGENTS.md's Phase 1 drives it for an
agent-run install, against a throwaway install tree (never the real repo
or a real $HOME). The three encoder prompts (embedder device, reranker
device, GPU render-node group id) are UNCONDITIONAL (always asked, same
fixed-length answer sequence every time, regardless of the LLM_MODELS_DIR
answer or the device answers) -- see tests/test_change_group_contracts.py's
prompt-count guard, which is exactly what forces this shape: a script whose
prompt count depends on an earlier answer's value breaks AGENTS.md's single
fixed printf that pipes all the answers in one shot.

M4 ruling (PR #308 review, operator-adjudicated 2026-08-24): there is no
EMBEDDER_DEVICE/RERANKER_DEVICE var written to .env at all -- it was a
persisted derived value (decision:1032) whose only consumer was a
drift-checker for the divergence its own existence created. The two device
answers decide ONLY the four per-service replica vars.

Covers:
  - accepting the "cpu" default for both (blank/Enter) writes NOTHING new --
    the existing pair-wise CPU_ENCODER_REPLICAS/GPU_ENCODER_REPLICAS in the
    template keep deciding exactly as before this question existed
  - answering embedder=gpu, reranker=cpu writes ALL FOUR per-service replica
    lines live (uncommented) -- M3: not only the moved encoder's pair, so
    the rendered compose never depends on a pair-wise fallback once Q3b has
    answered for it
  - GPU_RENDER_GID is written (M2) when either encoder goes gpu, and NOT
    written when both stay cpu
  - no EMBEDDER_DEVICE/RERANKER_DEVICE line is EVER written (M4)
  - device answers are case-insensitive ("GPU"/"Gpu" normalise to "gpu")
    before being acted on or written (L3)
  - an unrecognised answer (neither "cpu" nor "gpu", in any case) is treated
    as cpu with a warning, never silently accepted as-is or fatal
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
    # W5 (R-D): the encoder-endpoint append-block shells out to
    # framework_defaults.py (never a second literal in bash) -- a genuine
    # runtime dependency of every install now, not just the tests that
    # assert on its output.
    shutil.copy(REPO_ROOT / "shared-memory" / "scripts" / "framework_defaults.py",
                root / "shared-memory" / "scripts" / "framework_defaults.py")
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


def _base_answers(models_dir: str, embedder="", reranker="", render_gid="") -> list[str]:
    """The eight answers every install needs: two dirs, models dir, two
    encoder-device answers (blank accepts the "cpu" default), one render-gid
    answer (blank accepts the auto-detected/fallback default), two
    passwords (>8 chars, no '/')."""
    return [
        "",                            # NEO4J_HOST_DIR -- accept default
        "",                            # PG_DATA_DIR -- accept default
        models_dir,                   # LLM_MODELS_DIR
        embedder,                     # EMBEDDER device
        reranker,                     # RERANKER device
        render_gid,                   # GPU_RENDER_GID
        "supersecret_neo4j_pw_123",   # NEO4J_PASSWORD
        "supersecret_pg_pw_123",      # PG_PASSWORD
    ]


def _live_lines(root):
    env_text = (root / "shared-memory" / ".env").read_text()
    return [l for l in env_text.splitlines() if not l.strip().startswith("#")]


def test_accepting_cpu_default_for_both_writes_nothing_new(tmp_path):
    root = _fake_install(tmp_path)
    home = tmp_path / "home"
    answers = _base_answers(str(tmp_path / "models")) + [
        "n",   # skip systemd service install
        "n",   # skip reasoning-LLM backend config
    ]
    proc = _run(root, answers, home)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    live = _live_lines(root)
    assert not any(l.startswith("EMBEDDER_CPU_REPLICAS=") for l in live)
    assert not any(l.startswith("EMBEDDER_GPU_REPLICAS=") for l in live)
    assert not any(l.startswith("RERANKER_CPU_REPLICAS=") for l in live)
    assert not any(l.startswith("RERANKER_GPU_REPLICAS=") for l in live)
    assert not any(l.startswith("GPU_RENDER_GID=") for l in live)


def test_no_device_var_is_ever_written(tmp_path):
    """M4: EMBEDDER_DEVICE/RERANKER_DEVICE must never appear live in .env,
    regardless of the answers -- there is no such var any more."""
    root = _fake_install(tmp_path)
    home = tmp_path / "home"
    answers = _base_answers(str(tmp_path / "models"), embedder="gpu", reranker="gpu") + [
        "n", "n",
    ]
    proc = _run(root, answers, home)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    live = _live_lines(root)
    assert not any(l.startswith("EMBEDDER_DEVICE=") for l in live)
    assert not any(l.startswith("RERANKER_DEVICE=") for l in live)


def test_embedder_gpu_reranker_cpu_writes_all_four_replica_lines(tmp_path):
    """M3: ALL FOUR per-service replica lines are written, not only the
    moved encoder's pair -- the reranker's cpu=1/gpu=0 pair must be
    explicit too, so the rendered compose never falls back to the pair-wise
    default for it."""
    root = _fake_install(tmp_path)
    home = tmp_path / "home"
    answers = _base_answers(str(tmp_path / "models"), embedder="gpu", reranker="cpu") + [
        "n",   # skip systemd service install
        "n",   # skip reasoning-LLM backend config
    ]
    proc = _run(root, answers, home)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "embedder=gpu reranker=cpu" in proc.stdout

    live = _live_lines(root)
    assert "EMBEDDER_CPU_REPLICAS=0" in live
    assert "EMBEDDER_GPU_REPLICAS=1" in live
    assert "RERANKER_CPU_REPLICAS=1" in live
    assert "RERANKER_GPU_REPLICAS=0" in live
    # Exactly one LIVE line per key -- the append must not duplicate into
    # the commented template section higher up the same file.
    assert sum(1 for l in live if l.startswith("EMBEDDER_CPU_REPLICAS=")) == 1


def test_both_gpu_writes_all_four_replica_lines_and_render_gid(tmp_path):
    root = _fake_install(tmp_path)
    home = tmp_path / "home"
    answers = _base_answers(str(tmp_path / "models"), embedder="gpu", reranker="gpu") + [
        "n", "n",
    ]
    proc = _run(root, answers, home)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    live = _live_lines(root)
    assert "EMBEDDER_GPU_REPLICAS=1" in live
    assert "RERANKER_GPU_REPLICAS=1" in live
    assert any(l.startswith("GPU_RENDER_GID=") for l in live)
    assert any(l.startswith("ENCODER_GPU_INDEX=") for l in live)


def test_render_gid_answer_is_written_verbatim_when_gpu_chosen(tmp_path):
    root = _fake_install(tmp_path)
    home = tmp_path / "home"
    answers = _base_answers(str(tmp_path / "models"), embedder="gpu", reranker="cpu",
                             render_gid="992") + [
        "n", "n",
    ]
    proc = _run(root, answers, home)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    live = _live_lines(root)
    assert "GPU_RENDER_GID=992" in live


def test_device_answers_case_insensitive(tmp_path):
    """L3: 'GPU'/'Gpu' must mean the same as 'gpu', not fall through to the
    unrecognised-answer branch."""
    root = _fake_install(tmp_path)
    home = tmp_path / "home"
    answers = _base_answers(str(tmp_path / "models"), embedder="GPU", reranker="Cpu") + [
        "n", "n",
    ]
    proc = _run(root, answers, home)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout + proc.stderr
    assert "unrecognised" not in out
    live = _live_lines(root)
    assert "EMBEDDER_GPU_REPLICAS=1" in live
    assert "RERANKER_CPU_REPLICAS=1" in live


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
    live = _live_lines(root)
    # Coerced to cpu -- never a literal "banana" written, and (both
    # effectively cpu) nothing appended at all.
    assert not any(l.startswith("EMBEDDER_CPU_REPLICAS=") for l in live)


# ── W5 (R-D, decision:1824 §3) — encoder ENDPOINTS written explicitly ──────

def test_embedder_reranker_url_written_explicitly_with_framework_defaults(tmp_path):
    """The shipped compose's default encoder endpoints are no longer an
    invisible commented-out fallback -- install_framework.sh writes them
    explicitly, sourced from framework_defaults.py (never a second literal
    in bash)."""
    root = _fake_install(tmp_path)
    home = tmp_path / "home"
    answers = _base_answers(str(tmp_path / "models")) + ["n", "n"]
    proc = _run(root, answers, home)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    live = _live_lines(root)
    assert "EMBEDDER_URL=http://localhost:8070" in live
    assert "RERANKER_URL=http://localhost:8071" in live
    # Exactly one LIVE line per key -- never duplicated into the commented
    # template section higher up the same file.
    assert sum(1 for l in live if l.startswith("EMBEDDER_URL=")) == 1
    assert sum(1 for l in live if l.startswith("RERANKER_URL=")) == 1


def test_embedder_reranker_url_written_regardless_of_device_answers(tmp_path):
    """Unconditional -- unlike the per-service replica vars, the endpoint
    lines are written even when both encoders stay on cpu (the common
    case)."""
    root = _fake_install(tmp_path)
    home = tmp_path / "home"
    answers = _base_answers(str(tmp_path / "models"), embedder="gpu", reranker="gpu") + ["n", "n"]
    proc = _run(root, answers, home)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    live = _live_lines(root)
    assert any(l.startswith("EMBEDDER_URL=") for l in live)
    assert any(l.startswith("RERANKER_URL=") for l in live)


def test_blank_models_dir_still_asks_the_device_questions(tmp_path):
    """Q2's 'existing endpoint' branch (blank LLM_MODELS_DIR) does NOT skip
    the device/render-gid prompts -- they must stay unconditional so the
    fixed-length piped-answer sequence never depends on an earlier answer's
    value.

    `read -p`'s prompt text itself is suppressed under piped (non-tty)
    stdin (bash only displays it on a real terminal), so this checks for
    the guidance line printed via plain `echo` right before the prompts --
    present only if that code path ran."""
    root = _fake_install(tmp_path)
    home = tmp_path / "home"
    answers = _base_answers("") + [
        "n", "n",
    ]
    proc = _run(root, answers, home)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout + proc.stderr
    assert "embedder fits comfortably" in out
