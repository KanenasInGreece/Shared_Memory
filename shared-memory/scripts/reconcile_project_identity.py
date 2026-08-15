#!/usr/bin/env python3
"""Stamp every graph project node with its registry IDENTITY (migration 027).

WHY A TOOL AND NOT A MIGRATION. Migration 027 gives each registry row an ``id``
and moves both referencing tables onto it. It cannot touch Neo4j — a Postgres
migration has no reach into the graph — so the `:Project` nodes keep pointing at
nothing until something stamps them. This is that something.

WHY NOT AT GATEWAY STARTUP. Making the gateway mutate the graph as it boots adds
a second continuous writer to a store whose write path is deliberately a single
outbox worker, races that worker on the first save of a cold start, and leaves a
half-stamped graph behind any partial start. An upgrade step is explicit,
re-runnable and observable; a boot side effect is none of those.

WHY NOT ONLY STAMP-ON-WRITE. The write path does stamp: from 027 the coordinator
keys the node on the identity when it knows one. That heals every project that
sees traffic and NEVER heals a quiet one, and a quiet project is precisely the
one whose decisions sit unfolded waiting for a second project to join them. So
the sweep is the completion and the write path is the maintenance.

WHAT IT DOES, AND WHAT IT REFUSES TO DO. It matches a graph node to a registry
row BY NAME — the one moment where the name is still the bridge, which is why
this runs once per deployment rather than becoming a resolution path — and sets
``project_id``. It NEVER creates a node, never deletes one, and never invents a
registry row: a node whose name is in no registry is REPORTED and left exactly
as it is, because deciding what an unregistered project node means is an
operator's judgement about their own corpus, not a script's.

The constraint that makes the identity unique is NOT created here. It is
declared in ``neo4j_init.cypher`` and applied by ``verify_neo4j_init.py
--apply``, which is the one writer of graph constraints; two scripts creating
constraints is how a schema acquires two answers.

UPGRADE ORDER — all three steps, in this order:

    uv run --with psycopg2-binary python shared-memory/migrations/apply.py
    uv run --with psycopg2-binary --with neo4j python \\
        shared-memory/scripts/reconcile_project_identity.py --apply
    uv run --with neo4j python shared-memory/migrations/verify_neo4j_init.py --apply

Until step 2 has run, ``GET /health`` reports the outstanding nodes under
``project_identity`` and the insight gate declines to count an unidentified
project toward its two-project rule — the upgrade is incomplete, visibly, rather
than silently wrong.

Read-only by default. ``--apply`` writes.
"""
import os
import sys
from pathlib import Path

import psycopg2
from neo4j import GraphDatabase

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import secure_env  # noqa: E402

# MATCH, never MERGE. This tool identifies nodes that already exist and must
# never be the thing that creates one: a name with no node is a project with no
# records yet, and minting an empty node for it would put a project into the
# graph that nothing belongs to. Held as a value so the property can be asserted
# directly — a guard commented out leaves its own text in the file, which is how
# two source-text tests in this repo passed against dead guards.
STAMP_CYPHER = (
    "UNWIND $rows AS row"
    " MATCH (p:Project {name: row.name})"
    " SET p.project_id = row.id"
    " RETURN count(p) AS n"
)


def _load_env() -> None:
    # Delegates to secure_env's split loader (Credential_Custody_Plan PR A4,
    # SEC-05-class sweep) — same shared-memory/.env-first, pre-0.6-root-fallback
    # candidate list three scripts once got wrong (died on "no password
    # supplied"), but PG_PASSWORD/NEO4J_PASSWORD now land in secure_env's
    # in-process store, never os.environ; read them back via
    # secure_env.get_secret().
    secure_env.load_split_env()


def _pg_dsn() -> str:
    return (
        f"postgresql://{os.environ.get('PG_USER', 'postgres')}:"
        f"{secure_env.get_secret('PG_PASSWORD', '')}@"
        f"{os.environ.get('PG_HOST', 'localhost')}:"
        f"{os.environ.get('PG_PORT', '5432')}/"
        f"{os.environ.get('PG_DATABASE', 'agent_data')}"
    )


def classify(registry: dict, nodes: list) -> dict:
    """Split the graph's project nodes against the registry. Pure.

    ``registry`` maps name → id; ``nodes`` is [{"name":…, "project_id":…}, …].
    Four buckets, each of which is a different conversation:

      ``correct``      already carries the id the registry gives its name
      ``to_stamp``     registered, carries no id — the population this fixes
      ``conflicting``  registered, carries a DIFFERENT id: a repair, and the one
                       case worth reading twice, because it means a node was
                       stamped against a registry that has since changed
      ``unregistered`` no registry row for that name — reported, never touched
    """
    out = {"correct": [], "to_stamp": [], "conflicting": [], "unregistered": []}
    for node in nodes:
        name = node.get("name")
        current = node.get("project_id")
        expected = registry.get(name)
        if expected is None:
            out["unregistered"].append(name)
        elif current is None:
            out["to_stamp"].append((name, expected))
        elif current == expected:
            out["correct"].append(name)
        else:
            out["conflicting"].append((name, current, expected))
    return out


def main() -> int:
    _load_env()
    apply = "--apply" in sys.argv[1:]

    conn = psycopg2.connect(_pg_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name, id FROM projects")
            registry = {r[0]: r[1] for r in cur.fetchall()}
    finally:
        conn.close()

    driver = GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"),
              secure_env.get_secret("NEO4J_PASSWORD", "")),
    )
    try:
        with driver.session() as session:
            nodes = [
                {"name": r["name"], "project_id": r["project_id"]}
                for r in session.run(
                    "MATCH (p:Project) RETURN p.name AS name,"
                    " p.project_id AS project_id"
                )
            ]
            buckets = classify(registry, nodes)

            print(f"registry rows: {len(registry)} · graph project nodes: {len(nodes)}")
            print(f"  already identified : {len(buckets['correct'])}")
            print(f"  to stamp           : {len(buckets['to_stamp'])}")
            print(f"  conflicting        : {len(buckets['conflicting'])}")
            print(f"  unregistered       : {len(buckets['unregistered'])}"
                  f" (reported only, never modified)")
            for name in buckets["unregistered"]:
                print(f"    ⚠ no registry row for graph project {name!r}"
                      f" — it will not count toward cross-project folds until"
                      f" it is registered or merged")
            for name, current, expected in buckets["conflicting"]:
                print(f"    ⚠ {name!r} carries id {current}, registry says {expected}")

            writes = buckets["to_stamp"] + [
                (n, e) for n, _c, e in buckets["conflicting"]
            ]
            if not writes:
                print("nothing to do — every registered project node is identified")
                return 0
            if not apply:
                print(f"DRY RUN — {len(writes)} node(s) would be stamped."
                      f" Re-run with --apply to write.")
                return 0

            written = session.run(
                STAMP_CYPHER,
                rows=[{"name": n, "id": i} for n, i in writes],
            ).single()["n"]
            print(f"stamped {written} project node(s) with their registry id")
            print("next: verify_neo4j_init.py --apply, to enforce uniqueness on it")
            return 0
    finally:
        driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
