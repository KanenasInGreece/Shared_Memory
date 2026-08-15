#!/usr/bin/env python3
"""One-time cleanup of noise Entity nodes (Phase 1 inbound hygiene).

The outbox->graph and REM gates (``ontology.sanitize_entity_name``) stop NEW
noise names from becoming Entity hubs. This script removes the noise that entered
the graph BEFORE the gates existed — using the gate's own definition of noise, so
prevention and cleanup can never drift: an Entity is junk iff
``sanitize_entity_name(name)`` rejects it (numeric-only leaked pg-ids, single
characters, booleans/placeholders, schema vocabulary).

Scope is deliberately narrow: only ``:Entity`` nodes, only the noise names, only
the graph (Neo4j). Postgres Tier-1 facts are the source of truth and are left
untouched — a fact that genuinely mentioned "256" keeps its content and metadata;
we only drop the meaningless graph hub and its edges.

Dry-run is the DEFAULT. Nothing is deleted unless you pass ``--apply``.

Usage (on the gateway host):
    uv run --with neo4j python shared-memory/scripts/cleanup_entity_noise.py            # preview
    uv run --with neo4j python shared-memory/scripts/cleanup_entity_noise.py --apply    # delete
"""
import argparse
import os
import sys
from pathlib import Path

from neo4j import GraphDatabase

sys.path.insert(0, os.path.dirname(__file__))
from ontology import sanitize_entity_name, ONT  # noqa: E402
import secure_env  # noqa: E402

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"


def _load_env() -> None:
    """Delegates to secure_env's split loader (Credential_Custody_Plan PR A4,
    SEC-05-class sweep) — same shared-memory/.env-first, repo-root-fallback
    candidate order as the gateway's own loader, but NEO4J_PASSWORD lands in
    secure_env's in-process store, never os.environ; read it back via
    secure_env.get_secret()."""
    secure_env.load_split_env()


def find_noise(session) -> list[dict]:
    """Return noise Entity nodes with degree and a sample of neighbours."""
    rows = session.run(
        f"MATCH (e:{ONT.entity}) "
        f"RETURN e.name AS name, labels(e) AS labels, "
        f"       COUNT {{ (e)--() }} AS degree"
    )
    noise = []
    for r in rows:
        name = r["name"]
        if sanitize_entity_name(name) is None:
            noise.append({"name": name, "labels": r["labels"], "degree": r["degree"]})
    noise.sort(key=lambda d: (-d["degree"], str(d["name"])))
    return noise


def delete_noise(session, names: list[str]) -> int:
    """DETACH DELETE the named noise Entity nodes. Returns count removed."""
    rec = session.run(
        f"MATCH (e:{ONT.entity}) WHERE e.name IN $names "
        f"DETACH DELETE e RETURN count(*) AS removed",
        names=names,
    ).single()
    return int(rec["removed"]) if rec else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default is a dry-run preview)")
    args = ap.parse_args()

    _load_env()
    password = secure_env.get_secret("NEO4J_PASSWORD", "")
    if not password:
        print("ERROR: NEO4J_PASSWORD not set (shared-memory/.env).", file=sys.stderr)
        return 2

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, password))
    try:
        with driver.session() as session:
            noise = find_noise(session)
            if not noise:
                print("No noise Entity nodes found — graph is clean.")
                return 0

            print(f"Found {len(noise)} noise Entity node(s) "
                  f"(rejected by sanitize_entity_name):\n")
            for n in noise:
                extra = "" if n["labels"] == [ONT.entity] else f"  labels={n['labels']}"
                print(f"  {n['name']!r:<28} degree={n['degree']}{extra}")

            if not args.apply:
                print(f"\nDRY-RUN: nothing deleted. Re-run with --apply to remove "
                      f"these {len(noise)} node(s) and their edges.")
                return 0

            removed = delete_noise(session, [n["name"] for n in noise])
            print(f"\nAPPLIED: DETACH DELETE removed {removed} noise Entity node(s).")
            return 0
    finally:
        driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
