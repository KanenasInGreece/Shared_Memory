#!/usr/bin/env python3
"""
migrate_retro_edges.py — one-time conversion of legacy HAD_OUTCOME self-loop
retrospectives into full records (retro-as-record, stage 3).

For every legacy `(d:Decision)-[o:HAD_OUTCOME {rating,date,notes}]->(d)` edge:
  1. Postgres: INSERT a technical_docs row — content = the notes (embedded via
     the gateway, hard mandate), metadata carries target_pg_id, the rating
     mapped onto the outcome-state enum (original wording preserved as
     original_rating), migrated=true, and source/principal recovered from a
     surviving legacy outbox row where one exists (else "unknown").
     **created_at is BACKDATED to the edge's date** so recency weighting stays
     honest — a plain INSERT would stamp every legacy retro as new today.
  2. Neo4j: MERGE the :Retrospective node (rem_processed=true — legacy
     enrichment is a deliberate later choice, never a surprise REM backlog),
     MERGE the (d)-[:HAD_OUTCOME {date}]->(r) trigger edge, DELETE the self-loop.
  3. No outbox row is written — a migrated record is applied by construction.

Legacy outbox retro rows are NOT touched: rows already consumed by an insight
fold were deleted by the ledger close, and surviving open rows remain valid
re-fold triggers (the insight path keys them on COALESCE(target_pg_id, pg_id)
and reads the wording from the migrated record after conversion).

Re-runnable: converted self-loops are deleted, so a re-run sees an empty
worklist; the content-hash upsert makes the Postgres side idempotent.

    # inspect the mapping first (no writes):
    uv run --with httpx --with psycopg2-binary --with neo4j \
      python shared-memory/scripts/migrate_retro_edges.py --dry-run
    # convert:
    ... migrate_retro_edges.py --apply
"""
import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx
import psycopg2
import psycopg2.extensions
from neo4j import GraphDatabase

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ontology import ONT, RETRO_RATINGS  # noqa: E402


def _load_env() -> None:
    # The live deployment keeps .env at shared-memory/.env; older layouts used
    # the repo root. Try both — setdefault means the first found wins.
    for env_path in (Path(__file__).parent.parent / ".env",
                     Path(__file__).parent.parent.parent / ".env"):
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


_load_env()

NEO4J_URI  = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASS = os.environ.get("NEO4J_PASSWORD", "")
_pg_pass   = os.environ.get("PG_PASSWORD", "")
PG_CONN    = os.environ.get(
    "PG_CONN", f"postgresql://postgres:{_pg_pass}@localhost:5432/agent_data"
)
EMBED_URL = os.environ.get("EMBED_URL", "http://localhost:8888/v1/embeddings")
_AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "").strip() or None
BACKUP_ADVISORY_LOCK_KEY = int(os.environ.get("BACKUP_ADVISORY_LOCK_KEY", "8765309"))

# Legacy free-text rating → outcome-state enum. Deterministic; the original
# wording is always preserved in metadata.original_rating. Anything not in the
# map falls to "mixed" and is FLAGGED in the report — inspect via --dry-run.
RATING_MAP: dict[str, str] = {
    # held up
    "validated": "validated", "high": "validated", "good": "validated",
    "validated-external": "validated", "validated-external-result": "validated",
    "confirmed-after": "validated", "implemented": "validated",
    "shipped-verified": "validated", "shipped": "validated",
    "success": "validated", "positive": "validated",
    "validated-direction": "validated",   # direction held; scope nuance in notes
    "amended": "refined",                 # the decision evolved (e.g. 580 → 582)
    # partly / poorly (but not withdrawn)
    "mixed": "mixed", "medium": "mixed", "low": "mixed",
    "partial": "mixed", "negative": "mixed",
    # the decision evolved
    "refined": "refined", "allocation-evolution": "refined",
    "gating-evolution": "refined", "evolved": "refined",
    # not yet judged
    "pending": "pending", "pending-validation": "pending",
    # withdrawn (structural)
    "reversed": "reversed",
}
FALLBACK_RATING = "mixed"


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_AGENT_TOKEN}"} if _AGENT_TOKEN else {}


def embed_one(client: httpx.Client, text: str) -> list[float]:
    """One raw gateway embedding — same vector the coordinator stores (no extra
    normalisation)."""
    r = client.post(EMBED_URL, headers=_auth_headers(),
                    json={"input": [text], "model": "bge-m3"})
    if r.status_code == 401:
        sys.exit("[x] 401 from embedding proxy — set AGENT_TOKEN (gateway auth is on).")
    r.raise_for_status()
    return r.json()["data"][0]["embedding"]


def fetch_self_loops(driver) -> list[dict]:
    with driver.session() as session:
        res = session.run(
            f"MATCH (d:{ONT.decision})-[o:{ONT.had_outcome}]->(d)"
            f" RETURN d.pg_id AS decision_id, o.rating AS rating,"
            f"        o.date AS date, o.notes AS notes, elementId(o) AS edge_id"
            f" ORDER BY d.pg_id, o.date"
        )
        return [dict(r) for r in res]


def fetch_legacy_rows(conn) -> dict:
    """Surviving legacy retro outbox rows, indexed by (decision_pg_id, notes) —
    the only place source/principal survive for pre-conversion retros."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_id, cypher_params FROM neo4j_outbox"
            " WHERE COALESCE(cypher_params->>'type', 'fact') = 'retrospective'"
            "   AND cypher_params->>'v' IS NULL"
        )
        out = {}
        for pg_id, params in cur.fetchall():
            retro = (params or {}).get("retrospective") or {}
            key = (pg_id, retro.get("notes") or "")
            out[key] = {
                "source": (params or {}).get("source"),
                "principal": retro.get("principal"),
                "connected_from": retro.get("connected_from"),
            }
        return out


def build_plan(loops: list[dict], legacy_rows: dict) -> list[dict]:
    plan = []
    for e in loops:
        orig = (e["rating"] or "").strip()
        mapped = RATING_MAP.get(orig.lower())
        recovered = legacy_rows.get((e["decision_id"], e["notes"] or ""), {})
        plan.append({
            **e,
            "mapped_rating": mapped or FALLBACK_RATING,
            "unmapped": mapped is None,
            "source": recovered.get("source") or "unknown",
            "principal": recovered.get("principal"),
            "connected_from": recovered.get("connected_from"),
            "created_at": e["date"] or None,   # backdate; None → now (flagged)
        })
    return plan


def print_report(plan: list[dict]) -> None:
    print("=" * 72)
    print("  RETRO EDGE → RECORD MIGRATION PLAN")
    print("=" * 72)
    print(f"self-loop edges found: {len(plan)}")
    counts = Counter(p["mapped_rating"] for p in plan)
    print("mapping counts:", dict(sorted(counts.items())))
    by_orig = Counter((p['rating'] or '(empty)', p['mapped_rating']) for p in plan)
    for (orig, mapped), n in sorted(by_orig.items()):
        print(f"  {orig!r:<32} → {mapped:<10} ×{n}")
    unmapped = [p for p in plan if p["unmapped"]]
    if unmapped:
        print(f"\n[!] UNMAPPED (fell to {FALLBACK_RATING!r} — extend RATING_MAP?):")
        for p in unmapped:
            print(f"    decision {p['decision_id']}: rating={p['rating']!r}")
    no_date = [p for p in plan if not p["created_at"]]
    if no_date:
        print(f"\n[!] {len(no_date)} edge(s) without a date — created_at will be now():"
              f" decisions {[p['decision_id'] for p in no_date]}")
    recovered = sum(1 for p in plan if p["source"] != "unknown")
    print(f"\nprovenance recovered from surviving outbox rows: {recovered}/{len(plan)}")
    dup_notes = [k for k, n in Counter((p['decision_id'], p['notes']) for p in plan).items() if n > 1]
    if dup_notes:
        print(f"[!] duplicate (decision, notes) pairs (will hash-merge): {dup_notes}")
    print("=" * 72)


def apply_plan(plan: list[dict], conn, driver) -> dict:
    converted = 0
    with httpx.Client(timeout=60.0) as client:
        for i, p in enumerate(plan, 1):
            notes = p["notes"] or ""
            if not notes.strip():
                print(f"  [!] decision {p['decision_id']}: empty notes — skipped", file=sys.stderr)
                continue
            metadata = {
                "type": "retrospective",
                "target_pg_id": p["decision_id"],
                "rating": p["mapped_rating"],
                "original_rating": p["rating"],
                "date": p["date"] or "",
                "source": p["source"],
                "migrated": True,
            }
            if p.get("principal"):
                metadata["principal"] = p["principal"]
            if p.get("connected_from"):
                metadata["connected_from"] = p["connected_from"]
            # inherit the target decision's project (as the v2 write path does)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(metadata->'decision'->>'project',"
                    "                metadata->>'project') FROM technical_docs WHERE id=%s",
                    (p["decision_id"],),
                )
                row = cur.fetchone()
                if row and row[0]:
                    metadata["project"] = row[0]

            embedding = embed_one(client, notes)
            # Identity matches the coordinator's v2 write path: (type, target
            # decision, notes) — identical boilerplate notes on two different
            # decisions stay two records, and a retro can never hash-collide
            # with a plain fact whose content equals the notes.
            content_hash = hashlib.sha256(
                f"retrospective:{p['decision_id']}:{notes}".encode()
            ).hexdigest()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO technical_docs
                        (content, metadata, embedding, content_hash,
                         agent_id, scope, visibility, created_at)
                    VALUES (%s, %s::jsonb, %s::vector, %s, %s, 'global', 'global',
                            COALESCE(%s::timestamptz, now()))
                    ON CONFLICT (content_hash) DO UPDATE
                        SET metadata = EXCLUDED.metadata
                    RETURNING id
                    """,
                    (notes, json.dumps(metadata), str(embedding), content_hash,
                     p["source"], p["created_at"]),
                )
                retro_pg_id = cur.fetchone()[0]

            with driver.session() as session:
                session.run(
                    f"MERGE (r:{ONT.retrospective} {{pg_id: $pg_id}})"
                    f" SET r.rating = $rating, r.date = $date,"
                    f"     r.content = $content, r.source = $source,"
                    f"     r.fact_kind = 'observation',"
                    f"     r.rem_processed = true, r.migrated = true",
                    pg_id=retro_pg_id, rating=p["mapped_rating"],
                    date=p["date"] or "", content=notes[:200], source=p["source"],
                ).consume()
                session.run(
                    f"MATCH (d:{ONT.decision} {{pg_id: $target}})"
                    f" MATCH (r:{ONT.retrospective} {{pg_id: $pg_id}})"
                    f" MERGE (d)-[:{ONT.had_outcome} {{date: $date}}]->(r)",
                    target=p["decision_id"], pg_id=retro_pg_id, date=p["date"] or "",
                ).consume()
                session.run(
                    "MATCH ()-[o]->() WHERE elementId(o) = $eid DELETE o",
                    eid=p["edge_id"],
                ).consume()
            converted += 1
            print(f"  [{i}/{len(plan)}] decision {p['decision_id']} → retro record "
                  f"{retro_pg_id} ({p['rating']!r} → {p['mapped_rating']})")

    with driver.session() as session:
        remaining = session.run(
            f"MATCH (d:{ONT.decision})-[o:{ONT.had_outcome}]->(d) RETURN count(o) AS n"
        ).single()["n"]
        nodes = session.run(
            f"MATCH (r:{ONT.retrospective}) RETURN count(r) AS n"
        ).single()["n"]
    return {"converted": converted, "self_loops_remaining": remaining,
            "retrospective_nodes": nodes}


def main() -> None:
    ap = argparse.ArgumentParser(description="Convert legacy HAD_OUTCOME self-loops to Retrospective records (one-time).")
    ap.add_argument("--dry-run", action="store_true", help="print the mapping plan, write nothing")
    ap.add_argument("--apply",   action="store_true", help="convert edges to records")
    args = ap.parse_args()
    if not (args.dry_run or args.apply):
        sys.exit("choose --dry-run (inspect the plan) or --apply (convert)")

    assert set(RATING_MAP.values()) <= set(RETRO_RATINGS), "RATING_MAP must target the enum"

    conn = psycopg2.connect(PG_CONN, connect_timeout=5)
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    try:
        # Backup fence — never convert mid-dump (shared advisory lock,
        # session-scoped; auto-releases when conn closes).
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock_shared(%s)", (BACKUP_ADVISORY_LOCK_KEY,))
            if not cur.fetchone()[0]:
                sys.exit("[x] backup in progress (advisory lock held) — retry later")

        loops = fetch_self_loops(driver)
        if not loops:
            print("No HAD_OUTCOME self-loops found — nothing to migrate (already converted?).")
            return
        plan = build_plan(loops, fetch_legacy_rows(conn))
        print_report(plan)
        if args.dry_run:
            return
        summary = apply_plan(plan, conn, driver)
        print(json.dumps(summary, indent=2))
        if summary["self_loops_remaining"]:
            print("[!] self-loops remain — inspect and re-run.", file=sys.stderr)
    finally:
        conn.close()
        driver.close()


if __name__ == "__main__":
    main()
