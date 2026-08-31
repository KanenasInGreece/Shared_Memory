"""install_framework.sh — the reasoning-LLM-backend prompt's interactive
default and skip wording (W0 item ③).

Before this, the prompt showed `[y/N]` (default No) unconditionally, and
its skip branch said "A single default backend at LLM_DEFAULT_TARGET
(http://localhost:5000) is used until you configure one" -- worded as if
the implicit fallback were a stable, permanent feature. Both change:

  - the prompt is now `[Y/n]` (default Yes) for an interactive run, but a
    genuinely non-interactive run (piped stdin with nothing left to answer
    with, exactly at this prompt) still takes the N branch and the
    installer exits 0 -- a DELIBERATE, ruled behaviour change from before,
    where the unguarded `read` died non-zero under `set -e` at this exact
    point (after .env was already written).
  - the skip text now says the fallback is being retired and names the
    explicit config command, replacing "is used until" wording that read
    as a permanent feature.

Runs the REAL shipped script end-to-end via subprocess (same harness as
tests/test_install_framework_encoder_device_split.py), against a throwaway
install tree -- never the real repo or a real $HOME.
"""
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
INSTALL_SH = REPO_ROOT / "shared-memory" / "scripts" / "install_framework.sh"
ENV_EXAMPLE = REPO_ROOT / "shared-memory" / ".env.example"

# The exact, static prompt sentence this test anchors on -- a bare
# `[Y/n]` regex would also match another prompt in this script (the
# systemd-service question), so the full sentence is required to pin THIS
# one uniquely.
PROMPT_SENTENCE = (
    "Configure reasoning-LLM backend(s) now (local, remote, or a paid cloud API)? [Y/n] "
)


def _fake_install(tmp_path):
    root = tmp_path / "repo"
    (root / "shared-memory" / "scripts").mkdir(parents=True)
    (root / "shared-memory" / "ops").mkdir(parents=True)
    shutil.copy(INSTALL_SH, root / "shared-memory" / "scripts" / "install_framework.sh")
    shutil.copy(ENV_EXAMPLE, root / "shared-memory" / ".env.example")
    return root


def _run(root, answers: list[str], home, close_stdin_after=None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = str(home)
    stdin_text = "".join(f"{a}\n" for a in answers)
    return subprocess.run(
        ["bash", str(root / "shared-memory" / "scripts" / "install_framework.sh")],
        input=stdin_text, capture_output=True, text=True, timeout=60, env=env,
        cwd=str(root),
    )


def _base_answers(models_dir: str) -> list[str]:
    """The eight answers every install needs before the systemd/LLM prompts:
    two dirs, models dir, two encoder-device answers, one render-gid
    answer, two passwords -- same fixed sequence as
    tests/test_install_framework_encoder_device_split.py's _base_answers."""
    return [
        "", "",                       # NEO4J_HOST_DIR, PG_DATA_DIR -- defaults
        models_dir,                   # LLM_MODELS_DIR
        "", "", "",                   # embedder, reranker, render_gid -- defaults
        "supersecret_neo4j_pw_123",   # NEO4J_PASSWORD
        "supersecret_pg_pw_123",      # PG_PASSWORD
    ]


def test_prompt_sentence_present_and_shows_yn_default(tmp_path):
    text = INSTALL_SH.read_text()
    assert PROMPT_SENTENCE in text
    assert text.count(PROMPT_SENTENCE) == 1


def test_old_wording_gone_new_wording_present(tmp_path):
    text = INSTALL_SH.read_text()
    assert "is used until" not in text
    assert "falls back to" in text
    assert "being retired" in text
    assert "bash shared-memory/ops/install_llm_backends.sh" in text


def test_exhausted_pipe_at_this_prompt_takes_n_branch_and_exits_zero(tmp_path):
    """The measured behaviour change: a fully-scripted install that answers
    everything up to and including the systemd-service prompt, then runs
    OUT of input exactly at the LLM-backend prompt, must exit 0 and print
    the Skipped text -- not die non-zero under set -e."""
    root = _fake_install(tmp_path)
    home = tmp_path / "home"
    answers = _base_answers(str(tmp_path / "models")) + [
        "n",   # skip systemd service install
        # -- nothing left for the LLM prompt: stdin runs dry here --
    ]
    proc = _run(root, answers, home)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Skipped. Until you configure backends" in proc.stdout
    assert (root / "shared-memory" / ".env").exists()


def test_explicit_n_still_skips(tmp_path):
    root = _fake_install(tmp_path)
    home = tmp_path / "home"
    answers = _base_answers(str(tmp_path / "models")) + ["n", "n"]
    proc = _run(root, answers, home)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Skipped. Until you configure backends" in proc.stdout


def test_agents_md_piped_n_n_install_still_passes(tmp_path):
    """The exact AGENTS.md:149-161 piped install -- 8 answers then "n\\nn"
    for the two trailing prompts -- must still succeed end to end."""
    root = _fake_install(tmp_path)
    home = tmp_path / "home"
    neo4j_dir = str(tmp_path / "databases" / "neo4j")
    pg_dir = str(tmp_path / "databases" / "postgres")
    stdin_text = (
        f"{neo4j_dir}\n{pg_dir}\n{tmp_path / 'models'}\n\n\n\n"
        "supersecret_neo4j_pw_123\nsupersecret_pg_pw_123\nn\nn\n"
    )
    env = dict(os.environ)
    env["HOME"] = str(home)
    proc = subprocess.run(
        ["bash", str(root / "shared-memory" / "scripts" / "install_framework.sh")],
        input=stdin_text, capture_output=True, text=True, timeout=60, env=env,
        cwd=str(root),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (root / "shared-memory" / ".env").exists()
