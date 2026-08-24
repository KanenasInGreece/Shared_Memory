"""uninstall_framework.sh — `.env.mintlock` removal (corpus fact:1515, F6).

MEASURED: `bootstrap_tokens.sh` takes a read-modify-write lock on $ENV_FILE
while minting a token, via a sibling lockfile named `${ENV_FILE}.mintlock`
(see bootstrap_tokens.sh's own `_LOCKFILE=` comment). `--level data` removed
`$ENV_FILE` itself but never that lockfile -- a Medium-severity leftover that
survives every uninstall at `data` or `all`, forever, because nothing else in
this repository ever cleans it up.

THE FIX: remove `${ENV_FILE}.mintlock`, independently of whether $ENV_FILE
itself still exists (a re-run after a partial uninstall may find .env
already gone but the lockfile still there), at the same point $ENV_FILE is
removed -- i.e. `data` and `all`, never `service` (which never touches
$ENV_FILE at all and exits before reaching that code).

HOW THIS IS SAFE TO RUN NON-DRY-RUN, UNLIKE test_uninstall_guards.py. That
file's own comment explains the hazard it avoids: `systemctl --user` reaches
the real user's session bus regardless of a sandboxed $HOME, so a real
`--level service` run (or `data`/`all`, which also touch the service section
first) can stop and disable the live gateway. The guard in this script itself
is what makes it safe here: the service section only calls systemctl at all
when `$UNIT_PATH` (`$HOME/.config/systemd/user/<unit>`) exists -- and this
file's fixture never creates one. So every scenario below, `service` included,
never reaches a live systemctl call; `docker` is PATH-stubbed for the same
reason `test_uninstall_compose_down.py` stubs it, so `data`/`all` never touch
a real docker either.
"""
import os
import shutil
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SRC = REPO_ROOT / "shared-memory" / "scripts" / "uninstall_framework.sh"


def _docker_stub_body(bash_bin: str) -> str:
    # A clean compose down (exit 0) with no leftover containers -- this file
    # is about mintlock, not about compose_down_and_verify() itself (that is
    # test_uninstall_compose_down.py's job), so the stub always succeeds.
    return (
        f"#!{bash_bin}\n"
        'case "$1" in\n'
        '    compose) exit 0 ;;\n'
        '    ps)      exit 0 ;;\n'  # no output => no leftovers found
        "    *)       exit 0 ;;\n"
        "esac\n"
    )


def _fake_install(tmp_path, *, with_mintlock: bool, with_env: bool = True):
    root = tmp_path / "repo"
    (root / "shared-memory" / "scripts").mkdir(parents=True)
    (root / "shared-memory" / "ops").mkdir(parents=True)
    shutil.copy(SRC, root / "shared-memory" / "scripts" / "uninstall_framework.sh")

    state = tmp_path / "home" / ".shared-memory"
    (state / "backups").mkdir(parents=True)

    data = tmp_path / "data"
    (data / "neo4j").mkdir(parents=True)
    (data / "pg").mkdir(parents=True)

    env_path = root / "shared-memory" / ".env"
    if with_env:
        env_path.write_text(
            f"NEO4J_HOST_DIR={data / 'neo4j'}\n"
            f"PG_DATA_DIR={data / 'pg'}\n"
        )
    mintlock_path = Path(str(env_path) + ".mintlock")
    if with_mintlock:
        mintlock_path.write_text("")  # flock target -- content is irrelevant

    # A real compose file must exist for the "docker or compose file absent"
    # branch to be skipped and compose_down_and_verify() to actually run. It
    # must also declare at least one container_name: -- since Ops-14 (empty
    # parsed list is refused as an unverifiable teardown, never read as
    # "nothing to check"), an empty services: block here would make the down
    # verification fail and this file's tests never reach the mintlock code
    # at all.
    (root / "shared-memory" / "ops" / "postgres_neo4j_limits.yaml").write_text(
        "name: shared-memory\n"
        "services:\n"
        "  neo4j:\n"
        "    container_name: neo4j-memory\n"
        "  postgres:\n"
        "    container_name: postgres-vector\n"
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    bash_bin = shutil.which("bash")
    assert bash_bin, "bash not found on the harness's own PATH"
    docker_stub = bin_dir / "docker"
    docker_stub.write_text(_docker_stub_body(bash_bin))
    docker_stub.chmod(docker_stub.stat().st_mode | stat.S_IEXEC)

    return root, env_path, mintlock_path, bin_dir


def _run(root, args, home, bin_dir):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '/usr/bin:/bin')}"
    return subprocess.run(
        ["bash", str(root / "shared-memory" / "scripts" / "uninstall_framework.sh"), *args],
        capture_output=True, text=True, timeout=60, env=env,
    )


def test_level_data_removes_the_mintlock_alongside_the_env_file(tmp_path):
    root, env_path, mintlock_path, bin_dir = _fake_install(tmp_path, with_mintlock=True)
    home = tmp_path / "home"

    res = _run(root, ["--level", "data", "--yes", "--no-backup"], home, bin_dir)

    assert res.returncode == 0, res.stdout + res.stderr
    assert not env_path.exists(), "env file should have been removed"
    assert not mintlock_path.exists(), "mintlock should have been removed alongside it"
    assert "removed" in (res.stdout + res.stderr) and "mintlock" in (res.stdout + res.stderr)


def test_level_all_also_removes_the_mintlock(tmp_path):
    root, env_path, mintlock_path, bin_dir = _fake_install(tmp_path, with_mintlock=True)
    home = tmp_path / "home"

    res = _run(root, ["--level", "all", "--yes", "--no-backup"], home, bin_dir)

    assert res.returncode == 0, res.stdout + res.stderr
    assert not mintlock_path.exists()


def test_level_service_never_touches_the_env_file_or_the_mintlock(tmp_path):
    """service is reversible and never removes credentials -- confirmed here
    with a REAL (non-dry-run) invocation, safe because the fixture creates no
    systemd unit file, so the script's own guard skips systemctl entirely."""
    root, env_path, mintlock_path, bin_dir = _fake_install(tmp_path, with_mintlock=True)
    home = tmp_path / "home"

    res = _run(root, ["--level", "service", "--yes"], home, bin_dir)

    assert res.returncode == 0, res.stdout + res.stderr
    assert env_path.exists(), "service level must never remove the env file"
    assert mintlock_path.exists(), "service level must never remove the mintlock"


def test_partial_uninstall_rerun_env_already_gone_mintlock_still_cleared(tmp_path):
    """F6's actual re-run scenario: a PRIOR --level data run already removed
    $ENV_FILE (or it never existed) but the mintlock survived. A second run
    must still clear the mintlock -- the removal is independent of whether
    $ENV_FILE itself is present."""
    root, env_path, mintlock_path, bin_dir = _fake_install(
        tmp_path, with_mintlock=True, with_env=False,
    )
    home = tmp_path / "home"
    assert not env_path.exists()
    assert mintlock_path.exists()

    res = _run(root, ["--level", "data", "--yes", "--no-backup"], home, bin_dir)

    assert res.returncode == 0, res.stdout + res.stderr
    assert not mintlock_path.exists(), "mintlock must be cleared even when .env was already gone"


def test_no_mintlock_present_is_not_an_error(tmp_path):
    """The common case: bootstrap_tokens.sh was never run concurrently, so
    there is no mintlock to remove. Must not fail or print a false claim."""
    root, env_path, mintlock_path, bin_dir = _fake_install(tmp_path, with_mintlock=False)
    home = tmp_path / "home"

    res = _run(root, ["--level", "data", "--yes", "--no-backup"], home, bin_dir)
    out = res.stdout + res.stderr

    assert res.returncode == 0, out
    assert not mintlock_path.exists()
    # Not a bare "mintlock" substring check -- tmp_path itself legitimately
    # contains "mintlock" in its own directory name (derived from this test's
    # name), so that would be a false positive. Pin the actual UI lines.
    assert "mint lock" not in out, f"nothing to remove listed anyway:\n{out}"
    # ".mintlock" (with the leading dot, the real file suffix) rather than a
    # bare "mintlock" substring -- tmp_path's own directory name legitimately
    # contains "mintlock" (derived from this test's name), which would be a
    # false positive against a bare substring check.
    assert not any(".mintlock" in line and "✓ removed" in line for line in out.splitlines()), (
        f"a removal line for the mintlock was printed despite none existing:\n{out}"
    )
