#!/usr/bin/env python3
"""Backfill the ``PROJECT_OF`` edge for facts written before it existed.

The edge has only been written since the fact-provenance work; every fact
stored before that carries its project in Postgres metadata and nothing in the
graph. Nothing graph-side can be gated on the project axis while that gap is
open, which is why this runs as a PREREQUISITE of the release that starts
gating on it — not as a follow-up.

**It enqueues outbox rows; it never writes Neo4j.** The gateway's outbox worker
applies them, so outbox atomicity holds and a partial run leaves durable work
rather than half a graph.

⚠ It enqueues a NARROW ``project_of`` row, not an ordinary fact row. Replaying a
fact row would also re-run that row's ``MENTIONS`` merges and resurrect every
enrichment edge a later sweep deliberately deleted. A repair must touch only
what it repairs.

Facts whose project does not resolve are **left alone**: they have no project to
backfill, and inventing one is the bucket this whole line of work exists to
remove. They are the repair path's population, not this script's.

⚠⚠ **The gateway must already be running the code that HANDLES this row type.**
An older worker does not recognise ``project_of`` and falls through to its
ordinary fact branch, which runs ``SET f.content = $content`` with the content
this row does not carry — blanking the content of every fact it touches. That is
silent, graph-side data loss, so the version check below is a GUARD, not a
convenience: enqueue only after the deploy, never before.

Dry-run by default. Idempotent: re-running enqueues nothing for facts that
already have the edge, and skips any fact with a row already pending.

    python backfill_project_of.py                 # report only
    python backfill_project_of.py --apply         # enqueue
"""
import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

import psycopg2
from neo4j import GraphDatabase

# The first version whose outbox worker handles a 'project_of' row.
MIN_GATEWAY_VERSION = (0, 8, 32)
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8888")


def gateway_version() -> tuple[str, tuple[int, ...] | None]:
    """(raw, parsed) version of the RUNNING gateway. parsed is None whenever the
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


def gateway_handles_project_of(parsed) -> bool:
    """Whether it is SAFE to enqueue against the running gateway.

    Fails closed: an unknown version is not permission to write. Getting this
    backwards is not a missed optimisation — it blanks fact content.
    """
    return parsed is not None and tuple(parsed) >= MIN_GATEWAY_VERSION

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ontology import ONT  # noqa: E402
from project_axis import PROJECT_SQL  # noqa: E402
import secure_env  # noqa: E402


def _load_env() -> None:
    # Item 9(a), fix round 1: this was the one sibling of the twelve-script
    # SEC-05 sweep left teaching the deprecated pattern (Opus O4) — it read
    # NEO4J_PASSWORD/PG_PASSWORD directly from a bare os.environ with NO
    # .env-parsing loader of its own at all, so its only working invocation
    # was an already-exported-secret shell (never the file-based delivery
    # its eleven siblings gained in this same PR). Delegates to secure_env's
    # split loader — same shared-memory/.env-first, repo-root-fallback
    # candidate order as every sibling, and now gets $CREDENTIALS_DIRECTORY/
    # <KEY>_FILE for free too.
    secure_env.load_split_env()


_load_env()

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASS = secure_env.get_secret("NEO4J_PASSWORD", "")
PG_CONN = (
    f"postgresql://{os.environ.get('PG_USER', 'postgres')}:"
    f"{secure_env.get_secret('PG_PASSWORD', '')}@{os.environ.get('PG_HOST', 'localhost')}:"
    f"{os.environ.get('PG_PORT', '5432')}/{os.environ.get('PG_DATABASE', 'agent_data')}"
)


def edgeless_fact_pg_ids(driver) -> tuple[list[int], int]:
    """pg_ids of :Fact nodes with no PROJECT_OF edge, plus how many such nodes
    carry no pg_id at all (inert — nothing in Postgres to resolve them from)."""
    with driver.session() as session:
        rows = session.run(
            f"MATCH (f:{ONT.fact})"
            f" WHERE NOT (f)-[:{ONT.project_of}]->()"
            f" RETURN collect(f.pg_id) AS ids, count(f) AS total"
        ).data()[0]
    ids = [int(i) for i in rows["ids"] if i is not None]
    return ids, rows["total"] - len(ids)


def resolve(conn, pg_ids: list[int]) -> dict[int, str | None]:
    if not pg_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT id, {PROJECT_SQL} FROM technical_docs WHERE id = ANY(%s)",
            (pg_ids,),
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def already_queued(conn, pg_ids: list[int]) -> set[int]:
    """pg_ids with a project_of row still pending — so a re-run before the
    worker drains does not enqueue the same repair twice."""
    if not pg_ids:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT pg_id FROM neo4j_outbox"
            " WHERE cypher_params->>'type' = 'project_of' AND pg_id = ANY(%s)",
            (pg_ids,),
        )
        return {r[0] for r in cur.fetchall()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="enqueue the outbox rows (default: report only)")
    args = ap.parse_args()

    if not NEO4J_PASS:
        print("NEO4J_PASSWORD is not set.", file=sys.stderr)
        return 2

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    conn = psycopg2.connect(PG_CONN, connect_timeout=5)
    try:
        ids, no_pg_id = edgeless_fact_pg_ids(driver)
        resolved = resolve(conn, ids)
        pending = already_queued(conn, ids)

        targets = {p: v for p, v in resolved.items()
                   if isinstance(v, str) and v.strip() and p not in pending}
        unresolvable = [p for p, v in resolved.items()
                        if not (isinstance(v, str) and v.strip())]
        missing = [p for p in ids if p not in resolved]

        print(f"Facts with no {ONT.project_of} edge : {len(ids) + no_pg_id}")
        print(f"  carrying no pg_id (inert)        : {no_pg_id}")
        print(f"  absent from Postgres             : {len(missing)}")
        print(f"  already queued for repair        : {len(pending)}")
        print(f"  no resolvable project (left for the repair path): {len(unresolvable)}")
        print(f"  TO BACKFILL                      : {len(targets)}")
        by_project: dict[str, int] = {}
        for v in targets.values():
            by_project[v] = by_project.get(v, 0) + 1
        for name, n in sorted(by_project.items(), key=lambda kv: -kv[1]):
            print(f"      {n:>4}  {name}")

        if not args.apply:
            print("\nDry run — nothing enqueued. Re-run with --apply.")
            return 0
        if not targets:
            print("\nNothing to do.")
            return 0

        # GUARD, not a courtesy — see the module docstring.
        raw, parsed = gateway_version()
        if not gateway_handles_project_of(parsed):
            need = ".".join(str(p) for p in MIN_GATEWAY_VERSION)
            print(f"\nREFUSING to enqueue: the running gateway reports {raw!r}, and "
                  f"these rows are only handled from {need}.\n"
                  f"An older worker would fall through to its ordinary fact branch "
                  f"and BLANK the content of every fact it touched.\n"
                  f"Deploy first (restart the gateway on this code), then re-run.",
                  file=sys.stderr)
            return 3
        print(f"\nRunning gateway is {raw} — handles these rows.")

        with conn.cursor() as cur:
            for pg_id, project in sorted(targets.items()):
                cur.execute(
                    "INSERT INTO neo4j_outbox (pg_id, cypher_params, status)"
                    " VALUES (%s, %s, 'pending')",
                    (pg_id, json.dumps({"type": "project_of", "project": project})),
                )
        conn.commit()
        print(f"\nEnqueued {len(targets)} project_of row(s). The gateway's outbox "
              f"worker applies them; each row is deleted on success.")
        return 0
    finally:
        conn.close()
        driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
