#!/usr/bin/env python3
"""
alias_writer.py — ADR-017 "A" alias-edge writer (periodic sweep)
================================================================
Proposes and (step 2) writes soft Entity↔Entity ``ALIASES`` edges — synonym
links that NEVER merge nodes. Candidate generation is calibrated to the live
graph's measured density (decision pg_id 509; finding pg_id 510): on a
sparse-fact graph lexical variants dominate, so the signal priority is
lexical-primary, name-cosine as the recall net, graph shared-facts a sparse
confirmer.

Two candidate tiers
-------------------
  1. normalized-exact  → AUTO-ACCEPT. Names identical after
     ``lower()`` + strip-non-alphanumeric are provably the same token
     (``API_VERSION`` ↔ ``api_version``, ``ADR-001`` ↔ ``ADR001``). Cheap O(N)
     grouping, needs no embeddings.
  2. cosine recall net → LLM-ADJUDICATE (step 2). Non-exact pairs with
     name-cosine ≥ THRESHOLD, found via the ``entity_embeddings`` pgvector store
     (embed-once, HNSW ANN) — the scaling choice over an O(N²) numpy re-embed.
     Name-cosine is a BLOCKING key here, never the verdict.

Per-pair signals recorded for the LLM + audit ledger: name-cosine, lexical
Jaccard, shared-fact count (graph confirmer), domain-disjointness of the
mentioning facts (over-merge warning).

Standalone (heavy embed work stays out of the gateway event loop). Reuses the
ER calibration harness for entity/domain fetch, embedding, and Jaccard.

    uv run --with httpx --with numpy --with neo4j --with psycopg2-binary \
      python shared-memory/scripts/alias_writer.py --dry-run
"""
import os
import re
import sys
import json
import argparse

import psycopg2
from psycopg2.extras import execute_values

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from entity_resolution_eval import (          # noqa: E402  (shared harness helpers)
    fetch_entities, fetch_domains, embed, jaccard, real_domains, PG_CONN,
)

COSINE_THRESHOLD = float(os.environ.get("ALIAS_COSINE_THRESHOLD", "0.82"))
ANN_K = int(os.environ.get("ALIAS_ANN_K", "25"))   # neighbours per entity (recall net)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(name: str) -> str:
    """Case/format-insensitive key: two names with the same key are the same
    token modulo punctuation/case (the provably-safe auto-accept class)."""
    return _NON_ALNUM.sub("", name.lower())


def _vec_literal(vec) -> str:
    return "[" + ",".join(f"{float(x):.8f}" for x in vec) + "]"


def sync_embeddings(conn, names: list[str]) -> int:
    """Embed only the entity names not yet in entity_embeddings and upsert them.
    Embed-once: a sweep pays O(new names), not O(all)."""
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM entity_embeddings WHERE name = ANY(%s)", (names,))
        have = {r[0] for r in cur.fetchall()}
    missing = [n for n in names if n not in have]
    if missing:
        vecs = embed(missing)   # L2-normalised BGE-M3, via the gateway
        rows = [(n, _vec_literal(v)) for n, v in zip(missing, vecs)]
        with conn.cursor() as cur:
            execute_values(
                cur,
                "INSERT INTO entity_embeddings (name, embedding) VALUES %s "
                "ON CONFLICT (name) DO UPDATE SET embedding = EXCLUDED.embedding, "
                "updated_at = now()",
                rows, template="(%s, %s::vector)",
            )
        conn.commit()
    return len(missing)


def ann_neighbors(conn, name: str, k: int, threshold: float) -> list[tuple[str, float]]:
    """Cosine-nearest entity names to `name` via the HNSW index, filtered to
    cosine >= threshold. The query point is a stored constant, so the ANN index
    is used (the scaling path — sublinear per query)."""
    with conn.cursor() as cur:
        cur.execute("SELECT embedding FROM entity_embeddings WHERE name = %s", (name,))
        row = cur.fetchone()
        if not row:
            return []
        emb = row[0]
        cur.execute(
            "SELECT name, 1 - (embedding <=> %s::vector) AS cos "
            "FROM entity_embeddings WHERE name <> %s "
            "ORDER BY embedding <=> %s::vector LIMIT %s",
            (emb, name, emb, k),
        )
        return [(r[0], float(r[1])) for r in cur.fetchall() if float(r[1]) >= threshold]


def already_adjudicated(conn) -> set[frozenset]:
    with conn.cursor() as cur:
        cur.execute("SELECT name_a, name_b FROM alias_adjudications")
        return {frozenset((a, b)) for a, b in cur.fetchall()}


def build_candidates(threshold: float = COSINE_THRESHOLD, k: int = ANN_K) -> dict:
    """Generate alias candidates with signals. No writes, no LLM — the shared
    core of the dry-run and (step 2) the writer."""
    nodes = fetch_entities()
    names = [n["name"] for n in nodes]
    factset = {n["name"]: set(n["pg_ids"] or []) for n in nodes}
    all_pg = sorted({p for s in factset.values() for p in s})
    dom_map = fetch_domains(all_pg)
    domains = {nm: real_domains(list(fs), dom_map) for nm, fs in factset.items()}

    conn = psycopg2.connect(PG_CONN, connect_timeout=5)
    try:
        n_embedded = sync_embeddings(conn, names)
        done = already_adjudicated(conn)

        def signals(a: str, b: str, cos: float | None) -> dict:
            di, dj = domains.get(a, set()), domains.get(b, set())
            return {
                "a": a, "b": b,
                "cosine": round(cos, 4) if cos is not None else None,
                "lexical_jaccard": round(jaccard(a, b), 4),
                "shared_facts": len(factset[a] & factset[b]),
                "domain_disjoint": bool(di) and bool(dj) and di.isdisjoint(dj),
            }

        # Tier 1 — normalized-exact grouping (O(N), no embeddings needed).
        groups: dict[str, list[str]] = {}
        for nm in names:
            groups.setdefault(normalize(nm), []).append(nm)
        auto, seen = [], set()
        for members in groups.values():
            if len(members) < 2:
                continue
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    key = frozenset((members[i], members[j]))
                    seen.add(key)
                    if key in done:
                        continue
                    auto.append(signals(members[i], members[j], None))

        # Tier 2 — cosine recall net (ANN), excluding the auto class.
        llm = []
        for nm in names:
            for other, cos in ann_neighbors(conn, nm, k, threshold):
                key = frozenset((nm, other))
                if key in seen or key in done:
                    continue
                seen.add(key)
                if normalize(nm) == normalize(other):   # belongs to Tier 1
                    continue
                llm.append(signals(nm, other, cos))
    finally:
        conn.close()

    return {
        "entities": len(names),
        "newly_embedded": n_embedded,
        "already_adjudicated": len(done),
        "auto_accept": auto,
        "llm_candidates": llm,
    }


def _print_dry_run(r: dict) -> None:
    print("=" * 70)
    print("  ALIAS-WRITER DRY-RUN (no LLM, no writes)")
    print("=" * 70)
    print(f"entities={r['entities']}  newly_embedded={r['newly_embedded']}  "
          f"already_adjudicated={r['already_adjudicated']}")
    print(f"\nTier 1 — normalized-exact AUTO-ACCEPT: {len(r['auto_accept'])} pair(s)")
    for c in r["auto_accept"][:12]:
        print(f"  {c['a']!r} <-> {c['b']!r}  (sharedFacts={c['shared_facts']})")
    llm = r["llm_candidates"]
    print(f"\nTier 2 — cosine recall net → LLM-ADJUDICATE: {len(llm)} pair(s)")
    withfacts = sum(1 for c in llm if c["shared_facts"] > 0)
    disjoint = sum(1 for c in llm if c["domain_disjoint"])
    print(f"  of which share >=1 fact: {withfacts}  |  domain-disjoint (over-merge warn): {disjoint}")
    for c in sorted(llm, key=lambda x: -x["cosine"])[:15]:
        print(f"  cos={c['cosine']} lexJac={c['lexical_jaccard']} "
              f"sharedFacts={c['shared_facts']} disjoint={c['domain_disjoint']}  "
              f"{c['a']!r} <-> {c['b']!r}")
    print("=" * 70)


def main() -> None:
    ap = argparse.ArgumentParser(description="Alias-writer sweep (ADR-017 A).")
    ap.add_argument("--dry-run", action="store_true",
                    help="generate candidates only — no LLM, no writes (step-1 mode)")
    ap.add_argument("--json", action="store_true", help="machine-readable candidate dump")
    ap.add_argument("--threshold", type=float, default=COSINE_THRESHOLD)
    args = ap.parse_args()
    if not (args.dry_run or args.json):
        sys.exit("step 2 (LLM adjudication + edge write) not wired yet; use --dry-run")
    result = build_candidates(threshold=args.threshold)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_dry_run(result)


if __name__ == "__main__":
    main()
