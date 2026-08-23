"""ops/restore.sh — --force must REPLACE the Neo4j store, not fail half way.

MEASURED DEFECT (found on a real cross-machine restore, 2026-08-23). The
Postgres half runs `pg_restore --clean --if-exists`, so it genuinely replaces
what is there. The Neo4j half did neither of the two things that requires:

  1. The APOC export emits bare `CREATE CONSTRAINT <name> FOR …` and
     `CREATE [RANGE|POINT|…] INDEX FOR …` — no IF NOT EXISTS. Replaying onto a
     store that already has them dies with "An equivalent constraint already
     exists", aborting the open transaction. Since Postgres is restored FIRST,
     the run ends with the two stores divergent — the exact state a quiesced
     backup exists to prevent, manufactured during the restore instead.
  2. The replay never CLEARED the graph, so a forced restore merged the
     incoming graph into the existing one. Node counts then exceed the manifest
     and restore.sh's own closing comparison reports a mismatch it cannot explain.

Both only appear on a NON-EMPTY target — which is precisely the target --force
exists for, so the feature could never work in the situation it was written for.

These drive the REAL shipped restore.sh with a fake `docker` on PATH, capturing
what is actually piped to cypher-shell. Asserting on the script's source text
instead would prove only that a string is present.
"""
import os
import shutil
import stat
import subprocess
import gzip
import json
import hashlib
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
RESTORE_SRC = REPO_ROOT / "shared-memory" / "ops" / "restore.sh"

# A faithful miniature of a real APOC export: transaction markers, an unnamed
# POINT index, an unnamed RANGE index, a named constraint, a DROP, and data.
FAKE_EXPORT = """:begin
CREATE POINT INDEX FOR (n:Entity) ON (n.location);
CREATE RANGE INDEX FOR (n:Conversation) ON (n.archived);
CREATE CONSTRAINT ai_agent_name FOR (node:AIAgent) REQUIRE (node.name) IS UNIQUE;
CREATE CONSTRAINT entity_name FOR (node:Entity) REQUIRE (node.name) IS UNIQUE;
:commit
UNWIND [{name:"a"}] AS row
CREATE (n:Entity{name: row.name});
DROP CONSTRAINT UNIQUE_IMPORT_NAME;
CALL db.awaitIndexes(300);
"""

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Fake `docker` for restore.sh. Records every cypher-shell stdin payload and
# every cypher-shell argument, so a test can see what the script really sent.
cmd="$1"; shift
[[ "$cmd" == "exec" ]] || exit 0
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
  psql)
    # restore.sh asks for the technical_docs row count.
    echo "${FAKE_PG_ROWS:-7}"
    ;;
  pg_restore) cat >/dev/null ;;
  cypher-shell)
    args=""
    while [[ "$1" == -* ]]; do
      case "$1" in
        --format) shift 2 ;;
        -u) shift 2 ;;
        *) shift ;;
      esac
    done
    args="$*"
    if [[ -n "$args" ]]; then
      printf '%s\n' "$args" >> "$CAPTURE_DIR/cypher_args.txt"
      case "$args" in
        *"MATCH (n) RETURN count(n)"*) printf 'count(n)\n%s\n' "${FAKE_NODES:-3}" ;;
        *"MATCH ()-[r]->() RETURN count(r)"*) printf 'count(r)\n%s\n' "${FAKE_RELS:-4}" ;;
      esac
    else
      cat >> "$CAPTURE_DIR/replay_stdin.cypher"
    fi
    ;;
esac
exit 0
"""


def _make_set(backup_dir: Path, name: str = "sm-backup-test", with_logs: bool = False) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    pgdump = backup_dir / f"{name}.pgdump"
    cypher = backup_dir / f"{name}.cypher.gz"
    pgdump.write_bytes(b"FAKE-PGDUMP")
    cypher.write_bytes(gzip.compress(FAKE_EXPORT.encode()))
    logs_extra = {}
    if with_logs:
        srcdir = backup_dir.parent / "_logsrc" / "logs"
        srcdir.mkdir(parents=True, exist_ok=True)
        (srcdir / "credential-audit.jsonl").write_text('{"event":"from the OTHER host"}\n')
        tarpath = backup_dir / f"{name}.logs.tar.gz"
        with tarfile.open(tarpath, "w:gz") as t:
            t.add(srcdir, arcname="logs")
        logs_extra = {
            "logs_file": f"{name}.logs.tar.gz",
            "logs_sha256": hashlib.sha256(tarpath.read_bytes()).hexdigest(),
        }
    (backup_dir / f"{name}.manifest.json").write_text(json.dumps({**logs_extra,
        "name": name,
        "pg_db": "agent_data",
        "pg_file": f"{name}.pgdump",
        "pg_sha256": hashlib.sha256(pgdump.read_bytes()).hexdigest(),
        "neo4j_file": f"{name}.cypher.gz",
        "neo4j_sha256": hashlib.sha256(cypher.read_bytes()).hexdigest(),
        "neo4j_nodes": "3",
        "neo4j_rels": "4",
    }))


def _run_restore(tmp_path, args, nodes="3", pg_rows="7", with_logs=False):
    fake_root = tmp_path / "repo"
    ops = fake_root / "shared-memory" / "ops"
    ops.mkdir(parents=True)
    shutil.copy(RESTORE_SRC, ops / "restore.sh")
    (fake_root / "shared-memory" / ".env").write_text(
        "PG_PASSWORD=fake\nNEO4J_PASSWORD=fake\n")

    docker = tmp_path / "fake_docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    capture = tmp_path / "capture"
    capture.mkdir()
    backups = tmp_path / "backups"
    _make_set(backups, with_logs=with_logs)

    env = dict(os.environ)
    env.update({
        "DOCKER": str(docker),
        "BACKUP_DIR": str(backups),
        "CAPTURE_DIR": str(capture),
        "FAKE_NODES": nodes,
        "FAKE_PG_ROWS": pg_rows,
        "HOME": str(tmp_path),
    })
    res = subprocess.run(
        ["bash", str(ops / "restore.sh"), "sm-backup-test", *args],
        capture_output=True, text=True, timeout=60, env=env, cwd=str(fake_root),
    )
    replay = capture / "replay_stdin.cypher"
    argfile = capture / "cypher_args.txt"
    return (res,
            replay.read_text() if replay.exists() else "",
            argfile.read_text() if argfile.exists() else "")


def test_every_schema_statement_is_replayed_idempotently(tmp_path):
    """THE regression. Each of these aborted the whole transaction on a target
    that already had them — which every live host does."""
    res, replay, _ = _run_restore(tmp_path, ["--force"])

    assert res.returncode == 0, res.stdout + res.stderr
    for stmt in ("CREATE POINT INDEX", "CREATE RANGE INDEX",
                 "CREATE CONSTRAINT ai_agent_name", "CREATE CONSTRAINT entity_name"):
        line = next(l for l in replay.splitlines() if l.startswith(stmt))
        assert "IF NOT EXISTS" in line, f"unguarded schema statement replayed: {line}"
    drop = next(l for l in replay.splitlines() if l.startswith("DROP CONSTRAINT"))
    assert "IF EXISTS" in drop, f"unguarded drop replayed: {drop}"


def test_the_data_statements_are_passed_through_untouched(tmp_path):
    """The rewrite must touch schema statements ONLY. A stream edit that also
    caught data would corrupt the restore silently, which is far worse than the
    defect being fixed."""
    _, replay, _ = _run_restore(tmp_path, ["--force"])

    assert 'UNWIND [{name:"a"}] AS row' in replay
    assert 'CREATE (n:Entity{name: row.name});' in replay
    assert "CALL db.awaitIndexes(300);" in replay
    assert ":begin" in replay and ":commit" in replay


def test_forcing_over_a_populated_graph_clears_it_first(tmp_path):
    """--force means REPLACE, as it already does for Postgres (--clean).
    Without this the incoming graph is MERGED into the existing one and the
    node count silently exceeds the manifest."""
    _, _, args = _run_restore(tmp_path, ["--force"], nodes="3")

    assert "DETACH DELETE" in args, "a forced restore did not clear the target graph"


def test_an_empty_target_is_not_cleared(tmp_path):
    """Nothing to replace, so no destructive statement should be issued at all —
    the documented empty-target path must stay exactly as cheap as it was."""
    _, _, args = _run_restore(tmp_path, [], nodes="0", pg_rows="0")

    assert "DETACH DELETE" not in args


def test_a_non_empty_target_still_refuses_without_force(tmp_path):
    """The existing guard must survive the fix — clearing the graph is something
    --force authorises, never something the restore decides on its own."""
    res, replay, args = _run_restore(tmp_path, [], nodes="3", pg_rows="7")

    assert res.returncode != 0
    assert "pass --force to overwrite" in (res.stdout + res.stderr)
    assert "DETACH DELETE" not in args
    assert replay == "", "a refused restore must not replay anything"


def test_a_successful_restore_says_the_data_is_not_yet_migrated(tmp_path):
    """Finding from the Test & Verification review of this branch.

    The closing message is the ONLY thing connecting a finished restore to the
    forward-migration that must follow it. Before it existed the script's last
    line read "Restore complete", which an operator reasonably takes as done —
    and the restored database sits at whatever schema level the dump was taken
    at, under a gateway expecting a newer one.

    Untested, it could be deleted or reworded to nothing and every other test
    here would still pass.
    """
    res, _, _ = _run_restore(tmp_path, ["--force"])
    out = res.stdout + res.stderr

    assert "NOT yet migrated" in out
    assert "update_framework.sh --from-restore" in out


def test_a_refused_restore_does_not_claim_anything_was_restored(tmp_path):
    """The counterpart: the hand-off message must not appear when nothing was
    restored, or it would send the operator to migrate a database that never
    received the dump."""
    res, _, _ = _run_restore(tmp_path, [], nodes="3", pg_rows="7")
    out = res.stdout + res.stderr

    assert "NOT yet migrated" not in out
    assert "--from-restore" not in out


def test_the_graph_is_cleared_before_postgres_is_overwritten(tmp_path):
    """Review finding (Code Quality / Test & Verification / Adversarial, all three).

    The clear used to run AFTER pg_restore. A clear that then failed — timeout,
    dropped connection, heap pressure mid-batch — left Postgres holding the
    restored corpus and Neo4j holding a partly-emptied old one, and the script
    died there: the split-brain a quiesced backup exists to prevent, manufactured
    by the restore itself.

    Ordering is the fix, so ordering is what is asserted — not that both
    statements merely happened.
    """
    res, _, _ = _run_restore(tmp_path, ["--force"])
    out = res.stdout + res.stderr

    assert "Clearing Neo4j" in out and "Restoring Postgres" in out
    assert out.index("Clearing Neo4j") < out.index("Restoring Postgres"), (
        "the destructive Neo4j clear runs after Postgres is overwritten — a "
        "failure there leaves the two stores divergent")


def test_the_export_is_one_statement_per_line(tmp_path):
    """Adversarial finding: the in-stream `sed` is line-anchored, so it is only
    safe while no DATA line can begin with `CREATE CONSTRAINT`/`CREATE INDEX`.

    That holds because the exporter emits one statement per line and escapes
    newlines inside string properties — measured on a real 2554-line export:
    every line began with a statement keyword, 700 data lines began with
    `CREATE ` and none matched the schema pattern.

    It was an unstated assumption, which is how it would have been broken later.
    Pinned here against the fixture so a future exporter change that emits
    multi-line values fails loudly instead of silently rewriting data.
    """
    import re
    schema_re = re.compile(r"^CREATE (CONSTRAINT|([A-Z]+ )?INDEX)")
    keyword_re = re.compile(r"^(CREATE|MATCH|UNWIND|DROP|CALL|:begin|:commit)")

    data_lines = [l for l in FAKE_EXPORT.splitlines()
                  if l.strip() and not schema_re.match(l)]
    for line in data_lines:
        assert not schema_re.match(line)
    # Every non-blank line is a statement start, never a continuation of a
    # multi-line value — the property the line anchor depends on.
    for line in FAKE_EXPORT.splitlines():
        if line.strip():
            assert keyword_re.match(line), (
                f"line is not a statement start: {line!r} — if the exporter has "
                f"begun emitting multi-line values, the line-anchored sed is no "
                f"longer safe")


def test_rewritten_schema_statements_keep_a_valid_shape(tmp_path):
    """The replay harness uses a fake cypher-shell, which cannot reject invalid
    Cypher — so assert the rewritten statements still match the grammar shape
    Neo4j documents, rather than only that a substring was inserted."""
    import re
    _, replay, _ = _run_restore(tmp_path, ["--force"])

    for line in replay.splitlines():
        if line.startswith("CREATE CONSTRAINT"):
            assert re.match(
                r"^CREATE CONSTRAINT \w+ IF NOT EXISTS FOR \(\w+:\w+\) REQUIRE .+;$",
                line), f"malformed constraint after rewrite: {line}"
        elif line.startswith("CREATE") and "INDEX" in line:
            assert re.match(
                r"^CREATE ([A-Z]+ )?INDEX IF NOT EXISTS FOR \(\w+:\w+\) ON .+;$",
                line), f"malformed index after rewrite: {line}"


# ── Restored logs must never become live logs ────────────────────────────────
#
# The monitor reads the framework log directory directly (its logs_reader), and
# no backup contained it — so a restored deployment showed a healthy corpus and
# could not surface a single warning. Backing them up is the fix; unpacking them
# into the live files would have been a worse bug than the one being fixed,
# because an audit trail carrying another machine's events as if they were local
# is confidently wrong rather than merely short.


def _live_logs(tmp_path):
    d = tmp_path / ".shared-memory" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    (d / "credential-audit.jsonl").write_text('{"event":"THIS host"}\n')
    return d


def test_restored_logs_do_not_overwrite_the_live_ones(tmp_path):
    """THE guard. The live audit trail must read exactly as it did before."""
    live = _live_logs(tmp_path)
    before = (live / "credential-audit.jsonl").read_text()

    _run_restore(tmp_path, ["--force"], with_logs=True)

    assert (live / "credential-audit.jsonl").read_text() == before
    assert "from the OTHER host" not in (live / "credential-audit.jsonl").read_text()


def test_restored_logs_land_outside_the_directory_the_monitor_reads(tmp_path):
    """A sidecar INSIDE the live log directory was the first idea and is wrong:
    logs_reader and logrotate both work over that directory."""
    live = _live_logs(tmp_path)
    _run_restore(tmp_path, ["--force"], with_logs=True)

    restored = tmp_path / ".shared-memory" / "restored-logs"
    assert restored.exists(), "restored logs were not written to the sidecar"
    assert not (live / "restored").exists(), "sidecar is inside the live log dir"
    # Nothing new appeared in the live directory at all.
    assert {f.name for f in live.iterdir()} == {"credential-audit.jsonl"}


def test_every_restored_log_file_carries_the_prefix(tmp_path):
    """A directory name labels a file only while the file stays in it."""
    _live_logs(tmp_path)
    _run_restore(tmp_path, ["--force"], with_logs=True)

    restored = tmp_path / ".shared-memory" / "restored-logs"
    # RESTORED.json is deliberately NOT prefixed: it is the marker a reader
    # looks up by a stable name to learn that everything beside it is another
    # host's history. Prefixing the label would defeat the label.
    files = [f for f in restored.rglob("*")
             if f.is_file() and f.name != "RESTORED.json"]
    assert files, "nothing was extracted"
    for f in files:
        assert f.name.startswith("restored-"), f"unprefixed restored log: {f}"


def test_the_restored_set_is_discoverable_and_self_describing(tmp_path):
    """Keeping restored logs out of the live directory stops them being read as
    local events — and, alone, also stops them being read at all. A reader needs
    a stable path and a statement of what it is looking at."""
    _live_logs(tmp_path)
    _run_restore(tmp_path, ["--force"], with_logs=True)

    latest = tmp_path / ".shared-memory" / "restored-logs" / "latest"
    assert latest.exists(), "no 'latest' pointer for a reader to follow"
    marker = json.loads((latest / "RESTORED.json").read_text())
    assert marker["is_local_history"] is False
    assert marker["set"]


def test_a_set_without_logs_restores_and_says_so(tmp_path):
    """Every set written before log capture has none; absence is normal, and the
    operator is told what a monitor here will therefore be missing."""
    _live_logs(tmp_path)
    res, _, _ = _run_restore(tmp_path, ["--force"], with_logs=False)

    assert res.returncode == 0, res.stdout + res.stderr
    assert "no logs in this set" in (res.stdout + res.stderr)
    assert not (tmp_path / ".shared-memory" / "restored-logs").exists()
