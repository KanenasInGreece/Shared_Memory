"""uninstall_framework.sh --level service — the printed "Reverse with:"
procedure (RULING 1, fix/uninstall-reverse-and-help).

MEASURED ON A REAL HOST: the OLD text --

    bash shared-memory/ops/install_service.sh && bash shared-memory/scripts/sync_skills.sh
    (each agent then needs its token re-issued: bootstrap_tokens.sh --remint <name>)

-- failed in three independent ways when followed literally: plain
sync_skills.sh SKIPS a directory it doesn't already find (it only UPDATES an
existing install, never recreates one); `--remint <name>` with no
--install-path mints a token nobody receives and exits 0; and the tool's own
fallback advice for THAT case pointed at `--add`, which refuses an
already-registered name (fixed separately in generate_tokens.py, see
tests/test_generate_tokens_mint_flow.py's RULING 1.3 tests).

This file pins the FIXED text: every step is a real, working invocation
(sync_skills.sh --install, bootstrap_tokens.sh --remint ... --install-path
..., a gateway restart) and the two specific broken forms above are gone.
test_sync_skills_install_flag.py separately proves --install itself does
what step 2's text claims.

⛔ SEPARATE FILE FROM test_uninstall_guards.py, DELIBERATELY. That file
enforces (via test_no_test_in_this_file_runs_a_destructive_level) that every
`--level` invocation IN IT passes --dry-run — the reverse text is only
printed at the very end of a REAL (non-dry-run) `--level service` run,
which never happens there. This file runs `--level service --yes` for real
against a from-scratch fake install (own tmp_path, own $HOME) where:
  * no systemd unit exists at the fake $HOME -> the script's own "no unit
    ... leaving systemd alone" branch fires, so `systemctl` is never
    invoked at all (verified below);
  * no AGENT_INSTALLS registry and no skill directory exist under the fake
    $HOME -> SKILL_DIRS is empty, so the "Removing agent skill directories"
    step removes nothing;
  * `--level service` never touches docker/containers/data dirs at all
    (that only happens at `data`/`all`, which this file never exercises).
So nothing here can reach real infrastructure regardless of whether the
guards work — verified per-test by asserting on the script's own output,
not merely assumed.
"""
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SRC = REPO_ROOT / "shared-memory" / "scripts" / "uninstall_framework.sh"


def _fake_service_level_install(tmp_path):
    """Minimal install shape for a `--level service` run: no unit, no
    AGENT_INSTALLS registry, no skill directories -- so the run is a
    no-op besides printing the inventory and the reverse text."""
    root = tmp_path / "repo"
    (root / "shared-memory" / "scripts").mkdir(parents=True)
    (root / "shared-memory" / "ops").mkdir(parents=True)
    shutil.copy(SRC, root / "shared-memory" / "scripts" / "uninstall_framework.sh")

    state = tmp_path / "home" / ".shared-memory"
    (state / "backups").mkdir(parents=True)

    (root / "shared-memory" / ".env").write_text("PLACEHOLDER=1\n")
    return root


def _run_real_service_level(root, home):
    """A REAL (non-dry-run) --level service run. Deliberately no --dry-run:
    the reverse text is only printed at the end of a real run. Safety is
    structural (see module docstring), not merely asserted -- every caller
    below checks the output for proof the no-op branches actually fired."""
    env = dict(os.environ)
    env["HOME"] = str(home)
    return subprocess.run(
        ["bash", str(root / "shared-memory" / "scripts" / "uninstall_framework.sh"),
         "--level", "service", "--yes"],
        capture_output=True, text=True, timeout=60, env=env,
    )


def test_this_run_touches_no_real_service_or_skill_directory(tmp_path):
    """Structural precondition for every other test below: prove the run
    actually took the inert branches before trusting its printed text."""
    root = _fake_service_level_install(tmp_path)
    res = _run_real_service_level(root, tmp_path / "home")
    out = res.stdout + res.stderr

    assert res.returncode == 0, out
    assert "no unit at" in out and "leaving systemd alone" in out, (
        f"expected the no-unit branch (systemctl never invoked):\n{out}"
    )
    # The per-directory line's fixed suffix -- never appears in the static
    # "Removing agent skill directories (...)" heading (which always prints
    # regardless of whether anything was found), only in the per-dir loop.
    assert "⚠ contains that agent's raw token" not in out, (
        f"expected zero skill directories found -- a real one would be "
        f"unsafe to touch from this test:\n{out}"
    )
    assert "(none)" in out.split("Removing agent skill directories", 1)[1], (
        f"expected the (none) branch for agent skill directories:\n{out}"
    )


def test_reverse_text_names_sync_skills_with_install_flag(tmp_path):
    root = _fake_service_level_install(tmp_path)
    out = _run_real_service_level(root, tmp_path / "home").stdout

    reverse = out.split("Reverse with:", 1)[1]
    assert "sync_skills.sh --install" in reverse, (
        f"reverse text no longer names --install -- plain sync_skills.sh "
        f"SKIPS a deleted directory:\n{reverse}"
    )
    # The OLD broken form: sync_skills.sh with no flag at all.
    assert "sync_skills.sh\n" not in reverse and "sync_skills.sh &&" not in reverse, (
        f"reverse text still offers bare sync_skills.sh somewhere:\n{reverse}"
    )


def test_reverse_text_names_remint_with_install_path(tmp_path):
    root = _fake_service_level_install(tmp_path)
    out = _run_real_service_level(root, tmp_path / "home").stdout

    reverse = out.split("Reverse with:", 1)[1]
    assert "bootstrap_tokens.sh --remint <name> --install-path" in reverse, (
        f"reverse text no longer pairs --remint with --install-path -- "
        f"--remint alone mints a token nobody receives and exits 0:\n{reverse}"
    )


def test_reverse_text_restarts_the_gateway_after_reminting(tmp_path):
    """Tokens are reminted into $ENV_FILE AFTER install_service.sh already
    started the gateway with the OLD digests -- a restart is required to
    load what bootstrap_tokens.sh just wrote (bootstrap_tokens.sh says so
    itself: "Restart the gateway to load the new AGENT_TOKENS.")."""
    root = _fake_service_level_install(tmp_path)
    out = _run_real_service_level(root, tmp_path / "home").stdout

    reverse = out.split("Reverse with:", 1)[1]
    assert "restart" in reverse.lower(), (
        f"reverse text never restarts the gateway after reminting tokens -- "
        f"the running gateway would keep answering with STALE digests:\n{reverse}"
    )


def test_reverse_text_no_longer_offers_the_measured_broken_forms(tmp_path):
    """Pin against regression to EXACTLY the three measured failures."""
    root = _fake_service_level_install(tmp_path)
    out = _run_real_service_level(root, tmp_path / "home").stdout
    reverse = out.split("Reverse with:", 1)[1]

    # (1) sync_skills.sh with no --install.
    assert "&& bash shared-memory/scripts/sync_skills.sh\n" not in reverse
    # (2) --remint with no --install-path anywhere near it.
    assert "--remint <name>)" not in reverse
    # (3) the old, wrong fallback command (--add refuses an already-
    # registered name) must not be suggested here either.
    assert "generate_tokens.py --add" not in reverse
