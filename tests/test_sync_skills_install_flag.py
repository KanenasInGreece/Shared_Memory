"""sync_skills.sh --install — proves the CENTRAL factual claim behind this
branch's fix to uninstall_framework.sh's `--level service` reverse text.

RULING 1 (fix/uninstall-reverse-and-help): the reverse text used to say
"bash shared-memory/scripts/sync_skills.sh" with NO --install. Plain
sync_skills.sh only ever UPDATES an existing install (`SKIP (not installed):
<path>` for anything missing) — it never recreates a directory an uninstall
--level service just deleted. --install is the one thing that does, but only
for a directory the AGENT_INSTALLS registry actually names (never a guess),
and it does more than `mkdir -p`: the created directory is also POPULATED
from the tracked skill copy in the same run, via the existing per-agent
manifest loop falling through instead of `continue`-ing past the CREATED
branch. This file proves both halves against the REAL script, hermetically:
no real skill directory, no real $HOME, no real AGENT_INSTALLS is ever read
or written -- SHARED_MEMORY_ENV_FILE points at a throwaway registry and
SHARED_MEMORY_SYNC_AGENTS points at a throwaway target directory, the exact
seams test_skill_delivery.py already established for this purpose.
SHARED_MEMORY_SYNC_SKIP_TRACKED=1 keeps phase 1 (which writes into the
REPO's own tracked skill copy) from running at all.
"""
import os
import subprocess

_REPO = os.path.join(os.path.dirname(__file__), "..")
_SYNC = os.path.join(_REPO, "shared-memory", "scripts", "sync_skills.sh")
_SKILL_COPY = os.path.join(_REPO, "shared-memory-skill", "shared-memory")


def _manifest_entries():
    with open(os.path.join(_SKILL_COPY, "MANIFEST.txt"), encoding="utf-8") as fh:
        return [ln.strip() for ln in fh
                if ln.strip() and not ln.strip().startswith("#")]


def _run_sync(agent_dir, registry_env_path=None, extra_args=()):
    env = dict(os.environ)
    env["SHARED_MEMORY_SYNC_AGENTS"] = str(agent_dir)
    env["SHARED_MEMORY_SYNC_SKIP_TRACKED"] = "1"
    if registry_env_path is not None:
        env["SHARED_MEMORY_ENV_FILE"] = str(registry_env_path)
    return subprocess.run(
        ["bash", _SYNC, *extra_args],
        capture_output=True, text=True, env=env, cwd=_REPO, timeout=180,
    )


def test_plain_sync_skips_a_missing_directory_even_when_registered(tmp_path):
    """The half of RULING 1 that made the OLD reverse text wrong: without
    --install, a registered-but-missing directory is left untouched."""
    registry_env = tmp_path / "gateway.env"
    target = tmp_path / "agent" / "skills" / "shared-memory"
    registry_env.write_text(f"AGENT_INSTALLS=codex:{target}/.env\n")

    result = _run_sync(target, registry_env_path=registry_env)

    assert not target.exists(), (
        "plain sync_skills.sh (no --install) created a directory it was "
        "never supposed to touch"
    )
    assert "SKIP" in result.stdout, result.stdout + result.stderr


def test_install_flag_creates_and_populates_a_registered_missing_directory(tmp_path):
    """THE claim the fixed reverse text now makes: --install both creates
    AND populates (every MANIFEST.txt file lands, not just an empty dir) a
    directory the registry names but that does not yet exist on disk --
    exactly the state uninstall_framework.sh --level service leaves behind."""
    registry_env = tmp_path / "gateway.env"
    target = tmp_path / "agent" / "skills" / "shared-memory"
    registry_env.write_text(f"AGENT_INSTALLS=codex:{target}/.env\n")

    result = _run_sync(target, registry_env_path=registry_env, extra_args=["--install"])

    assert target.is_dir(), (
        f"--install did not create the registered directory:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "CREATED" in result.stdout, result.stdout + result.stderr

    entries = _manifest_entries()
    assert entries, "MANIFEST.txt parsed to nothing -- test would pass vacuously"
    missing = [rel for rel in entries
               if rel not in (".env.example",) and not (target / rel).exists()]
    assert not missing, (
        f"--install created the directory but did not POPULATE it -- missing "
        f"manifest file(s) {missing}:\n{result.stdout}\n{result.stderr}"
    )
    # No .env is created (no token exists yet) -- that is bootstrap_tokens.sh's
    # job, and this is exactly why the fixed reverse text puts --install
    # BEFORE bootstrap_tokens.sh --remint ... --install-path, not after.
    assert not (target / ".env").exists(), (
        "sync_skills.sh --install wrote a .env -- it must leave token "
        "provisioning entirely to bootstrap_tokens.sh"
    )


def test_install_flag_does_not_conjure_an_unregistered_directory(tmp_path):
    """--install honours the registry -- it does not create a directory that
    happens to be on the default candidate list but was never registered."""
    registry_env = tmp_path / "gateway.env"
    registry_env.write_text("AGENT_INSTALLS=codex:/some/other/path/.env\n")
    target = tmp_path / "agent" / "skills" / "shared-memory"  # NOT registered

    result = _run_sync(target, registry_env_path=registry_env, extra_args=["--install"])

    assert not target.exists(), (
        f"--install created a directory the registry never named:\n"
        f"{result.stdout}\n{result.stderr}"
    )
