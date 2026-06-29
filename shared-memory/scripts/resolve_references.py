#!/usr/bin/env python3
"""One-time reference-resolution backfill (Stage 1.2b).

Scans existing record content for textual cross-references (e.g. "refines decision
381", "addendum to pg_id 257"), resolves each to a real technical_docs id, and
materialises a record→record edge in Neo4j: Decision→Decision = INFORMED_BY,
otherwise REFERENCES (relationship type via reference_resolver.classify_relation,
which honours REFERENCE_JUDGE_MODE). Safe + idempotent:
  - MERGE (no duplicate edges); skips a pair that already has SUPERSEDES.
  - never creates SUPERSEDES (explicit-only).
  - both endpoints must exist as :Fact/:Decision nodes.

Dry-run is the DEFAULT. Nothing is written unless you pass --apply.
Going forward, REM applies the same resolver incrementally (Stage 1.3).

Usage (gateway host):
    uv run --with psycopg2-binary --with neo4j --with httpx \
        python shared-memory/scripts/resolve_references.py [--apply]
"""
import argparse
import os
import sys
from pathlib import Path

import psycopg2
from neo4j import GraphDatabase

sys.path.insert(0, os.path.dirname(__file__))
from ontology import ONT  # noqa: E402
import reference_resolver as rr  # noqa: E402

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"


def _load_env() -> None:
    here = Path(__file__).resolve()
    for cand in (here.parent.parent / ".env", here.parent.parent.parent / ".env"):
        if cand.exists():
            for line in cand.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
            return


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write edges (default: dry-run)")
    args = ap.parse_args()
    _load_env()

    pg_pw = os.environ.get("PG_PASSWORD", "")
    neo_pw = os.environ.get("NEO4J_PASSWORD", "")
    if not pg_pw or not neo_pw:
        print("ERROR: PG_PASSWORD / NEO4J_PASSWORD not set (shared-memory/.env).", file=sys.stderr)
        return 2

    conn = psycopg2.connect(
        f"postgresql://postgres:{pg_pw}@localhost:5432/agent_data")
    cur = conn.cursor()
    cur.execute("SELECT id, metadata->>'type' FROM technical_docs WHERE superseded = false")
    type_by_id = {r[0]: r[1] for r in cur.fetchall()}
    valid_ids = set(type_by_id)

    def label_of(pg_id):
        return ONT.decision if type_by_id.get(pg_id) == "decision" else ONT.fact

    cur.execute("SELECT id, content FROM technical_docs WHERE superseded = false")
    plan = []  # (src, tgt, rel, cue, snippet)
    import httpx
    client = httpx.Client(timeout=30.0) if rr.judge_enabled() else None
    try:
        for sid, content in cur.fetchall():
            for ref, cue, snippet in rr.extract_references(content, sid, valid_ids):
                rel = rr.classify_relation(label_of(sid), label_of(ref), snippet, client=client)
                plan.append((sid, ref, rel, cue, snippet))
    finally:
        if client:
            client.close()
    conn.close()

    if not plan:
        print("No resolvable references found.")
        return 0

    by_rel = {}
    for _, _, rel, _, _ in plan:
        by_rel[rel] = by_rel.get(rel, 0) + 1
    judge = "llm:" + rr._URL if rr.judge_enabled() else "deterministic"
    print(f"{len(plan)} resolvable reference(s); judgment={judge}; by type={by_rel}\n")
    for sid, ref, rel, cue, snippet in plan[:30]:
        print(f"  ({label_of(sid)} {sid}) -[:{rel}]-> ({label_of(ref)} {ref})  cue={cue!r}")
    if len(plan) > 30:
        print(f"  … and {len(plan) - 30} more")

    if not args.apply:
        print("\nDRY-RUN: nothing written. Re-run with --apply to create the edges.")
        return 0

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, neo_pw))
    created = 0
    try:
        with driver.session() as session:
            for sid, ref, rel, cue, snippet in plan:
                rec = session.run(
                    "MATCH (s) WHERE s.pg_id = $sid AND (s:" + ONT.fact + " OR s:" + ONT.decision + ") "
                    "MATCH (t) WHERE t.pg_id = $ref AND (t:" + ONT.fact + " OR t:" + ONT.decision + ") "
                    "WITH s, t WHERE NOT (s)-[:" + ONT.supersedes + "]-(t) "
                    "MERGE (s)-[r:" + rel + "]->(t) "
                    "ON CREATE SET r.resolved_from = 'content', r.cue = $cue, "
                    "              r.created_at = datetime() "
                    "RETURN r IS NOT NULL AS ok",
                    sid=sid, ref=ref, cue=cue,
                ).single()
                if rec and rec["ok"]:
                    created += 1
        print(f"\nAPPLIED: materialised {created} record→record edge(s).")
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
