#!/usr/bin/env python3
"""One-time cleanup of foreign schema drift (Phase 2 compliance).

The inbound gates (coordinator + REM) now keep new writes within the ontology,
but pre-gate experiments left non-ontology labels and relationship types in the
graph — surfaced by the `compliance` section of /memory/telemetry. This removes
them, using the SAME vocabulary the telemetry checks against
(ontology.KNOWN_LABELS / KNOWN_RELATIONSHIPS), so the cleanup can never disagree
with what compliance reports.

Three categories (all shown in dry-run; --apply executes):
  A. Junk nodes      — every label is foreign  → DETACH DELETE (removes the node
                       and all its edges; this clears most foreign rels, whose
                       endpoints are these nodes).
  B. Spurious labels — node has a valid label AND a foreign one (e.g.
                       :Entity:Object) → REMOVE the foreign label, keep the node.
  C. Foreign rels    — type is foreign and BOTH endpoints survive A → DELETE the
                       relationship. Use --preserve-rel TYPE (repeatable) to keep
                       a type pending a decision (e.g. a legacy-but-meaningful
                       relationship between legitimate nodes).

Dry-run is the DEFAULT. Nothing changes unless you pass --apply.

Usage (on the gateway host):
    uv run --with neo4j python shared-memory/scripts/cleanup_foreign_schema.py
    uv run --with neo4j python shared-memory/scripts/cleanup_foreign_schema.py \
        --preserve-rel HAS_STEP --apply
"""
import argparse
import os
import re
import sys
from pathlib import Path

from neo4j import GraphDatabase

sys.path.insert(0, os.path.dirname(__file__))
from ontology import KNOWN_LABELS, KNOWN_RELATIONSHIPS  # noqa: E402

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
_VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _load_env() -> None:
    """Credentials from the framework .env (shared-memory/.env first, root fallback)."""
    here = Path(__file__).resolve()
    for cand in (here.parent.parent / ".env", here.parent.parent.parent / ".env"):
        if cand.exists():
            for line in cand.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
            return


def survey(session, preserve_rels: set[str]) -> dict:
    known_labels = list(KNOWN_LABELS)
    known_rels = list(KNOWN_RELATIONSHIPS)
    # A: nodes whose every label is foreign
    junk = session.run(
        "MATCH (n) WHERE size(labels(n)) > 0 "
        "  AND none(l IN labels(n) WHERE l IN $kl) "
        "RETURN labels(n) AS labels, coalesce(n.name, '<' + toString(n.pg_id) + '>') AS name",
        kl=known_labels,
    ).data()
    # B: mixed nodes (>=1 valid AND >=1 foreign label)
    mixed = session.run(
        "MATCH (n) WHERE any(l IN labels(n) WHERE l IN $kl) "
        "  AND any(l IN labels(n) WHERE NOT l IN $kl) "
        "RETURN labels(n) AS labels, coalesce(n.name, '<' + toString(n.pg_id) + '>') AS name",
        kl=known_labels,
    ).data()
    # C: foreign rels where both endpoints survive A (all labels valid)
    rels = session.run(
        "MATCH (a)-[r]->(b) WHERE NOT type(r) IN $kr "
        "  AND all(l IN labels(a) WHERE l IN $kl) "
        "  AND all(l IN labels(b) WHERE l IN $kl) "
        "RETURN type(r) AS rel, count(*) AS c ORDER BY c DESC",
        kr=known_rels, kl=known_labels,
    ).data()
    foreign_labels_on_mixed = sorted({
        l for row in mixed for l in row["labels"] if l not in KNOWN_LABELS
    })
    return {
        "junk_nodes": junk,
        "mixed_nodes": mixed,
        "foreign_labels_on_mixed": foreign_labels_on_mixed,
        "foreign_rels": [r for r in rels if r["rel"] not in preserve_rels],
        "preserved_rels": [r for r in rels if r["rel"] in preserve_rels],
    }


def apply(session, foreign_labels_on_mixed: list[str], foreign_rel_types: list[str]) -> dict:
    known_labels = list(KNOWN_LABELS)
    # A: delete all-foreign-label nodes
    a = session.run(
        "MATCH (n) WHERE size(labels(n)) > 0 AND none(l IN labels(n) WHERE l IN $kl) "
        "DETACH DELETE n RETURN count(*) AS n", kl=known_labels,
    ).single()["n"]
    # B: strip each foreign label from mixed nodes (label can't be parametrised;
    #    validated as a Cypher identifier before interpolation)
    b = 0
    for lbl in foreign_labels_on_mixed:
        if not _VALID_IDENTIFIER.match(lbl):
            print(f"  [!] skipping un-interpolatable label {lbl!r}", file=sys.stderr)
            continue
        b += session.run(
            f"MATCH (n:`{lbl}`) WHERE any(l IN labels(n) WHERE l IN $kl) "
            f"REMOVE n:`{lbl}` RETURN count(*) AS n", kl=known_labels,
        ).single()["n"]
    # C: delete remaining foreign rels (between surviving nodes)
    c = 0
    if foreign_rel_types:
        c = session.run(
            "MATCH ()-[r]->() WHERE type(r) IN $types DELETE r RETURN count(*) AS n",
            types=foreign_rel_types,
        ).single()["n"]
    return {"nodes_deleted": a, "labels_stripped": b, "rels_deleted": c}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    ap.add_argument("--preserve-rel", action="append", default=[],
                    help="foreign rel TYPE to KEEP (repeatable)")
    args = ap.parse_args()
    preserve = set(args.preserve_rel)

    _load_env()
    pw = os.environ.get("NEO4J_PASSWORD", "")
    if not pw:
        print("ERROR: NEO4J_PASSWORD not set (shared-memory/.env).", file=sys.stderr)
        return 2

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, pw))
    try:
        with driver.session() as session:
            s = survey(session, preserve)
            print(f"A. Junk nodes (all labels foreign): {len(s['junk_nodes'])}")
            for n in s["junk_nodes"]:
                print(f"     {n['name']!r:<26} labels={n['labels']}")
            print(f"\nB. Spurious foreign labels to strip from valid nodes: "
                  f"{s['foreign_labels_on_mixed']} on {len(s['mixed_nodes'])} node(s)")
            for n in s["mixed_nodes"][:20]:
                print(f"     {n['name']!r:<26} labels={n['labels']}")
            print(f"\nC. Foreign rels between surviving nodes (to delete): "
                  f"{[(r['rel'], r['c']) for r in s['foreign_rels']]}")
            if s["preserved_rels"]:
                print(f"   PRESERVED (--preserve-rel): "
                      f"{[(r['rel'], r['c']) for r in s['preserved_rels']]}")

            if not args.apply:
                print("\nDRY-RUN: nothing changed. Re-run with --apply to execute A+B+C.")
                return 0

            res = apply(session, s["foreign_labels_on_mixed"],
                        [r["rel"] for r in s["foreign_rels"]])
            print(f"\nAPPLIED: deleted {res['nodes_deleted']} node(s), "
                  f"stripped {res['labels_stripped']} label(s), "
                  f"deleted {res['rels_deleted']} relationship(s).")
            return 0
    finally:
        driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
