#!/usr/bin/env python3
"""Make the ``PROJECT_OF`` edge mirror the Postgres project resolution.

Postgres metadata IS the project resolution (P1); the graph is a mirror of it.
Nothing enforced that, so the two stores drifted — and drifted SILENTLY, because
every reader asks one store or the other and never both.

WHAT DRIFT LOOKS LIKE, and why it was invisible. The edge writer was a bare
``MERGE`` until the promotion writer landed, and a MERGE only ever ADDS. So a
record whose project changed, or whose edge was written from a spelling that has
since been merged, keeps the old edge alongside the new one — and "which project
is this record in?" gets two answers depending on whether you ask Postgres or
count edges. Measured on the corpus this was written for: 6 facts whose edge
named a different project than Postgres, and 4 spine nodes carrying two edges at
once.

THE DIRECTION OF REPAIR IS NOT SYMMETRIC. This tool rewrites the GRAPH to match
Postgres, never the reverse. Postgres is where the value was asserted, validated
at ingress against the registry, and where the resolution is defined; the edge is
a projection of it. A tool that "reconciled" by copying an edge back into
metadata would launder a graph-side accident into an asserted fact.

PARKED RECORDS ARE LEFT ALONE, DELIBERATELY. A record with no resolvable project
that nonetheless carries an edge is not repaired here: it is the PROMOTION path's
population, and the promotion writer replaces the edge as part of establishing
the real value. Clearing the edge first would be a second write to the same
property for no gain, and it would destroy the only surviving hint about where
that record belongs before anything has been decided about it.

**It enqueues outbox rows; it never writes Neo4j.** The gateway's outbox worker
applies them, so outbox atomicity holds and a partial run leaves durable work
rather than half a graph.

⚠ The rows it enqueues REPLACE the record's project edge, which only the worker
from v0.8.36 does. An older worker still recognises the row type and MERGEs —
not destructive, but it would leave the stale edge in place and report success,
which is the failure mode this whole file exists to remove. Hence the version
guard, which FAILS CLOSED.

Dry-run by default. Idempotent: a re-run after the worker drains finds nothing.

    python reconcile_project_edges.py                 # report only
    python reconcile_project_edges.py --apply         # enqueue
"""
import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

import psycopg2
from neo4j import GraphDatabase

# The first version whose outbox worker REPLACES rather than merges the edge.
MIN_GATEWAY_VERSION = (0, 8, 36)
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8888")


def _load_env() -> None:
    """The framework env is shared-memory/.env; the repo root is the fallback.
    Same candidate order as apply.py — a standalone CLI in a fresh shell has no
    PG_PASSWORD otherwise, and reading the wrong file fails with an empty
    password rather than a missing one."""
    here = Path(__file__).resolve().parent
    env_path = next((p for p in (here.parent / ".env", here.parent.parent / ".env")
                     if p.exists()), None)
    if env_path is None:
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


_load_env()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ontology import ONT  # noqa: E402
from project_axis import PROJECT_SQL  # noqa: E402
from project_promotion import is_parked  # noqa: E402

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_AUTH = (os.environ.get("NEO4J_USER", "neo4j"),
              os.environ.get("NEO4J_PASSWORD", ""))
PG_CONN = (
    f"postgresql://{os.environ.get('PG_USER', 'postgres')}:"
    f"{os.environ.get('PG_PASSWORD', '')}@{os.environ.get('PG_HOST', 'localhost')}:"
    f"{os.environ.get('PG_PORT', '5432')}/{os.environ.get('PG_DATABASE', 'agent_data')}"
)


def gateway_version() -> tuple[str, tuple[int, ...] | None]:
    """(raw, parsed) version of the RUNNING gateway; parsed is None whenever the
    answer is not knowable — unreachable, refused, or unparseable."""
    try:
        with urllib.request.urlopen(f"{GATEWAY_URL}/health", timeout=10) as r:
            raw = json.load(r).get("version", "")
    except Exception as exc:
        return f"unreachable ({exc})", None
    try:
        return raw, tuple(int(p) for p in raw.split(".")[:3])
    except ValueError:
        return raw, None


def gateway_replaces_project_edges(parsed) -> bool:
    """Whether the running gateway REPLACES the edge. Fails closed: an unknown
    version is not permission to enqueue."""
    return parsed is not None and tuple(parsed) >= MIN_GATEWAY_VERSION


def graph_project_edges(driver) -> dict[int, list[str]]:
    """pg_id → the project names its spine node points at.

    ⚠ Keyed per LABEL and merged deliberately: pg_id is unique per TABLE, not
    across the graph, so a :CommunitySummary can carry the same pg_id as a
    :Fact. Restricting to the spine is what keeps this comparison against
    technical_docs honest — an earlier measurement that swept every node with a
    pg_id silently collided summaries with facts and mis-stated the drift.
    """
    out: dict[int, list[str]] = {}
    with driver.session() as session:
        for label in (ONT.fact, ONT.decision, ONT.retrospective):
            for rec in session.run(
                f"MATCH (n:{label}) WHERE n.pg_id IS NOT NULL"
                f" RETURN n.pg_id AS pg_id,"
                f"        [(n)-[:{ONT.project_of}]->(p) | p.name] AS projects"
            ):
                out.setdefault(int(rec["pg_id"]), []).extend(rec["projects"])
    return out


def postgres_projects(conn) -> dict[int, str | None]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT id, {PROJECT_SQL} FROM technical_docs")
        return {r[0]: r[1] for r in cur.fetchall()}


def already_queued(conn, pg_ids: list[int]) -> set[int]:
    if not pg_ids:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT pg_id FROM neo4j_outbox"
            " WHERE cypher_params->>'type' = 'project_of' AND pg_id = ANY(%s)",
            (pg_ids,),
        )
        return {r[0] for r in cur.fetchall()}


def find_drift(pg: dict, graph: dict, pending: set) -> dict[int, tuple[list[str], str]]:
    """pg_id → (current edges, the project they should be).

    REPAIRING A DISAGREEMENT IS NOT THE SAME ACT AS FILLING AN ABSENCE, and this
    tool only does the first. A record carrying an edge that contradicts Postgres
    is stating something false and there is nothing to decide; a record carrying
    NO edge is stating nothing, and whether it should is a question about that
    record type — for facts it was answered by backfill_project_of.py, and for
    retrospectives it is still open, because whether a retrospective's project is
    its own or its decision's has not been settled.

    Conflating them is not academic: on the corpus this was written for, 161 of
    167 "drifted" records were edgeless retrospectives, so a tool that treated
    absence as drift would have quietly created 161 edges — a substantial graph
    change wearing the label of a repair.

    So: a candidate has a REAL project AND at least one existing edge. Two shapes
    qualify — the edge names something else, or there is more than one.
    """
    drift = {}
    for pg_id, edges in graph.items():
        if pg_id in pending or pg_id not in pg:
            continue
        project = pg[pg_id]
        if is_parked(project) or not edges:
            continue
        if edges != [project]:
            drift[pg_id] = (edges, project)
    return drift


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="enqueue the outbox rows (default: report only)")
    args = ap.parse_args()

    if not NEO4J_AUTH[1]:
        print("NEO4J_PASSWORD is not set.", file=sys.stderr)
        return 2

    driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
    conn = psycopg2.connect(PG_CONN, connect_timeout=5)
    try:
        graph = graph_project_edges(driver)
        pg = postgres_projects(conn)
        pending = already_queued(conn, list(graph))
        drift = find_drift(pg, graph, pending)

        parked_with_edge = [
            i for i, e in graph.items()
            if i in pg and is_parked(pg[i]) and e
        ]
        edgeless = [
            i for i, e in graph.items()
            if i in pg and not is_parked(pg[i]) and not e
        ]

        print(f"Spine nodes with a pg_id            : {len(graph)}")
        print(f"  already queued for repair         : {len(pending)}")
        print(f"  PARKED but carrying an edge       : {len(parked_with_edge)}"
              f"   (the promotion path's, left alone)")
        print(f"  real project but NO edge          : {len(edgeless)}"
              f"   (an absence, not a disagreement — NOT repaired here)")
        print(f"  DRIFTED — graph disagrees with PG : {len(drift)}")
        for pg_id, (edges, project) in sorted(drift.items()):
            shape = "extra edge" if project in edges else "wrong project"
            print(f"      pg_id {pg_id:>5}: {edges} → ['{project}']   ({shape})")

        if not args.apply:
            print("\nDry run — nothing enqueued. Re-run with --apply.")
            return 0
        if not drift:
            print("\nNothing to do.")
            return 0

        raw, parsed = gateway_version()
        if not gateway_replaces_project_edges(parsed):
            need = ".".join(str(p) for p in MIN_GATEWAY_VERSION)
            print(f"\nREFUSING to enqueue: the running gateway reports {raw!r}, and "
                  f"these rows only REPLACE the edge from {need}.\n"
                  f"An older worker would merge the new edge beside the stale one "
                  f"and report success — leaving exactly the drift this repairs.\n"
                  f"Deploy first (restart the gateway on this code), then re-run.",
                  file=sys.stderr)
            return 3
        print(f"\nRunning gateway is {raw} — replaces project edges.")

        with conn.cursor() as cur:
            for pg_id, (_edges, project) in sorted(drift.items()):
                cur.execute(
                    "INSERT INTO neo4j_outbox (pg_id, cypher_params, status)"
                    " VALUES (%s, %s, 'pending')",
                    (pg_id, json.dumps({"type": "project_of", "project": project})),
                )
        conn.commit()
        print(f"\nEnqueued {len(drift)} project_of row(s). The gateway's outbox "
              f"worker applies them; each row is deleted on success.")
        return 0
    finally:
        conn.close()
        driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
