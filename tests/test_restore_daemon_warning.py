"""ops/restore.sh — Invariant I8: Daemon detection, warning, and non-refusal.

Invariant I8:
Restore warns with PIDs and the stop line when this install's daemon lives,
never refuses, and stays silent for an editor, `uv` or another install.
Failure to re-introduce: match by substring.

Tests verify:
1. When this install's live daemon process lives:
   - restore.sh warns with PID
   - restore.sh prints the exact stop line: systemctl --user stop hive-mind-gateway.service
   - restore.sh continues and succeeds (never refuses)
2. Stays silent for an editor (e.g. vim shared-memory/scripts/hive_mind_proxy.py)
3. Stays silent for uv (e.g. uv run python ...)
4. Stays silent for another install (python /other/install/shared-memory/scripts/hive_mind_proxy.py)
5. Tests fake the proc tree via $PROC_DIR the way tests fake docker.
"""
import gzip
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
RESTORE_SRC = REPO_ROOT / "shared-memory" / "ops" / "restore.sh"

FAKE_DOCKER_SCRIPT = r"""#!/usr/bin/env bash
cmd="$1"; shift
if [[ "$cmd" != "exec" ]]; then exit 0; fi
while [[ "$1" == -* ]]; do
  case "$1" in
    -i) shift ;;
    --env-file) shift 2 ;;
    *) shift ;;
  esac
done
container="$1"; shift
prog="$1"; shift
case "$prog" in
  psql) echo "0" ;;
  pg_restore) cat >/dev/null ;;
  cypher-shell)
    q="$*"
    case "$q" in
      *"MATCH (n) RETURN count(n)"*) echo "0" ;;
      *"MATCH ()-[r]->() RETURN count(r)"*) echo "0" ;;
      *) cat >/dev/null ;;
    esac
    ;;
esac
exit 0
"""


def _setup_restore_sandbox(tmp_path):
    repo = tmp_path / "repo"
    ops = repo / "shared-memory" / "ops"
    ops.mkdir(parents=True)
    scripts = repo / "shared-memory" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "hive_mind_proxy.py").write_text("# proxy\n")
    (scripts / "consolidation_loop.py").write_text("# nrem\n")
    (scripts / "rem_loop.py").write_text("# rem\n")

    shutil.copy(RESTORE_SRC, ops / "restore.sh")
    (repo / "shared-memory" / ".env").write_text("PG_PASSWORD=test\nNEO4J_PASSWORD=test\n")

    docker = tmp_path / "fake_docker"
    docker.write_text(FAKE_DOCKER_SCRIPT)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    backups = tmp_path / "backups"
    backups.mkdir()
    name = "sm-backup-test"
    pgdump = backups / f"{name}.pgdump"
    cypher = backups / f"{name}.cypher.gz"
    pgdump.write_bytes(b"PGDUMP")
    cypher.write_bytes(gzip.compress(b":begin\nCREATE (n:E);\n:commit;\n"))
    import hashlib
    (backups / f"{name}.manifest.json").write_text(json.dumps({
        "name": name,
        "pg_file": f"{name}.pgdump",
        "pg_sha256": hashlib.sha256(b"PGDUMP").hexdigest(),
        "neo4j_file": f"{name}.cypher.gz",
        "neo4j_sha256": hashlib.sha256(gzip.compress(b":begin\nCREATE (n:E);\n:commit;\n")).hexdigest(),
        "neo4j_nodes": "1",
        "neo4j_rels": "0",
    }))

    return repo, docker, backups


def test_i8_restore_warns_with_pid_and_stop_line_and_continues(tmp_path):
    """Restore detects live daemon via fake proc tree (PROC_DIR), prints warning with PID and stop line, and continues without refusing."""
    repo, docker, backups = _setup_restore_sandbox(tmp_path)

    fake_proc = tmp_path / "proc"
    fake_proc.mkdir()
    p4321 = fake_proc / "4321"
    p4321.mkdir()
    proxy_script = os.path.realpath(repo / "shared-memory" / "scripts" / "hive_mind_proxy.py")
    (p4321 / "cmdline").write_bytes(f"python3\x00{proxy_script}\x00".encode("utf-8"))
    os.symlink(os.path.realpath(repo), str(p4321 / "cwd"))

    env = dict(os.environ)
    env.update({
        "DOCKER": str(docker),
        "BACKUP_DIR": str(backups),
        "PROC_DIR": str(fake_proc),
        "HOME": str(tmp_path),
    })
    res = subprocess.run(
        ["bash", str(repo / "shared-memory" / "ops" / "restore.sh"), "sm-backup-test"],
        capture_output=True, text=True, timeout=30, env=env, cwd=str(repo),
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Live daemon processes detected for this install:" in res.stdout
    assert "PID 4321 hive_mind_proxy.py" in res.stdout
    assert "systemctl --user stop hive-mind-gateway.service" in res.stdout
    assert "Restore complete" in res.stdout


def test_i8_restore_stays_silent_when_no_daemons_run(tmp_path):
    """When no daemon processes are found in /proc, restore stays silent about live daemons."""
    repo, docker, backups = _setup_restore_sandbox(tmp_path)

    fake_proc = tmp_path / "proc"
    fake_proc.mkdir()

    env = dict(os.environ)
    env.update({
        "DOCKER": str(docker),
        "BACKUP_DIR": str(backups),
        "PROC_DIR": str(fake_proc),
        "HOME": str(tmp_path),
    })
    res = subprocess.run(
        ["bash", str(repo / "shared-memory" / "ops" / "restore.sh"), "sm-backup-test"],
        capture_output=True, text=True, timeout=30, env=env, cwd=str(repo),
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Live daemon processes detected" not in res.stdout
    assert "systemctl --user stop" not in res.stdout


def test_i8_builtin_scanner_filters_editor_uv_and_other_installs(tmp_path):
    """Verify built-in /proc scanner filters out:
    1. Editors (e.g. vim shared-memory/scripts/hive_mind_proxy.py)
    2. uv wrapper processes (argv0 == 'uv')
    3. Other installs (python /other/install/shared-memory/scripts/hive_mind_proxy.py)
    And matches ONLY this install's python daemon processes.
    """
    repo, docker, backups = _setup_restore_sandbox(tmp_path)

    fake_proc = tmp_path / "proc"
    fake_proc.mkdir()

    # PID 101: editor (vim)
    p101 = fake_proc / "101"
    p101.mkdir()
    (p101 / "cmdline").write_bytes(b"vim\x00shared-memory/scripts/hive_mind_proxy.py\x00")
    os.symlink(str(repo), str(p101 / "cwd"))

    # PID 102: uv wrapper process
    p102 = fake_proc / "102"
    p102.mkdir()
    (p102 / "cmdline").write_bytes(b"uv\x00run\x00python\x00shared-memory/scripts/hive_mind_proxy.py\x00")
    os.symlink(str(repo), str(p102 / "cwd"))

    # PID 103: another install's daemon
    other_repo = tmp_path / "other_repo"
    other_repo.mkdir()
    p103 = fake_proc / "103"
    p103.mkdir()
    (p103 / "cmdline").write_bytes(b"python3\x00shared-memory/scripts/hive_mind_proxy.py\x00")
    os.symlink(str(other_repo), str(p103 / "cwd"))

    # PID 104: THIS install's genuine daemon
    p104 = fake_proc / "104"
    p104.mkdir()
    (p104 / "cmdline").write_bytes(b"python3\x00shared-memory/scripts/consolidation_loop.py\x00")
    os.symlink(str(repo), str(p104 / "cwd"))

    env = dict(os.environ)
    env.update({
        "DOCKER": str(docker),
        "BACKUP_DIR": str(backups),
        "PROC_DIR": str(fake_proc),
        "HOME": str(tmp_path),
    })

    res = subprocess.run(
        ["bash", str(repo / "shared-memory" / "ops" / "restore.sh"), "sm-backup-test"],
        capture_output=True, text=True, timeout=30, env=env, cwd=str(repo),
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Live daemon processes detected for this install:" in res.stdout
    # PID 104 must be detected
    assert "PID 104 consolidation_loop.py" in res.stdout
    # PIDs 101, 102, 103 must NOT be detected
    assert "101" not in res.stdout, "editor must not be detected as daemon"
    assert "102" not in res.stdout, "uv process must not be detected as daemon"
    assert "103" not in res.stdout, "other install must not be detected as daemon"
    assert "systemctl --user stop hive-mind-gateway.service" in res.stdout
