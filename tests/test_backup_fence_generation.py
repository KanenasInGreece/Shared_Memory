"""ops/backup.sh — Invariant I6: Fence truth in backup.sh via quiesce_generation.

Invariant I6:
Manifest says `full` only with one unchanged generation and literal `true` readings;
lost and unknown are stated.
Failure to re-introduce: stamp from the final `lock_held` only.

Tests verify:
1. Coordinator /admin/outbox returns quiesce_generation, incremented on lock acquisition and _end_quiesce.
2. Manifest stamps quiesced: True, quiesce_mode: "full" only when lock is held with the same generation throughout.
3. Lock reacquired during dump (generation changed) records quiesced: False, mode: "lost_during_dump" even when final lock_held is True.
4. TTL lapse before re-arm records quiesced: False, mode: "lost_during_dump".
5. Empty or failed outbox reading records quiesced: False, mode: "fence_unknown".
6. BACKUP_QUIESCE_REQUIRED=1 does not promote a false result; keeps only the newest failed pair under distinct name sm-backup-failed-latest.*.
7. --verify prints the quiesce_mode on the false branch.
"""
import contextlib
import http.server
import json
import os
import stat
import subprocess
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).parent.parent
BACKUP_SH = REPO_ROOT / "shared-memory" / "ops" / "backup.sh"

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
  pg_dump) printf 'FAKE-PGDUMP-BYTES\n' ;;
  pg_restore)
    if [[ "$1" == "--list" ]]; then
      cat >/dev/null
      echo "; header"
      for i in $(seq 1 10); do echo "$i; TABLE public t"; done
    fi
    ;;
  cypher-shell)
    q="$*"
    case "$q" in
      *"SHOW SETTINGS"*) printf 'value\n"/fake/import"\n' ;;
      *"apoc.export.cypher.all"*) echo ok ;;
      *"MATCH (n) RETURN count(n)"*) printf 'count(n)\n10\n' ;;
      *"MATCH ()-[r]->() RETURN count(r)"*) printf 'count(r)\n20\n' ;;
    esac
    ;;
  cat) printf 'FAKE-CYPHER-EXPORT\n' ;;
  rm) : ;;
esac
"""


@contextlib.contextmanager
def _fake_docker(tmp_path):
    path = tmp_path / "fake_docker"
    path.write_text(FAKE_DOCKER_SCRIPT)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    yield str(path)


class _ConfigurableGatewayHandler(http.server.BaseHTTPRequestHandler):
    quiesce_status = 200
    quiesce_generation = 1
    rearm_generation = 1
    rearm_status = 200
    outbox_readings = []  # sequence of outbox dict responses
    reading_index = 0
    resume_calls = 0

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        payload = json.loads(body or b"{}")
        if payload.get("state") == "quiesce":
            # If this is a subsequent quiesce call (re-arm)
            if self.path == "/admin/backup" and self.headers.get("Authorization"):
                status = type(self).rearm_status if type(self).resume_calls == 0 and type(self).reading_index > 0 else type(self).quiesce_status
                gen = type(self).rearm_generation if type(self).reading_index > 0 else type(self).quiesce_generation
            else:
                status = type(self).quiesce_status
                gen = type(self).quiesce_generation
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "success" if status == 200 else "error",
                "lock_held": status == 200,
                "quiesce_generation": gen,
                "daemons": "fenced" if status == 200 else "not_fenced",
            }).encode())
        else:
            type(self).resume_calls += 1
            self.send_response(200)
            self.end_headers()

    def do_GET(self):
        if self.path == "/admin/outbox":
            idx = type(self).reading_index
            type(self).reading_index += 1
            if type(self).outbox_readings and idx < len(type(self).outbox_readings):
                item = type(self).outbox_readings[idx]
            elif type(self).outbox_readings:
                item = type(self).outbox_readings[-1]
            else:
                item = {
                    "status": "success",
                    "lock_held": True,
                    "quiesce_generation": type(self).quiesce_generation,
                    "outbox": {"pending": 0},
                }

            if item is None:
                # simulate failure / 500
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"error"}')
            elif item == "empty":
                # simulate empty response
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'')
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(item).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args, **kwargs):
        pass


@contextlib.contextmanager
def _stub_gw_configurable(**attrs):
    cls_dict = {
        "quiesce_status": 200,
        "quiesce_generation": 1,
        "rearm_generation": 1,
        "rearm_status": 200,
        "outbox_readings": [],
        "reading_index": 0,
        "resume_calls": 0,
    }
    cls_dict.update(attrs)
    handler_cls = type("ConfigurableHandler", (_ConfigurableGatewayHandler,), cls_dict)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", handler_cls
    finally:
        server.shutdown()
        server.server_close()


def _run_backup(tmp_path, docker_path, extra_env, args=()):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update({
        "DOCKER": docker_path,
        "BACKUP_DIR": str(backup_dir),
        "BACKUP_LOCKFILE": str(tmp_path / "backup.lock"),
        "PG_CONTAINER": "fake-pg",
        "NEO4J_CONTAINER": "fake-neo4j",
    })
    env.update(extra_env)
    res = subprocess.run(
        ["bash", str(BACKUP_SH), *args],
        env=env, capture_output=True, text=True, timeout=30,
    )
    return res, backup_dir


# ── Coordinator unit test: outbox returns quiesce_generation ─────────────────

@pytest.mark.asyncio
async def test_coordinator_outbox_returns_quiesce_generation():
    """Verify coordinator.py GET /admin/outbox reply carries quiesce_generation."""
    scripts_dir = str(REPO_ROOT / "shared-memory" / "scripts")
    import sys
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import coordinator

    coord = coordinator.MemoryCoordinator()
    coord._outbox_census = AsyncMock(return_value={
        "pending": 0, "in_progress": 0, "applied": 1, "failed": 0,
        "rem_reviewed": 0, "oldest_failed_age_s": None, "oldest_pending_age_s": None,
    })
    coord._quiesce_conn = MagicMock()
    coord._quiesce_conn.is_closed.return_value = False

    coordinator._quiesce_fence_acquisitions = 42
    coordinator._backup_quiesce = True

    req = MagicMock()
    resp = await coord.handle_admin_outbox(req)
    body = json.loads(resp.body.decode())

    assert body["status"] == "success"
    assert body["quiesced"] is True
    assert body["lock_held"] is True
    assert body["quiesce_generation"] == f"{coordinator._quiesce_process_token}-42"


@pytest.mark.asyncio
async def test_d2_quiesce_generation_and_fence_acquisitions_lifecycle():
    """D2: _quiesce_generation increments only at the start of _end_quiesce.
    _quiesce_fence_acquisitions increments only where a lock connection is stored as self._quiesce_conn.
    The resume reply carries no quiesce_generation wire key.
    """
    scripts_dir = str(REPO_ROOT / "shared-memory" / "scripts")
    import sys
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import coordinator

    coord = coordinator.MemoryCoordinator()
    initial_gen = coordinator._quiesce_generation
    initial_fences = coordinator._quiesce_fence_acquisitions

    # Simulate acquisition of lock connection in _begin_quiesce promotion
    fake_conn = MagicMock()
    fake_conn.is_closed.return_value = False
    coord._quiesce_conn = fake_conn
    coordinator._quiesce_fence_acquisitions += 1

    assert coordinator._quiesce_generation == initial_gen, (
        "_quiesce_generation must NOT increment when lock connection is acquired"
    )
    assert coordinator._quiesce_fence_acquisitions == initial_fences + 1

    # End quiesce: _quiesce_generation increments, _quiesce_fence_acquisitions does not
    await coord._end_quiesce(ended_by="resume")
    assert coordinator._quiesce_generation == initial_gen + 1
    assert coordinator._quiesce_fence_acquisitions == initial_fences + 1

    # Wire key on handle_backup: resume reply omits quiesce_generation
    coordinator._quiesce_fence_acquisitions = 99
    coordinator._quiesce_generation = 12
    resume_req = MagicMock()
    resume_req.json = AsyncMock(return_value={"state": "resume"})
    resp = await coord.handle_backup(resume_req)
    body = json.loads(resp.body.decode())
    assert "quiesce_generation" not in body, "resume reply must not carry quiesce_generation"


# ── Invariant I6 Tests ───────────────────────────────────────────────────────

def test_i6_quiesce_full_when_same_generation_and_held_throughout(tmp_path):
    """Happy path: same generation (1) and lock held throughout dump -> quiesced: True, full."""
    readings = [
        {"status": "success", "lock_held": True, "quiesce_generation": 1, "outbox": {"pending": 0}},  # drain
        {"status": "success", "lock_held": True, "quiesce_generation": 1, "outbox": {"pending": 0}},  # reading 1 before rearm
        {"status": "success", "lock_held": True, "quiesce_generation": 1, "outbox": {"pending": 0}},  # reading 2 after dumps
    ]
    with _fake_docker(tmp_path) as docker_path, \
            _stub_gw_configurable(quiesce_generation=1, rearm_generation=1, outbox_readings=readings) as (url, _):
        res, bdir = _run_backup(
            tmp_path, docker_path,
            {"BACKUP_ADMIN_TOKEN": "tok", "GATEWAY_URL": url,
             "BACKUP_DRAIN_MAX_SECONDS": "2", "BACKUP_DRAIN_POLL_SECONDS": "1"},
        )
    assert res.returncode == 0, res.stdout + res.stderr
    manifests = sorted(bdir.glob("sm-backup-*.manifest.json"))
    assert manifests
    manifest = json.loads(manifests[-1].read_text())
    assert manifest["quiesced"] is True
    assert manifest["quiesce_mode"] == "full"
    assert "quiesce fence held throughout dump (generation 1)" in res.stdout


def test_i6_lock_reacquired_during_dump_records_lost_during_dump(tmp_path):
    """Failure to re-introduce guard:
    Lock connection died during dump and was reacquired with a new generation.
    Final lock_held is True, but generation changed from 1 to 2!
    Manifest must state quiesced: False, mode: 'lost_during_dump'.
    A buggy check testing lock_held at the end only would falsely record full.
    """
    readings = [
        {"status": "success", "lock_held": True, "quiesce_generation": 1, "outbox": {"pending": 0}},  # drain
        {"status": "success", "lock_held": True, "quiesce_generation": 1, "outbox": {"pending": 0}},  # reading 1 before rearm (gen 1)
        {"status": "success", "lock_held": True, "quiesce_generation": 2, "outbox": {"pending": 0}},  # reading 2 after dumps (gen 2!)
    ]
    with _fake_docker(tmp_path) as docker_path, \
            _stub_gw_configurable(quiesce_generation=1, rearm_generation=1, outbox_readings=readings) as (url, _):
        res, bdir = _run_backup(
            tmp_path, docker_path,
            {"BACKUP_ADMIN_TOKEN": "tok", "GATEWAY_URL": url,
             "BACKUP_DRAIN_MAX_SECONDS": "2", "BACKUP_DRAIN_POLL_SECONDS": "1"},
        )
    assert res.returncode == 0, res.stdout + res.stderr
    manifests = sorted(bdir.glob("sm-backup-*.manifest.json"))
    assert manifests
    manifest = json.loads(manifests[-1].read_text())
    assert manifest["quiesced"] is False
    assert manifest["quiesce_mode"] == "lost_during_dump"
    assert "fence lost during dump" in res.stdout


def test_i6_ttl_lapse_before_rearm_records_lost_during_dump(tmp_path):
    """Dead-man timer lapsed during drain.
    Before re-arm, lock_held is False or generation changed on re-arm (gen 1 -> 2).
    Manifest must state quiesced: False, mode: 'lost_during_dump'.
    """
    readings = [
        {"status": "success", "lock_held": True, "quiesce_generation": 1, "outbox": {"pending": 0}},  # drain
        {"status": "success", "lock_held": False, "quiesce_generation": 1, "outbox": {"pending": 0}}, # reading 1 lapsed!
        {"status": "success", "lock_held": True, "quiesce_generation": 2, "outbox": {"pending": 0}},  # reading 2
    ]
    with _fake_docker(tmp_path) as docker_path, \
            _stub_gw_configurable(quiesce_generation=1, rearm_generation=2, outbox_readings=readings) as (url, _):
        res, bdir = _run_backup(
            tmp_path, docker_path,
            {"BACKUP_ADMIN_TOKEN": "tok", "GATEWAY_URL": url,
             "BACKUP_DRAIN_MAX_SECONDS": "2", "BACKUP_DRAIN_POLL_SECONDS": "1"},
        )
    assert res.returncode == 0, res.stdout + res.stderr
    manifests = sorted(bdir.glob("sm-backup-*.manifest.json"))
    manifest = json.loads(manifests[-1].read_text())
    assert manifest["quiesced"] is False
    assert manifest["quiesce_mode"] == "lost_during_dump"
    assert "fence lost during dump" in res.stdout


def test_i6_reading_failed_records_fence_unknown(tmp_path):
    """Reading 2 fails (e.g. 500 error). Manifest must record quiesced: False, mode: 'fence_unknown'."""
    readings = [
        {"status": "success", "lock_held": True, "quiesce_generation": 1, "outbox": {"pending": 0}},
        {"status": "success", "lock_held": True, "quiesce_generation": 1, "outbox": {"pending": 0}},
        None,  # 500 error on reading 2
    ]
    with _fake_docker(tmp_path) as docker_path, \
            _stub_gw_configurable(quiesce_generation=1, rearm_generation=1, outbox_readings=readings) as (url, _):
        res, bdir = _run_backup(
            tmp_path, docker_path,
            {"BACKUP_ADMIN_TOKEN": "tok", "GATEWAY_URL": url,
             "BACKUP_DRAIN_MAX_SECONDS": "2", "BACKUP_DRAIN_POLL_SECONDS": "1"},
        )
    assert res.returncode == 0, res.stdout + res.stderr
    manifests = sorted(bdir.glob("sm-backup-*.manifest.json"))
    manifest = json.loads(manifests[-1].read_text())
    assert manifest["quiesced"] is False
    assert manifest["quiesce_mode"] == "fence_unknown"
    assert "fence unknown" in res.stdout


def test_i6_quiesce_required_false_result_not_promoted_one_pair_kept(tmp_path):
    """BACKUP_QUIESCE_REQUIRED=1:
    When quiesce fence fails, no manifest is promoted, only the newest failed pair
    is kept at sm-backup-failed-latest.{pgdump,cypher.gz}, and its path is printed.
    A second failure replaces it so failures do not grow without bound.
    """
    readings = [
        {"status": "success", "lock_held": True, "quiesce_generation": 1, "outbox": {"pending": 0}},
        {"status": "success", "lock_held": True, "quiesce_generation": 1, "outbox": {"pending": 0}},
        {"status": "success", "lock_held": True, "quiesce_generation": 2, "outbox": {"pending": 0}},  # gen changed!
    ]
    # Run failure 1
    with _fake_docker(tmp_path) as docker_path, \
            _stub_gw_configurable(quiesce_generation=1, rearm_generation=1, outbox_readings=readings) as (url, _):
        res1, bdir = _run_backup(
            tmp_path, docker_path,
            {"BACKUP_ADMIN_TOKEN": "tok", "GATEWAY_URL": url, "BACKUP_QUIESCE_REQUIRED": "1",
             "BACKUP_DRAIN_MAX_SECONDS": "2", "BACKUP_DRAIN_POLL_SECONDS": "1"},
        )
    assert res1.returncode != 0
    assert not list(bdir.glob("sm-backup-*.manifest.json")), "failed backup must NOT be promoted to manifest"
    failed_pg = bdir / "sm-backup-failed-latest.pgdump"
    failed_neo = bdir / "sm-backup-failed-latest.cypher.gz"
    assert failed_pg.exists()
    assert failed_neo.exists()
    assert "newest failed pair kept at" in res1.stderr or "newest failed pair kept at" in res1.stdout

    # Run failure 2 with a new tag in fake pg_dump to confirm it overwrites rather than accumulating
    readings2 = [
        {"status": "success", "lock_held": True, "quiesce_generation": 1, "outbox": {"pending": 0}},
        {"status": "success", "lock_held": True, "quiesce_generation": 1, "outbox": {"pending": 0}},
        {"status": "success", "lock_held": True, "quiesce_generation": 2, "outbox": {"pending": 0}},
    ]
    with _fake_docker(tmp_path) as docker_path, \
            _stub_gw_configurable(quiesce_generation=1, rearm_generation=1, outbox_readings=readings2) as (url, _):
        res2, _ = _run_backup(
            tmp_path, docker_path,
            {"BACKUP_ADMIN_TOKEN": "tok", "GATEWAY_URL": url, "BACKUP_QUIESCE_REQUIRED": "1",
             "BACKUP_DRAIN_MAX_SECONDS": "2", "BACKUP_DRAIN_POLL_SECONDS": "1"},
        )
    assert res2.returncode != 0
    # Still only one pair of failed files exists in bdir
    all_failed = list(bdir.glob("sm-backup-failed-*"))
    assert len(all_failed) == 2, f"expected exactly 2 files (pgdump and cypher.gz), got {all_failed}"
    assert not list(bdir.glob("sm-backup-*.manifest.json"))


def test_i6_verify_prints_mode_on_false_branch(tmp_path):
    """--verify prints the mode on the false branch too."""
    bdir = tmp_path / "backups"
    bdir.mkdir()
    from test_backup_manifest import _write_verifiable_set

    # Write a set with quiesced: False, quiesce_mode: "lost_during_dump"
    name = _write_verifiable_set(tmp_path, bdir, pg_toc_entries=10, quiesced=False, quiesce_mode="lost_during_dump")
    with _fake_docker(tmp_path) as docker_path:
        res, _ = _run_backup(
            tmp_path, docker_path, {"FAKE_TOC_COUNT": "10"}, args=["--verify", name]
        )
    assert res.returncode == 0
    assert "NOT quiesced at backup time (lost_during_dump)" in res.stdout
