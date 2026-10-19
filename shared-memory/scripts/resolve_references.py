#!/usr/bin/env python3
"""Operator-run backfill: write textual record citations as REFERENCES edges.

Scans the content of every non-superseded record for a cited record id ("refines decision
381", "addendum to pg_id 257"; reference_resolver.extract_references) and writes
(citing)-[:REFERENCES]->(cited) in Neo4j, stamped resolved_from='content' with the cue.

Every citation is REFERENCES, whatever the two record types (decision:3135: a citation
found in text is not grounding). ⛔ Never INFORMED_BY: the insight gate and the section walk
count that edge as grounding, and only the operator grounds a decision. --apply also re-types
the INFORMED_BY edges earlier versions of this script wrote (Decision→Decision, stamped
resolved_from='content') to REFERENCES, marking each retyped_from='INFORMED_BY'. ⛔ An edge
that also carries asserted_by is left alone: the grounding writer merges an operator's
--informed-by onto an existing INFORMED_BY, so that edge is the operator's grounding too.

REFERENCES joins facts and decisions only (decision:474), so a citation with a retrospective
at either end is counted and skipped. A cited id must be a non-superseded record. A pair
already joined by SUPERSEDES is skipped; SUPERSEDES itself is explicit-only (`--supersedes`).
Every write is one MERGE in one transaction, so a re-run changes nothing and a failed run
writes nothing.

Dry-run is the DEFAULT. Nothing is written unless you pass --apply.

Usage (gateway host):
    uv run --with psycopg2-binary --with neo4j \
        python shared-memory/scripts/resolve_references.py [--apply]
"""
import argparse
import os
import sys

import psycopg2
from neo4j import GraphDatabase

sys.path.insert(0, os.path.dirname(__file__))
from ontology import ONT, record_label_for_type  # noqa: E402
import reference_resolver as rr  # noqa: E402
import secure_env  # noqa: E402

_ENDPOINT_LABELS = (ONT.fact, ONT.decision)

_LEGACY = (
    f"MATCH (s:{ONT.decision})-[old:{ONT.informed_by}]->(t:{ONT.decision}) "
    "WHERE old.resolved_from = 'content' AND old.asserted_by IS NULL "
)
_COUNT_LEGACY = _LEGACY + "RETURN count(old) AS n"
_RETYPE_LEGACY = (
    _LEGACY
    + f"MERGE (s)-[r:{ONT.references}]->(t) "
    "SET r.resolved_from = coalesce(r.resolved_from, 'content'), "
    "    r.cue = coalesce(r.cue, old.cue), r.created_at = coalesce(r.created_at, old.created_at), "
    f"    r.retyped_from = '{ONT.informed_by}' "
    "DELETE old"
)
_WRITE = (
    "UNWIND $rows AS row "
    f"MATCH (s) WHERE s.pg_id = row.s AND (s:{ONT.fact} OR s:{ONT.decision}) "
    f"MATCH (t) WHERE t.pg_id = row.t AND (t:{ONT.fact} OR t:{ONT.decision}) "
    f"WITH s, t, row WHERE NOT (s)-[:{ONT.supersedes}]-(t) "
    f"MERGE (s)-[r:{ONT.references}]->(t) "
    "ON CREATE SET r.resolved_from = 'content', r.cue = row.cue, r.created_at = datetime() "
    "RETURN count(r) AS matched"
)


def plan_edges(rows):
    """(plan, skipped) from (id, type, content) rows. Pure — no I/O.

    plan is [(citing, cited, cue)] for fact/decision pairs; skipped counts citations with a
    retrospective at either end, which REFERENCES cannot join (decision:474).
    """
    label_by_id = {pg_id: record_label_for_type(rtype) for pg_id, rtype, _ in rows}
    plan, skipped = [], 0
    for sid, _, content in rows:
        for ref, cue, _snippet in rr.extract_references(content, sid, label_by_id):
            if label_by_id[sid] in _ENDPOINT_LABELS and label_by_id[ref] in _ENDPOINT_LABELS:
                plan.append((sid, ref, cue))
            else:
                skipped += 1
    return plan, skipped


def _read_records(pg_pw):
    conn = psycopg2.connect(
        host=os.environ.get("PG_HOST", "localhost"),
        port=os.environ.get("PG_PORT", "5432"),
        user=os.environ.get("PG_USER", "postgres"),
        password=pg_pw,
        dbname=os.environ.get("PG_DATABASE", "agent_data"),
    )
    try:
        with conn.cursor() as cur:
            # One statement, so the type map and the scanned content are the same snapshot.
            cur.execute("SELECT id, metadata->>'type', content FROM technical_docs "
                        "WHERE superseded = false")
            return cur.fetchall()
    finally:
        conn.close()


def _write(tx, rows):
    retyped = tx.run(_RETYPE_LEGACY).consume().counters.relationships_deleted
    result = tx.run(_WRITE, rows=rows)
    matched = result.single()["matched"]
    created = result.consume().counters.relationships_created
    return retyped, matched, created


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write edges (default: dry-run)")
    args = ap.parse_args()
    # Secrets stay in secure_env's in-process store; read them with get_secret(), never os.environ.
    secure_env.load_split_env()

    pg_pw = secure_env.get_secret("PG_PASSWORD", "")
    neo_pw = secure_env.get_secret("NEO4J_PASSWORD", "")
    if not pg_pw or not neo_pw:
        print("ERROR: PG_PASSWORD / NEO4J_PASSWORD not set (shared-memory/.env).", file=sys.stderr)
        return 2

    plan, skipped = plan_edges(_read_records(pg_pw))
    print(f"{len(plan)} citation(s) to write as {ONT.references}; "
          f"{skipped} skipped (a retrospective at one end).")
    for sid, ref, cue in plan:
        print(f"  {sid} -[:{ONT.references}]-> {ref}  cue={cue!r}")

    driver = GraphDatabase.driver(os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
                                  auth=(os.environ.get("NEO4J_USER", "neo4j"), neo_pw))
    try:
        with driver.session() as session:
            if not args.apply:
                legacy = session.run(_COUNT_LEGACY).single()["n"]
                print(f"{legacy} {ONT.informed_by} edge(s) written by an earlier version "
                      f"would be re-typed to {ONT.references}.")
                print("\nDRY-RUN: nothing written. Re-run with --apply to write.")
                return 0
            rows = [{"s": s, "t": t, "cue": cue} for s, t, cue in plan]
            retyped, matched, created = session.execute_write(_write, rows)
    finally:
        driver.close()
    print(f"\nAPPLIED: re-typed {retyped} {ONT.informed_by} edge(s); "
          f"{created} new {ONT.references} edge(s), {matched - created} already present, "
          f"{len(plan) - matched} not written (endpoint not in the graph, or a SUPERSEDES pair).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
