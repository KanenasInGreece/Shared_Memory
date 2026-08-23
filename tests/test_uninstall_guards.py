"""uninstall_framework.sh — the guards, which are the whole point of the script.

An uninstall is the one operation with no undo, so what it REFUSES to do matters
more than what it does. These drive the real shipped script; the destructive path
itself is exercised on a sacrificial host, never here.

Three properties, each traced to a measured hazard:

  * --level is required. There is no safe default for an irreversible operation.
  * --dry-run removes NOTHING while still printing the full inventory. A blast
    radius you cannot read before committing to it is not a blast radius.
  * ~/.shared-memory survives every level. It holds the backup sets, the
    credential AUDIT TRAIL, the capacity measurement history and every postflight
    baseline. An audit trail an uninstall can erase is not an audit trail, and
    the measurements describe the host rather than the installation. (Measured:
    logs are NOT part of any backup set — a set is exactly .pgdump + .cypher.gz +
    .manifest.json — so that directory is the only copy in existence.)
"""
import os
import shutil
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SRC = REPO_ROOT / "shared-memory" / "scripts" / "uninstall_framework.sh"


def _fake_install(tmp_path):
    """A tree shaped like a real install, with nothing real in it."""
    root = tmp_path / "repo"
    (root / "shared-memory" / "scripts").mkdir(parents=True)
    (root / "shared-memory" / "ops").mkdir(parents=True)
    shutil.copy(SRC, root / "shared-memory" / "scripts" / "uninstall_framework.sh")

    state = tmp_path / "home" / ".shared-memory"
    (state / "backups").mkdir(parents=True)
    (state / "logs").mkdir(parents=True)
    (state / "logs" / "credential-audit.jsonl").write_text('{"event":"audit"}\n')
    (state / "capacity").mkdir(parents=True)
    (state / "capacity" / "derivations.jsonl").write_text("{}\n")
    (state / "backups" / "sm-backup-x.manifest.json").write_text("{}")

    data = tmp_path / "data"
    (data / "neo4j").mkdir(parents=True)
    (data / "pg").mkdir(parents=True)
    (data / "neo4j" / "keepme").write_text("graph")

    models = tmp_path / "models"
    models.mkdir()
    (models / "a.gguf").write_text("weights")

    (root / "shared-memory" / ".env").write_text(
        f"NEO4J_HOST_DIR={data / 'neo4j'}\n"
        f"PG_DATA_DIR={data / 'pg'}\n"
        f"LLM_MODELS_DIR={models}\n"
        f"BACKUP_DIR={state / 'backups'}\n"
    )
    return root, state, data, models


def _run(root, args, home):
    env = dict(os.environ)
    env["HOME"] = str(home)
    # No docker, no systemctl reachable: the script must degrade, not crash.
    return subprocess.run(
        ["bash", str(root / "shared-memory" / "scripts" / "uninstall_framework.sh"), *args],
        capture_output=True, text=True, timeout=60, env=env,
    )


def test_level_is_required(tmp_path):
    """No default. An operation with no undo is one you state explicitly."""
    root, state, _d, _m = _fake_install(tmp_path)
    res = _run(root, [], tmp_path / "home")

    assert res.returncode != 0
    assert "--level is required" in (res.stdout + res.stderr)


def test_an_unknown_level_is_refused(tmp_path):
    root, _s, _d, _m = _fake_install(tmp_path)
    res = _run(root, ["--level", "everything"], tmp_path / "home")

    assert res.returncode != 0
    assert "unknown level" in (res.stdout + res.stderr)


def test_dry_run_removes_nothing_at_the_most_destructive_level(tmp_path):
    """THE guard. --dry-run must be inert even at `all`."""
    root, state, data, models = _fake_install(tmp_path)
    res = _run(root, ["--level", "all", "--dry-run"], tmp_path / "home")

    assert res.returncode == 0
    assert (data / "neo4j" / "keepme").exists()
    assert (models / "a.gguf").exists()
    assert (root / "shared-memory" / ".env").exists()
    assert (state / "logs" / "credential-audit.jsonl").exists()


def test_dry_run_still_prints_the_full_inventory(tmp_path):
    """Inert is not the same as silent: the operator must be able to read the
    blast radius before agreeing to it."""
    root, _s, data, models = _fake_install(tmp_path)
    out = _run(root, ["--level", "all", "--dry-run"], tmp_path / "home").stdout

    assert "WILL BE REMOVED:" in out and "WILL BE KEPT:" in out
    assert str(data / "neo4j") in out
    assert str(models) in out


def test_the_host_state_directory_is_kept_at_every_level(tmp_path):
    """Backups, the audit trail, capacity history and baselines all live here,
    and logs are in no backup set — this directory is the only copy."""
    for level in ("service", "data", "all"):
        root, state, _d, _m = _fake_install(tmp_path / level)
        out = _run(root, ["--level", level, "--dry-run"], tmp_path / level / "home").stdout
        kept = out.split("WILL BE KEPT:", 1)
        assert len(kept) == 2, f"{level}: no KEPT section"
        assert str(state) in kept[1], f"{level}: host state not listed as kept"


def test_the_repo_checkout_is_never_removed_by_the_script(tmp_path):
    """It is the ground the script stands on; the final rm -rf is the operator's."""
    root, _s, _d, _m = _fake_install(tmp_path)
    out = _run(root, ["--level", "all", "--dry-run"], tmp_path / "home").stdout

    assert "the final rm -rf is yours" in out
    assert (root / "shared-memory" / "scripts" / "uninstall_framework.sh").exists()


def test_a_destructive_level_refuses_without_a_backup(tmp_path):
    """The gate that makes an irreversible operation safe to hand to an agent."""
    root, state, _d, _m = _fake_install(tmp_path)
    (state / "backups" / "sm-backup-x.manifest.json").unlink()

    res = _run(root, ["--level", "data", "--yes"], tmp_path / "home")

    assert res.returncode != 0
    assert "no backup set found" in (res.stdout + res.stderr)


def test_service_level_does_not_require_a_backup(tmp_path):
    """It removes nothing irreversible, so demanding a backup would train the
    operator to pass --no-backup out of habit."""
    root, state, _d, _m = _fake_install(tmp_path)
    (state / "backups" / "sm-backup-x.manifest.json").unlink()

    res = _run(root, ["--level", "service", "--dry-run"], tmp_path / "home")

    assert res.returncode == 0
    assert "no backup set found" not in (res.stdout + res.stderr)
