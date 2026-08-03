#!/usr/bin/env python3
"""One-time project-name normalisation (decision pg_id 276).

Canonical project names equal the project folder name. This script merges
legacy free-text spellings into the canonical name in BOTH stores:

  * Postgres ``technical_docs``: rewrites ``metadata->>'project'`` and
    ``metadata->'decision'->>'project'``.
  * Neo4j: rewires every relationship from the alias ``:Project`` node to the
    canonical node (creating it if needed), then deletes the alias node.

Run it on the gateway host, once per alias map change; the coordinator's
``PROJECT_ALIASES`` env keeps future writes canonical. Idempotent — re-running
with the same map is a no-op.

Usage:
    uv run --with psycopg2-binary --with neo4j python \\
        shared-memory/scripts/normalize_projects.py \\
        --map "shared_memory=shared-memory-GitHub,shared-memory=shared-memory-GitHub" \\
        [--dry-run]

With no --map, the PROJECT_ALIASES environment variable is used.
"""

import argparse
import os
import sys
from pathlib import Path

import psycopg2
from neo4j import GraphDatabase

sys.path.insert(0, os.path.dirname(__file__))
from ontology import ONT  # noqa: E402
from project_axis import PROJECT_MATCH_SQL  # noqa: E402


def _load_env() -> None:
    """Populate credentials from the framework .env — this is a standalone CLI
    run in a fresh shell where PG_PASSWORD/NEO4J_PASSWORD are otherwise unset
    (credentials are read from .env, never hardcoded).

    ⚠ THE FRAMEWORK ENV IS ``shared-memory/.env``; the repo root is the
    FALLBACK. This used to read the repo-root path only, and its docstring
    claimed that matched ``migrations/apply.py`` — which tries the framework
    path first. On an install that keeps credentials only where the documented
    setup puts them, this script therefore connected with an empty password and
    died on `fe_sendauth: no password supplied`: a shipped tool that could not
    run at all, reported by nothing until someone tried to use it. Same
    candidate order as apply.py, so there is one answer to "where does the
    password live" rather than one per script.
    """
    here = Path(__file__).resolve().parent
    candidates = [here.parent / ".env", here.parent.parent / ".env"]
    env_path = next((p for p in candidates if p.exists()), None)
    if env_path is None:
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


_load_env()

_pg_pass = os.environ.get("PG_PASSWORD", "")
PG_CONN = os.environ.get(
    "PG_CONN", f"postgresql://postgres:{_pg_pass}@localhost:5432/agent_data"
)
# Env-overridable, never a baked-in literal: our bolt port is one valid
# configuration, not the configuration. Matches backfill_project_of.py.
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_AUTH = (os.environ.get("NEO4J_USER", "neo4j"),
              os.environ.get("NEO4J_PASSWORD", ""))


def parse_alias_map(raw: str) -> dict:
    aliases = {}
    for pair in raw.split(","):
        old, sep, new = pair.partition("=")
        if sep and old.strip() and new.strip():
            aliases[old.strip()] = new.strip()
    return aliases


def normalize_postgres(conn, aliases: dict, dry_run: bool) -> None:
    with conn.cursor() as cur:
        for old, new in aliases.items():
            # PROJECT_MATCH_SQL, deliberately NOT project_axis.PROJECT_SQL: this
            # counts rows that need REWRITING, and a row carrying the old name in
            # the decision blob and the new one at the top level still does. The
            # resolution's COALESCE would shadow that row and under-reach. The
            # two UPDATEs below stay per-field for the same reason.
            cur.execute(
                "SELECT count(*) FROM technical_docs"
                f" WHERE {PROJECT_MATCH_SQL.format(p='%s')}",
                (old, old),
            )
            n = cur.fetchone()[0]
            print(f"  postgres: '{old}' → '{new}': {n} row(s)")
            if dry_run or not n:
                continue
            cur.execute(
                "UPDATE technical_docs"
                "   SET metadata = jsonb_set(metadata, '{project}', to_jsonb(%s::text))"
                " WHERE metadata->>'project' = %s",
                (new, old),
            )
            cur.execute(
                "UPDATE technical_docs"
                "   SET metadata = jsonb_set(metadata, '{decision,project}', to_jsonb(%s::text))"
                " WHERE metadata->'decision'->>'project' = %s",
                (new, old),
            )
    if not dry_run:
        conn.commit()


def record_aliases(conn, aliases: dict, dry_run: bool) -> None:
    """Remember each rename, so it never has to be decided twice.

    Rewriting the records is only half a rename. Without this the retired name
    comes back the moment a machine that still uses it saves again, and it reads
    as an unregistered stranger to every later review.

    ⚠ THE ORDER HERE IS THE INVARIANT (A3 then A1), not a preference:

      1. Re-point every alias already aimed at the old name. This is what keeps
         resolution ONE HOP: a chain a→b→c collapses to a→c and b→c at write
         time, so ingress never walks links and can never meet a cycle.
      2. DELETE the old registry row before interning it as an alias. A1 says a
         string is never both a registered project and an alias, and the
         database enforces it with a trigger — so inserting first would simply
         be refused.
      3. Only then map old → new.

    A registry row that cannot be removed (something still references it) is
    REPORTED and skipped, never forced: a rename that has to break a reference
    is not a rename, it is a different decision.
    """
    with conn.cursor() as cur:
        for old, new in aliases.items():
            if old == new:
                continue
            cur.execute("SELECT 1 FROM projects WHERE name = %s", (new,))
            if cur.fetchone() is None:
                print(f"  alias: SKIPPED {old!r} → {new!r} — {new!r} is not registered")
                continue
            if dry_run:
                print(f"  alias: would record {old!r} → {new!r}")
                continue
            try:
                cur.execute(
                    "UPDATE project_aliases SET project = %s"
                    " WHERE project = %s AND active",
                    (new, old),
                )
                repointed = cur.rowcount
                cur.execute("DELETE FROM projects WHERE name = %s", (old,))
                cur.execute(
                    "INSERT INTO aliases (name) VALUES (%s)"
                    " ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name"
                    " RETURNING id",
                    (old,),
                )
                alias_id = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO project_aliases (alias_id, project, reason, created_by)"
                    " VALUES (%s, %s, %s, 'normalize_projects')"
                    " ON CONFLICT DO NOTHING",
                    (alias_id, new, f"renamed to {new}"),
                )
                conn.commit()
                extra = f", {repointed} existing alias(es) re-pointed" if repointed else ""
                print(f"  alias: recorded {old!r} → {new!r}{extra}")
            except Exception as exc:
                conn.rollback()
                print(f"  alias: FAILED for {old!r} → {new!r} — {exc}")


def normalize_neo4j(driver, aliases: dict, dry_run: bool) -> None:
    with driver.session() as session:
        for old, new in aliases.items():
            count = session.run(
                f"MATCH (p:{ONT.project} {{name: $old}})<-[r]-() RETURN count(r) AS n",
                old=old,
            ).single()["n"]
            print(f"  neo4j: Project '{old}' → '{new}': {count} inbound edge(s)")
            if dry_run:
                continue
            # Rewire PROJECT_OF edges (the only inbound type the ontology
            # writes to Project nodes) to the canonical node. MERGE keeps the
            # rewiring idempotent.
            session.run(
                f"MATCH (alias:{ONT.project} {{name: $old}})"
                f" MERGE (canon:{ONT.project} {{name: $new}})"
                f" WITH alias, canon"
                f" MATCH (n)-[r:{ONT.project_of}]->(alias)"
                f" MERGE (n)-[:{ONT.project_of}]->(canon)"
                f" DELETE r",
                old=old, new=new,
            )
            # Drop the alias node only when nothing else points at it — an
            # unexpected edge type means a manual look, not a silent delete.
            leftover = session.run(
                f"MATCH (alias:{ONT.project} {{name: $old}})-[r]-()"
                f" RETURN count(r) AS n",
                old=old,
            ).single()
            if leftover and leftover["n"]:
                print(f"  neo4j: '{old}' kept — {leftover['n']} unexpected edge(s); inspect manually.")
            else:
                session.run(
                    f"MATCH (alias:{ONT.project} {{name: $old}}) DELETE alias",
                    old=old,
                )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--map", default=os.environ.get("PROJECT_ALIASES", ""),
                    help="comma-separated old=new pairs (default: $PROJECT_ALIASES)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report affected rows/edges without writing")
    args = ap.parse_args()

    aliases = parse_alias_map(args.map)
    if not aliases:
        sys.exit("No alias map — pass --map or set PROJECT_ALIASES.")

    print(f"Normalising {len(aliases)} project alias(es)"
          + (" [DRY RUN]" if args.dry_run else "") + ":")

    conn = psycopg2.connect(PG_CONN, connect_timeout=5)
    try:
        normalize_postgres(conn, aliases, args.dry_run)
        # Applying a rename and remembering it are one act, not two.
        record_aliases(conn, aliases, args.dry_run)
    finally:
        conn.close()

    driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
    try:
        normalize_neo4j(driver, aliases, args.dry_run)
    finally:
        driver.close()

    print("Done." if not args.dry_run else "Dry run complete — nothing written.")


if __name__ == "__main__":
    main()
