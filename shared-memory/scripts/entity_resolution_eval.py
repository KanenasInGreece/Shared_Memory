#!/usr/bin/env python3
"""
entity_resolution_eval.py — offline Entity-Resolution calibration harness (ADR-017)
===================================================================================
Server-side diagnostic. Converts the *suspicion* "cosine-similarity merge destroys
the graph" into a calibrated number on OUR actual entity set, so we can pick a
threshold for the alias layer (ADR-017) and prove that a raw-cosine merge would
over-link. It is the input-side instrument that makes entity resolution measurable
(family-C consolidation quality, pg_id 366).

It is a CALIBRATION tool, not live telemetry:
  - O(n²) pairwise — runs on demand, never in the hot path. The cheap aggregate
    counts (alias edges, singletons, hubs) live in /memory/telemetry instead
    (coordinator._entity_graph).
  - read-only — touches neither Neo4j writes nor Postgres writes.
  - cosine is used ONLY as a candidate GENERATOR to be filtered, never as a
    verdict. We already know embedding cosine is not a quality signal
    (feedback_consolidation_quality_def); this harness exists to quantify exactly
    how badly it over-merges so the alias proposer gates candidates with
    lexical-Jaccard + an LLM check rather than trusting cosine.

Ported from Cloe's tier3 `entity_resolution_eval.py`, with one adaptation. Cloe's
entities carry an ontology `label`, so her over-merge signal is `label != label`.
OUR Entity nodes are bare-name — `MERGE (e:Entity {name})` (coordinator.py) — with
no type/domain on the node. So we DERIVE the conflict axis from the project/domain
of the facts that MENTION each entity (Postgres `technical_docs.metadata`). Two
entities whose mentioning-fact domains are disjoint are a likely over-merge.

  Conflict-proxy fork (deferred, see memory `feedback_entity_conflict_proxy_fork`):
  derive-at-eval (this script) vs. store a derived `domain`/`type` ON the Entity
  node at MERGE time. Storing makes the signal first-class and cheaper but is a
  schema change; we start with derive-at-eval and revisit if the live telemetry
  wants the conflict axis cheaply.

Run on the gateway host (needs Neo4j + Postgres + an AGENT_TOKEN for the embedding
proxy on :8888). Honours the same env as the daemons.

    uv run --with httpx --with numpy --with neo4j --with psycopg2-binary \
      python shared-memory/scripts/entity_resolution_eval.py            # markdown
    uv run ... entity_resolution_eval.py --json                         # machine-readable
    uv run ... entity_resolution_eval.py --thresholds 0.82,0.85,0.88
"""
import os
import sys
import json
import argparse

import httpx

try:
    import numpy as np
except ImportError:
    sys.exit("[x] numpy required: add --with numpy")
try:
    import psycopg2
except ImportError:
    sys.exit("[x] psycopg2 required: add --with psycopg2-binary")
from neo4j import GraphDatabase

# Resolve sibling imports (ontology) the same way the daemons do.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ontology import ONT

# ── Config — mirrors consolidation_loop.py so one .env drives everything ──────
NEO4J_URI  = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASS = os.environ.get("NEO4J_PASSWORD", "")
_pg_pass   = os.environ.get("PG_PASSWORD", "")
PG_CONN    = os.environ.get(
    "PG_CONN", f"postgresql://postgres:{_pg_pass}@localhost:5432/agent_data"
)
# Embed through the gateway (:8888) — NEVER 8070 directly. The gateway is the only
# thing that guarantees the 1024-dim BGE-M3 contract every other tier was built on.
EMBED_URL    = os.environ.get("EMBED_URL", "http://localhost:8888/v1/embeddings")
_AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "").strip() or None
EMBED_BATCH  = int(os.environ.get("ER_EVAL_EMBED_BATCH", "32"))

# "general" is the bucket untagged facts collapse to (coordinator default) — it is
# the absence of a domain, so it never counts as a real domain for conflict.
_UNDETERMINED = "general"
DEFAULT_THRESHOLDS = [0.75, 0.80, 0.85, 0.90, 0.95]
DIVERGENCE_COSINE  = 0.85   # "semantically near"
DIVERGENCE_JACCARD = 0.20   # "...but lexically distant" → genuine synonym candidate


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_AGENT_TOKEN}"} if _AGENT_TOKEN else {}


def fetch_entities() -> list[dict]:
    """Every genuinely-referenced Entity plus the pg_ids of the (non-superseded)
    facts/decisions that MENTION it — the basis for deriving each entity's
    domain set AND the alias-candidate pool (both callers of this function).

    Requires >=1 non-superseded MENTIONS edge (decision 890 / fact 889's
    follow-up finding): a Decision's own CONSIDERED/REJECTED/UNDER_CONDITIONS/
    PRODUCES_INSIGHT targets are free-text provenance, deliberately allowed to
    be arbitrary-length prose (rem_loop.py's registry gate, decision 718, keeps
    unregistered free phrases from ever minting a NEW node here going forward)
    — but pre-718 legacy nodes of that shape still exist with zero MENTIONS
    edges, and alias-candidate generation was treating them as ordinary named
    entities, producing nonsensical merges like a Decision's condition text
    aliased to the real entity it happens to mention in passing. Requiring a
    MENTIONS edge is the single point excluding them everywhere this harness
    is reused, without enumerating relationship types (robust to new spine
    relationships being added later)."""
    cypher = (
        f"MATCH (e:{ONT.entity}) "
        f"OPTIONAL MATCH (n)-[:{ONT.entity_link}]->(e) "
        f"  WHERE n.pg_id IS NOT NULL AND coalesce(n.superseded,false) = false "
        f"WITH e, collect(DISTINCT n.pg_id) AS pg_ids "
        f"WHERE size(pg_ids) > 0 "
        f"RETURN e.name AS name, pg_ids "
        f"ORDER BY e.name"
    )
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    try:
        with driver.session() as session:
            return [dict(r) for r in session.run(cypher).data()]
    finally:
        driver.close()


def fetch_domains(pg_ids: list[int]) -> dict[int, str]:
    """pg_id → domain (project | domain | scope | 'general'), the same coalesce the
    coordinator's telemetry breakdown uses, so the axis matches the rest of the system."""
    if not pg_ids:
        return {}
    conn = psycopg2.connect(PG_CONN, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, COALESCE(metadata->>'project', metadata->>'domain', scope, 'general') "
                "FROM technical_docs WHERE id = ANY(%s)",
                (pg_ids,),
            )
            return {row[0]: row[1] for row in cur.fetchall()}
    finally:
        conn.close()


def embed(names: list[str]) -> np.ndarray:
    """L2-normalised BGE-M3 embeddings for entity names, batched through the gateway.
    Falls back to per-item on any batch-shape mismatch so one odd server response
    can't silently misalign names↔vectors."""
    vecs: list[list[float]] = []
    with httpx.Client(timeout=60.0) as client:
        for i in range(0, len(names), EMBED_BATCH):
            batch = names[i:i + EMBED_BATCH]
            print(f"    embedding {i + len(batch)}/{len(names)}…", file=sys.stderr)
            data = _embed_call(client, batch)
            if len(data) != len(batch):
                data = [_embed_call(client, [n])[0] for n in batch]
            vecs.extend(data)
    arr = np.asarray(vecs, dtype=np.float64)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return arr / norms


def _embed_call(client: httpx.Client, batch: list[str]) -> list[list[float]]:
    r = client.post(EMBED_URL, headers=_auth_headers(),
                    json={"input": batch, "model": "bge-m3"})
    if r.status_code == 401:
        sys.exit("[x] 401 from embedding proxy — set AGENT_TOKEN (gateway auth is on).")
    r.raise_for_status()
    return [d["embedding"] for d in r.json()["data"]]


def jaccard(a: str, b: str) -> float:
    import re
    wa = set(re.findall(r"\w+", a.lower()))
    wb = set(re.findall(r"\w+", b.lower()))
    union = wa | wb
    return len(wa & wb) / len(union) if union else 0.0


def real_domains(pg_ids: list[int], dom_map: dict[int, str]) -> set[str]:
    """The entity's domains, excluding the 'general' undetermined bucket."""
    return {dom_map[p] for p in pg_ids if dom_map.get(p, _UNDETERMINED) != _UNDETERMINED}


def evaluate(thresholds: list[float]) -> dict:
    print("[*] Fetching entities from Neo4j…", file=sys.stderr)
    nodes = fetch_entities()
    if len(nodes) < 2:
        sys.exit(f"[!] Only {len(nodes)} entities — nothing to evaluate.")
    print(f"[✓] {len(nodes)} entities.", file=sys.stderr)

    all_pg_ids = sorted({p for n in nodes for p in (n["pg_ids"] or [])})
    dom_map = fetch_domains(all_pg_ids)
    domains = [real_domains(n["pg_ids"] or [], dom_map) for n in nodes]
    names = [n["name"] for n in nodes]
    # Fact-set per entity — the basis for the GRAPH shared-fact signal. On a
    # sparse-fact graph this signal is absent for most candidates (an entity with
    # 0 facts can share none), so the alias proposer must NOT lean on it as the
    # primary weight. This density profile is what proves that on OUR data.
    factsets = [set(n["pg_ids"] or []) for n in nodes]
    nfacts = [len(s) for s in factsets]
    density = {
        "orphan":         sum(1 for k in nfacts if k == 0),   # 0 facts → no graph signal
        "single_fact":    sum(1 for k in nfacts if k == 1),
        "multi_fact":     sum(1 for k in nfacts if k >= 2),
        "distinct_facts": len(set().union(*factsets)) if factsets else 0,
    }

    vecs = embed(names)
    sim = vecs @ vecs.T

    # share_fact = candidate pairs that share >=1 fact (graph signal is present);
    # its fraction of candidates is the realizability test for graph-Jaccard.
    stats = {t: {"candidates": 0, "domain_conflicts": 0, "judgeable": 0, "share_fact": 0}
             for t in thresholds}
    divergent: list[dict] = []
    overmerges: list[dict] = []
    divergence_count = 0
    judgeable_entities = sum(1 for d in domains if d)

    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            cos = float(sim[i, j])
            di, dj = domains[i], domains[j]
            judgeable = bool(di) and bool(dj)            # both have a real domain
            conflict = judgeable and di.isdisjoint(dj)   # ...and they share none

            shares_fact = bool(factsets[i] and factsets[j] and (factsets[i] & factsets[j]))
            for t in thresholds:
                if cos >= t:
                    stats[t]["candidates"] += 1
                    if shares_fact:
                        stats[t]["share_fact"] += 1
                    if judgeable:
                        stats[t]["judgeable"] += 1
                        if conflict:
                            stats[t]["domain_conflicts"] += 1

            if cos >= DIVERGENCE_COSINE:
                jac = jaccard(names[i], names[j])
                if jac < DIVERGENCE_JACCARD:
                    divergence_count += 1
                    if len(divergent) < 15:
                        divergent.append({"a": names[i], "b": names[j],
                                          "cosine": round(cos, 4), "jaccard": round(jac, 4)})
                if conflict and len(overmerges) < 15:
                    overmerges.append({
                        "a": names[i], "a_domains": sorted(di),
                        "b": names[j], "b_domains": sorted(dj),
                        "cosine": round(cos, 4),
                    })

    table = []
    for t in thresholds:
        s = stats[t]
        ratio = (s["domain_conflicts"] / s["judgeable"] * 100) if s["judgeable"] else None
        share_pct = (s["share_fact"] / s["candidates"] * 100) if s["candidates"] else None
        table.append({"threshold": t, **s, "conflict_ratio_pct": ratio,
                      "share_fact_pct": share_pct})

    return {
        "entities_total": len(nodes),
        "entities_domain_judgeable": judgeable_entities,
        "density": density,
        "thresholds": table,
        "divergence": {
            "cosine_floor": DIVERGENCE_COSINE, "jaccard_ceiling": DIVERGENCE_JACCARD,
            "count": divergence_count, "examples": divergent[:10],
        },
        "overmerge_examples": overmerges[:10],
        "conflict_proxy": "domain-of-mentioning-facts (entity nodes are bare-name)",
    }


def print_markdown(d: dict) -> None:
    print("=" * 72)
    print("  ENTITY-RESOLUTION CALIBRATION — cosine over-merge risk (ADR-017)")
    print("=" * 72)
    print(f"\nEntities: {d['entities_total']}  "
          f"(domain-judgeable: {d['entities_domain_judgeable']})")
    print(f"Conflict proxy: {d['conflict_proxy']}\n")

    den = d.get("density")
    if den:
        n = d["entities_total"] or 1
        print("### 0. Fact-density (graph-signal realizability)")
        print(f"- orphan (0 facts, no graph signal): **{den['orphan']}** ({100*den['orphan']/n:.1f}%)")
        print(f"- exactly 1 fact: {den['single_fact']} ({100*den['single_fact']/n:.1f}%)  "
              f"| >=2 facts: {den['multi_fact']} ({100*den['multi_fact']/n:.1f}%)")
        print(f"- distinct facts referenced: {den['distinct_facts']}")
        print("*A graph shared-fact (nodeSimilarity) signal is only available for candidate "
              "pairs where BOTH entities touch facts; the `share_fact` column below is that "
              "fraction — if low, graph-Jaccard cannot be the primary alias signal.*\n")

    print("### 1. Cosine threshold → over-merge risk & graph-signal coverage")
    print("| Threshold | Candidate pairs | Share ≥1 fact | Domain-judgeable | Cross-domain (false-merge) | Conflict ratio |")
    print("| :-- | --: | --: | --: | --: | --: |")
    for r in d["thresholds"]:
        cr = "n/a" if r["conflict_ratio_pct"] is None else f"{r['conflict_ratio_pct']:.1f}%"
        sf = "n/a" if r.get("share_fact_pct") is None else f"{r['share_fact']} ({r['share_fact_pct']:.0f}%)"
        print(f"| **{r['threshold']:.2f}** | {r['candidates']} | {sf} | {r['judgeable']} "
              f"| {r['domain_conflicts']} | {cr} |")
    print("\n*Candidate pairs = entity pairs a raw-cosine merge at that threshold would link.*")
    print("*Share ≥1 fact = candidates with a graph shared-fact signal present (low ⇒ graph is a sparse confirmer, not a primary weight).*")
    print("*Cross-domain = those pairs whose mentioning-fact domains are disjoint = likely over-merge.*\n")

    dv = d["divergence"]
    print("### 2. Synonym yield (the addressable fragmentation)")
    print(f"- **{dv['count']}** pairs with cosine ≥ {dv['cosine_floor']} but lexical Jaccard "
          f"< {dv['jaccard_ceiling']} — concept-equal, vocabulary-different. These are the "
          "aliases exact-match MERGE silently fragments.")
    for e in dv["examples"][:5]:
        print(f"  - **{e['a']}** ↔ **{e['b']}**  (cos `{e['cosine']}`, jac `{e['jaccard']}`)")
    print()

    print("### 3. Over-merge examples (cosine ≥ 0.85, disjoint domains)")
    for o in d["overmerge_examples"][:5]:
        print(f"  - **{o['a']}** {o['a_domains']} ↔ **{o['b']}** {o['b_domains']}  (cos `{o['cosine']}`)")
    print()

    print("### 4. Verdict")
    row = next((r for r in d["thresholds"] if abs(r["threshold"] - 0.85) < 1e-6), None)
    cr = row["conflict_ratio_pct"] if row else None
    if cr is None:
        print("Insufficient domain-tagged entities to judge the conflict ratio — tag saves with "
              "`project`/`domain` to sharpen this axis. Synonym yield (§2) is unaffected.")
    elif cr > 15.0:
        print(f"⚠️  Cosine 0.85 has a **{cr:.1f}%** cross-domain conflict rate — confirms the "
              "cosine trap. The alias proposer MUST gate candidates with lexical-Jaccard + an LLM "
              "check; never auto-merge on cosine.")
    else:
        print(f"✓  Cosine 0.85 cross-domain conflict rate is **{cr:.1f}%**. Still gate with an LLM "
              "check before writing alias edges — cosine is a candidate generator, not a verdict.")
    print("=" * 72)


def main() -> None:
    ap = argparse.ArgumentParser(description="Offline ER calibration harness (ADR-017).")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--thresholds", help="comma-separated cosine thresholds")
    args = ap.parse_args()
    thresholds = ([float(x) for x in args.thresholds.split(",")]
                  if args.thresholds else list(DEFAULT_THRESHOLDS))
    result = evaluate(sorted(thresholds))
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_markdown(result)


if __name__ == "__main__":
    main()
