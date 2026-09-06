"""ops/backup.sh manifest correctness — pg_toc_entries and quiesced/quiesce_mode.

Measured defect this file guards against: backup.sh computed pg_toc_entries by
running `command -v pg_restore` / `pg_restore --list` ON THE HOST. Postgres runs
only inside $PG_CONTAINER; the host carries no postgres client tools, so
`command -v pg_restore` always failed and the `|| echo 0` fallback always fired
-- every manifest this framework has ever written recorded pg_toc_entries: 0,
a false integrity signal (measured live: a real backup set's manifest said 0,
listing the same dump inside the container reported 189 real entries).
`restore.sh` never reads pg_toc_entries (grepped -- confirmed absent), so this
was silent: it never broke a restore, it just lied in the manifest and in
`--verify`'s own archive-readable check (same host-side `command -v pg_restore`
bug, same silent skip).

Fix: `pgdump_toc()` in backup.sh now runs pg_restore --list INSIDE the
container, piping the host-side .pgdump file in via stdin (the archive lives
on the host; pg_restore lives in the container) -- reusing the script's
existing docker-exec idiom rather than inventing a second way to reach
Postgres. `--verify` uses the SAME function, so the archive-readable check
that silently no-op'd on every host now actually runs, and cross-checks the
live count against what the manifest recorded when the manifest is new enough
to carry a real one.

Backward compatibility: every manifest ever written before this fix recorded
pg_toc_entries: 0 (the ONLY value the old buggy line could ever produce, since
the host-side check always failed the same way) -- so 0 is NOT a genuinely
observed empty archive, it is the pre-fix sentinel, and --verify treats it the
same as an absent field (informational, never a MISMATCH) rather than diffing
against it. A NEW manifest with no recorded field at all (written by some
future caller that dropped the key) is likewise "unknown", never coerced to
a failure.

Also added: `quiesced` (bool) / `quiesce_mode` ("full"|"timeout"|null) on the
manifest -- previously nothing recorded whether client writes were shed and
daemons drained when a backup was taken, even though restore.sh's own closing
message already distinguishes "ran without full quiesce" as a normal case.

No docker, no live gateway: a fake `docker` CLI stub (this file writes one to
a tmp dir and points $DOCKER at it) stands in for the container boundary,
exercising backup.sh's REAL bash logic end to end (do_backup and do_verify),
never a hand-written reimplementation that could silently drift from the
shipped script -- same technique as test_postflight_a8.py's stub-server split.
A local http.server stub stands in for the gateway's /admin/backup and
/memory/telemetry, covering the 200 (full drain) and 202 (drain timeout)
quiesce responses without any live infrastructure.
"""
import contextlib
import http.server
import json
import os
import stat
import subprocess
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
BACKUP_SH = REPO_ROOT / "shared-memory" / "ops" / "backup.sh"

FAKE_DOCKER_SCRIPT = r"""#!/usr/bin/env bash
# Fake `docker` for backup.sh tests -- dispatches only what backup.sh calls.
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
  pg_dump) printf 'FAKE-PGDUMP-BYTES-%s\n' "${FAKE_DUMP_TAG:-x}" ;;
  pg_restore)
    if [[ "$1" == "--list" ]]; then
      cat >/dev/null
      echo "; header comment"
      echo ""
      for i in $(seq 1 "${FAKE_TOC_COUNT:-42}"); do echo "$i; TABLE public thing"; done
    fi
    ;;
  cypher-shell)
    q="$*"
    case "$q" in
      *"SHOW SETTINGS"*) printf 'value\n"%s"\n' "${FAKE_IMPORT_DIR:-/fake/import}" ;;
      *"apoc.export.cypher.all"*) echo ok ;;
      *"MATCH (n) RETURN count(n)"*) printf 'count(n)\n%s\n' "${FAKE_NODES:-5}" ;;
      *"MATCH ()-[r]->() RETURN count(r)"*) printf 'count(r)\n%s\n' "${FAKE_RELS:-9}" ;;
    esac
    ;;
  cat) printf 'FAKE-CYPHER-EXPORT-%s\n' "${FAKE_DUMP_TAG:-x}" ;;
  rm) : ;;
esac
"""


@contextlib.contextmanager
def _fake_docker(tmp_path):
    path = tmp_path / "fake_docker"
    path.write_text(FAKE_DOCKER_SCRIPT)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    yield str(path)


#: The shape drain_outbox reads: the single outbox.pending on GET /admin/outbox
#: (v0.9.92) — the admin token is confined to /admin/*, so this is the ONLY
#: route the backup token can poll for the drain count; the coordinator builds
#: `pending` as census pending + in_progress.
_OUTBOX_DRAINED = {"status": "success", "outbox": {"pending": 0}}

#: The gateway's real 403 body for an admin token outside /admin/* (v0.9.92,
#: coordinator.py `_error_body` via auth_middleware) -- this is what EVERY
#: non-/admin/outbox GET must answer now that the admin token cannot reach
#: /memory/telemetry at all (fact:2022).
_ADMIN_CONFINEMENT_BODY = {
    "status": "error",
    "message": "Admin token is confined to /admin/* routes. The credential is "
                "VALID — use a write-capable agent token for this route.",
}


class _QuiesceStubHandler(http.server.BaseHTTPRequestHandler):
    quiesce_status = 200
    resume_calls = 0
    outbox_body = _OUTBOX_DRAINED
    # Simulates a pre-0.9.92 gateway (or a build that forgot to add
    # /admin/outbox to _ADMIN_ROUTES): the admin token is confined off THIS
    # route too, so every GET -- /admin/outbox included -- gets the same 403.
    confine_admin_outbox = False
    # Every GET path this stub was asked for, in order -- reset per
    # _stub_gateway() context. Lets a test assert the drain gate actually
    # polled /admin/outbox rather than inferring the route from a 403
    # side-effect (a pre-0.9.92 gate polling /memory/telemetry would also
    # see a 403 body, so the status code alone cannot prove which route).
    seen_get_paths: list = []
    # Override for the 403 body's `message`, when confine_admin_outbox is set
    # -- lets a test hand back an attacker-controlled string (e.g. terminal
    # escape sequences) without changing the default confinement text.
    confinement_body = _ADMIN_CONFINEMENT_BODY

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        payload = json.loads(body or b"{}")
        if payload.get("state") == "quiesce":
            self.send_response(type(self).quiesce_status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"daemons": "drained"}).encode())
        else:
            type(self).resume_calls += 1
            self.send_response(200)
            self.end_headers()

    def do_GET(self):
        # BRANCH ON PATH (v0.9.92): the admin token this script carries can
        # reach ONLY /admin/outbox -- every other GET (including the old
        # /memory/telemetry drain poll) must answer the gateway's real 403
        # confinement body, so a build that still polls the wrong route fails
        # this stub exactly the way it fails a real gateway.
        type(self).seen_get_paths.append(self.path)
        if self.path == "/admin/outbox" and not type(self).confine_admin_outbox:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(type(self).outbox_body).encode())
        else:
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(type(self).confinement_body).encode())

    def log_message(self, *args, **kwargs):
        pass


@contextlib.contextmanager
def _stub_gateway(quiesce_status=200, outbox_body=None, confine_admin_outbox=False,
                   confinement_body=None):
    handler_cls = type("Handler", (_QuiesceStubHandler,), {
        "quiesce_status": quiesce_status, "resume_calls": 0,
        "outbox_body": _OUTBOX_DRAINED if outbox_body is None else outbox_body,
        "confine_admin_outbox": confine_admin_outbox,
        "seen_get_paths": [],
        "confinement_body": _ADMIN_CONFINEMENT_BODY if confinement_body is None else confinement_body,
    })
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", handler_cls
    finally:
        server.shutdown()
        server.server_close()


def _run_backup_sh(tmp_path, docker_path, extra_env, args=()):
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
    result = subprocess.run(
        ["bash", str(BACKUP_SH), *args],
        env=env, capture_output=True, text=True, timeout=30,
    )
    return result, backup_dir


def _latest_manifest(backup_dir):
    manifests = sorted(backup_dir.glob("sm-backup-*.manifest.json"))
    assert manifests, f"no manifest written in {backup_dir}"
    return json.loads(manifests[-1].read_text())


# ── do_backup: pg_toc_entries computed via the container, not the host ──────

def test_manifest_records_real_toc_count_from_container(tmp_path):
    with _fake_docker(tmp_path) as docker_path:
        result, backup_dir = _run_backup_sh(
            tmp_path, docker_path,
            {"BACKUP_ADMIN_TOKEN": "", "FAKE_TOC_COUNT": "37"},
        )
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = _latest_manifest(backup_dir)
    assert manifest["pg_toc_entries"] == 37


def test_manifest_records_quiesced_false_when_no_admin_token(tmp_path):
    with _fake_docker(tmp_path) as docker_path:
        result, backup_dir = _run_backup_sh(
            tmp_path, docker_path, {"BACKUP_ADMIN_TOKEN": ""},
        )
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = _latest_manifest(backup_dir)
    assert manifest["quiesced"] is False
    assert manifest["quiesce_mode"] is None


def test_manifest_records_quiesced_full_on_200(tmp_path):
    with _fake_docker(tmp_path) as docker_path, _stub_gateway(200) as (url, handler_cls):
        result, backup_dir = _run_backup_sh(
            tmp_path, docker_path,
            {"BACKUP_ADMIN_TOKEN": "tok", "GATEWAY_URL": url,
             "BACKUP_DRAIN_MAX_SECONDS": "2", "BACKUP_DRAIN_POLL_SECONDS": "1"},
        )
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = _latest_manifest(backup_dir)
    assert manifest["quiesced"] is True
    assert manifest["quiesce_mode"] == "full"
    assert handler_cls.resume_calls == 1  # released the fence


def test_drain_reports_drained_when_the_outbox_pending_key_says_zero(tmp_path):
    """The happy path, read from the key drain_outbox actually gates on."""
    with _fake_docker(tmp_path) as docker_path, \
            _stub_gateway(200, {"status": "success", "outbox": {"pending": 0}}) as (url, _h):
        result, _ = _run_backup_sh(
            tmp_path, docker_path,
            {"BACKUP_ADMIN_TOKEN": "tok", "GATEWAY_URL": url,
             "BACKUP_DRAIN_MAX_SECONDS": "2", "BACKUP_DRAIN_POLL_SECONDS": "1"},
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "outbox drained" in result.stdout, result.stdout


def test_drain_does_not_report_drained_while_the_outbox_still_has_work(tmp_path):
    """pending: 3 must never read as drained — it is the whole point of the gate."""
    with _fake_docker(tmp_path) as docker_path, \
            _stub_gateway(200, {"status": "success", "outbox": {"pending": 3}}) as (url, h):
        result, _ = _run_backup_sh(
            tmp_path, docker_path,
            {"BACKUP_ADMIN_TOKEN": "tok", "GATEWAY_URL": url,
             "BACKUP_DRAIN_MAX_SECONDS": "2", "BACKUP_DRAIN_POLL_SECONDS": "1"},
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "outbox drained" not in result.stdout, result.stdout
    assert "not fully drained" in result.stdout, result.stdout
    assert "/admin/outbox" in h.seen_get_paths, h.seen_get_paths


def test_drain_says_the_key_is_absent_instead_of_claiming_drained(tmp_path):
    """THE regression this file exists for at 0.9.91.

    With the old `${pend:-0}` / `${prog:-0}` defaults, a served body carrying no
    outbox key at all made both reads default to 0 and the FIRST poll print
    "✓ outbox drained" — a snapshot taken with Neo4j arbitrarily behind
    Postgres, under a green line. An absent key means the gate cannot answer:
    name the key, keep polling, fall through to the best-effort timeout.
    """
    with _fake_docker(tmp_path) as docker_path, \
            _stub_gateway(200, {"status": "success", "outbox": {}}) as (url, h):
        result, _ = _run_backup_sh(
            tmp_path, docker_path,
            {"BACKUP_ADMIN_TOKEN": "tok", "GATEWAY_URL": url,
             "BACKUP_DRAIN_MAX_SECONDS": "2", "BACKUP_DRAIN_POLL_SECONDS": "1"},
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "outbox drained" not in result.stdout, result.stdout
    assert f"outbox.pending absent from {url}/admin/outbox" in result.stdout, result.stdout
    assert "not fully drained" in result.stdout, result.stdout
    assert "/admin/outbox" in h.seen_get_paths, h.seen_get_paths


def test_drain_reports_drained_only_through_the_admin_route(tmp_path):
    """⭐ The regression this release exists for. The stub refuses EVERY GET
    that is not /admin/outbox with the gateway's real 403 confinement body —
    exactly what a real gateway does to an admin token today. This test FAILS
    at ceff79a (the gate polls /memory/telemetry, which the stub now 403s,
    so it never sees pending:0 and falls through to the timeout instead of
    printing "outbox drained") and passes once drain_outbox reads
    /admin/outbox instead."""
    with _fake_docker(tmp_path) as docker_path, \
            _stub_gateway(200, {"status": "success", "outbox": {"pending": 0}}) as (url, h):
        result, _ = _run_backup_sh(
            tmp_path, docker_path,
            {"BACKUP_ADMIN_TOKEN": "tok", "GATEWAY_URL": url,
             "BACKUP_DRAIN_MAX_SECONDS": "2", "BACKUP_DRAIN_POLL_SECONDS": "1"},
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "outbox drained" in result.stdout, result.stdout
    assert "/admin/outbox" in h.seen_get_paths, h.seen_get_paths
    assert not any(p.startswith("/memory/") for p in h.seen_get_paths), h.seen_get_paths


def test_drain_prints_the_gateway_refusal_once_on_403(tmp_path):
    """C2: a pre-0.9.92 gateway (or a build that forgot _ADMIN_ROUTES) answers
    403 on /admin/outbox too -- the gate must print the gateway's own refusal
    ONCE (not once per poll) so an operator can tell "pre-0.9.92 gateway /
    wrong role" from "route present, key absent", then fall through to the
    timeout without ever claiming drained."""
    with _fake_docker(tmp_path) as docker_path, \
            _stub_gateway(200, confine_admin_outbox=True) as (url, h):
        result, _ = _run_backup_sh(
            tmp_path, docker_path,
            {"BACKUP_ADMIN_TOKEN": "tok", "GATEWAY_URL": url,
             "BACKUP_DRAIN_MAX_SECONDS": "2", "BACKUP_DRAIN_POLL_SECONDS": "1"},
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "outbox drained" not in result.stdout, result.stdout
    assert "outbox.pending absent" in result.stdout, result.stdout
    assert result.stdout.count("Admin token is confined to /admin/* routes") == 1, result.stdout
    assert "not fully drained" in result.stdout, result.stdout
    assert "/admin/outbox" in h.seen_get_paths, h.seen_get_paths


def test_drain_strips_control_characters_from_the_gateway_message(tmp_path):
    """SEC S-1: the gateway's `message`/`error` text reaches the operator's
    terminal verbatim (fact:1499 -- both bodies are framework text, never a
    secret, but nothing upstream of drain_outbox constrains their BYTES). A
    spoofed or MITM'd endpoint could answer 403 on /admin/outbox with ANSI
    escape sequences -- a screen clear, a terminal-title spoof -- that the
    operator's emulator would interpret. drain_outbox must strip control
    characters before printing the line: the human text survives, ESC
    (\\x1b) does not."""
    poisoned_msg = "clear\x1b[2Jtitle\x1b]0;PWNED\x07 the human text"
    with _fake_docker(tmp_path) as docker_path, \
            _stub_gateway(200, confine_admin_outbox=True,
                          confinement_body={"status": "error", "message": poisoned_msg}) as (url, h):
        result, _ = _run_backup_sh(
            tmp_path, docker_path,
            {"BACKUP_ADMIN_TOKEN": "tok", "GATEWAY_URL": url,
             "BACKUP_DRAIN_MAX_SECONDS": "2", "BACKUP_DRAIN_POLL_SECONDS": "1"},
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "the human text" in result.stdout, result.stdout
    # The script's OWN ylw/grn color codes are legitimate ANSI and stay --
    # only the ATTACKER-SUPPLIED escape sequences must be gone from the
    # printed message line.
    assert "\x1b[2J" not in result.stdout, repr(result.stdout)
    assert "\x1b]0;PWNED" not in result.stdout, repr(result.stdout)
    assert "\x07" not in result.stdout, repr(result.stdout)
    assert "/admin/outbox" in h.seen_get_paths, h.seen_get_paths


def test_manifest_records_quiesced_timeout_on_202(tmp_path):
    with _fake_docker(tmp_path) as docker_path, _stub_gateway(202) as (url, handler_cls):
        result, backup_dir = _run_backup_sh(
            tmp_path, docker_path,
            {"BACKUP_ADMIN_TOKEN": "tok", "GATEWAY_URL": url,
             "BACKUP_DRAIN_MAX_SECONDS": "2", "BACKUP_DRAIN_POLL_SECONDS": "1"},
        )
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = _latest_manifest(backup_dir)
    assert manifest["quiesced"] is True
    assert manifest["quiesce_mode"] == "timeout"


# ── do_verify: legacy zero-sentinel vs a real mismatch ───────────────────────

def _write_verifiable_set(tmp_path, backup_dir, *, pg_toc_entries, quiesced=None,
                           quiesce_mode=None, name="sm-backup-fixture"):
    """Write a .pgdump/.cypher.gz/.manifest.json set whose sha256/gzip check out,
    so do_verify's TOC/quiesce checks are what's actually being exercised."""
    import gzip
    import hashlib

    pg_bytes = b"FIXTURE-PGDUMP-CONTENT\n"
    neo_bytes = gzip.compress(b"FIXTURE-CYPHER-EXPORT\n")
    (backup_dir / f"{name}.pgdump").write_bytes(pg_bytes)
    (backup_dir / f"{name}.cypher.gz").write_bytes(neo_bytes)
    manifest = {
        "name": name, "created": "2026-01-01T00:00:00+00:00",
        "pg_db": "agent_data",
        "pg_file": f"{name}.pgdump",
        "pg_sha256": hashlib.sha256(pg_bytes).hexdigest(),
        "pg_toc_entries": pg_toc_entries,
        "neo4j_file": f"{name}.cypher.gz",
        "neo4j_sha256": hashlib.sha256(neo_bytes).hexdigest(),
        "neo4j_nodes": "5", "neo4j_rels": "9",
    }
    if quiesced is not None:
        manifest["quiesced"] = quiesced
        manifest["quiesce_mode"] = quiesce_mode
    (backup_dir / f"{name}.manifest.json").write_text(json.dumps(manifest, indent=2))
    return name


def test_verify_treats_legacy_zero_toc_as_unknown_not_mismatch(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    name = _write_verifiable_set(tmp_path, backup_dir, pg_toc_entries=0)
    with _fake_docker(tmp_path) as docker_path:
        result, _ = _run_backup_sh(
            tmp_path, docker_path, {"FAKE_TOC_COUNT": "42"}, args=["--verify", name],
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "MISMATCH" not in result.stdout
    assert "predates a working count" in result.stdout


def test_verify_flags_a_real_toc_mismatch(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    name = _write_verifiable_set(tmp_path, backup_dir, pg_toc_entries=7)
    with _fake_docker(tmp_path) as docker_path:
        result, _ = _run_backup_sh(
            tmp_path, docker_path, {"FAKE_TOC_COUNT": "42"}, args=["--verify", name],
        )
    assert result.returncode != 0
    assert "MISMATCH" in result.stdout
    assert "7" in result.stdout and "42" in result.stdout


def test_verify_confirms_a_matching_toc(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    name = _write_verifiable_set(tmp_path, backup_dir, pg_toc_entries=42)
    with _fake_docker(tmp_path) as docker_path:
        result, _ = _run_backup_sh(
            tmp_path, docker_path, {"FAKE_TOC_COUNT": "42"}, args=["--verify", name],
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "matches manifest" in result.stdout


# ── do_verify: quiesced state is reported, never gates pass/fail ────────────

def test_verify_reports_quiesced_state_absent_true_false(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    cases = [
        (dict(pg_toc_entries=42, name="s-absent"), "unknown"),
        (dict(pg_toc_entries=42, quiesced=True, quiesce_mode="full", name="s-true"), "quiesced at backup time (full)"),
        (dict(pg_toc_entries=42, quiesced=False, quiesce_mode=None, name="s-false"), "NOT quiesced"),
    ]
    for kwargs, expect_text in cases:
        name = _write_verifiable_set(tmp_path, backup_dir, **kwargs)
        with _fake_docker(tmp_path) as docker_path:
            result, _ = _run_backup_sh(
                tmp_path, docker_path, {"FAKE_TOC_COUNT": "42"}, args=["--verify", name],
            )
        assert result.returncode == 0, result.stdout + result.stderr
        assert expect_text in result.stdout, (name, result.stdout)

    # json_get's bool→"true"/"false" rendering (not Python's "True"/"False")
    # is exercised for real above: every one of the three quiesced states
    # round-trips through the SHIPPED json_get inside backup.sh itself to
    # produce these exact messages -- mutation-checked by temporarily
    # dropping the bool special-case from backup.sh's json_get and
    # confirming this test (specifically the "s-true" case) is what breaks.
