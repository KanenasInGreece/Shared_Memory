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


def _make_set(backup_dir: Path, name: str = "sm-backup-test") -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    pgdump = backup_dir / f"{name}.pgdump"
    cypher = backup_dir / f"{name}.cypher.gz"
    pgdump.write_bytes(b"FAKE-PGDUMP")
    cypher.write_bytes(gzip.compress(FAKE_EXPORT.encode()))
    (backup_dir / f"{name}.manifest.json").write_text(json.dumps({
        "name": name,
        "pg_db": "agent_data",
        "pg_file": f"{name}.pgdump",
        "pg_sha256": hashlib.sha256(pgdump.read_bytes()).hexdigest(),
        "neo4j_file": f"{name}.cypher.gz",
        "neo4j_sha256": hashlib.sha256(cypher.read_bytes()).hexdigest(),
        "neo4j_nodes": "3",
        "neo4j_rels": "4",
    }))


def _run_restore(tmp_path, args, nodes="3", pg_rows="7"):
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
    _make_set(backups)

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
