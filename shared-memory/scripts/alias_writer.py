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

import httpx
import psycopg2
from psycopg2.extras import execute_values
from neo4j import GraphDatabase

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from entity_resolution_eval import (          # noqa: E402  (shared harness helpers)
    fetch_entities, fetch_domains, embed, jaccard, real_domains, PG_CONN,
    NEO4J_URI, NEO4J_USER, NEO4J_PASS, _auth_headers,
)
from ontology import ONT                       # noqa: E402  (vocabulary-driven, never hardcode labels/rels)
import alias_graph                             # noqa: E402  (refresh_components)

COSINE_THRESHOLD = float(os.environ.get("ALIAS_COSINE_THRESHOLD", "0.82"))
ANN_K = int(os.environ.get("ALIAS_ANN_K", "25"))   # neighbours per entity (recall net)
REASONER_URL = os.environ.get("REASONER_URL", "http://localhost:8888/v1/chat/completions")
LLM_BATCH = int(os.environ.get("ALIAS_LLM_BATCH", "10"))
# Gemma-4 (the dream reasoner) degrades at very low temperature — a documented
# quirk — so this mirrors the daemons' 0.6 default rather than a near-greedy 0.2.
LLM_TEMPERATURE = float(os.environ.get(
    "ALIAS_LLM_TEMPERATURE", os.environ.get("DREAM_TEMPERATURE", "0.6")))

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


def _parse_llm_json(candidate: str):
    """Strict json first; salvage Gemma-4 slips (missing comma / unescaped char in
    a rationale) with json_repair (decision 491). Lazy import so a missing dep
    doesn't break the module. Returns parsed object or None."""
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        try:
            import json_repair
            return json_repair.loads(candidate)
        except Exception:
            return None


_ADJUDICATOR_SYSTEM = (
    "You are an entity-resolution judge for a software-engineering knowledge graph. "
    "For each pair of entity names decide whether they denote the SAME underlying "
    "concept (an ALIAS / synonym) or are DISTINCT. Names that merely share words but "
    "denote different facets (e.g. 'authentication' vs 'identity') are DISTINCT. "
    "Prefer DISTINCT when uncertain — never merge on doubt. Output ONLY JSONL: one "
    "JSON object per line, no prose, no code fences."
)


def adjudicate_batch(candidates: list[dict]) -> dict[int, dict]:
    """LLM verdicts for a batch of candidate pairs. Returns {idx: {verdict,
    confidence, rationale}}. Signals are given as HINTS; the name pair is the
    decision. Missing/failed idx are simply absent (caller leaves them for a
    later sweep)."""
    lines = []
    for i, c in enumerate(candidates):
        lines.append(
            f'{i}. A="{c["a"]}"  B="{c["b"]}"  | cosine={c["cosine"]} '
            f'lexJac={c["lexical_jaccard"]} shared_facts={c["shared_facts"]} '
            f'domain_disjoint={c["domain_disjoint"]}'
        )
    user = (
        "Signals: cosine=name-embedding similarity; lexJac=word overlap; "
        "shared_facts=documents mentioning both (higher ⇒ more likely same); "
        "domain_disjoint=true means they appear in unrelated project areas (a mild "
        "distinctness hint, NOT decisive — the same concept can span projects).\n"
        'For each pair output one line: {"idx": <n>, "verdict": "alias"|"distinct", '
        '"confidence": <0.0-1.0>, "rationale": "<short>"}\n\nPairs:\n'
        + "\n".join(lines)
    )
    try:
        resp = httpx.post(
            REASONER_URL, headers=_auth_headers(), timeout=180.0,
            json={"model": "local-model", "temperature": LLM_TEMPERATURE,
                  "messages": [{"role": "system", "content": _ADJUDICATOR_SYSTEM},
                               {"role": "user", "content": user}]},
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        print(f"  [!] LLM batch failed: {exc}", file=sys.stderr)
        return {}
    out: dict[int, dict] = {}
    for line in raw.splitlines():
        s, e = line.find("{"), line.rfind("}") + 1
        if s == -1 or e == 0:
            continue
        obj = _parse_llm_json(line[s:e])
        if isinstance(obj, dict) and "idx" in obj and "verdict" in obj:
            out[int(obj["idx"])] = obj
    return out


def _record_adjudications(conn, rows: list[tuple]) -> None:
    """Persist verdicts (audit + don't-re-ask). rows: (name_a, name_b, verdict,
    method, confidence, cosine, lexical_jaccard, shared_facts, domain_disjoint,
    rationale). name_a < name_b canonical order is enforced here."""
    if not rows:
        return
    canon = [(min(a, b), max(a, b), *rest) for a, b, *rest in rows]
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO alias_adjudications (name_a, name_b, verdict, method, "
            "confidence, cosine, lexical_jaccard, shared_facts, domain_disjoint, "
            "rationale) VALUES %s ON CONFLICT (name_a, name_b) DO NOTHING",
            canon,
        )
    conn.commit()


def _write_alias_edge(session, a: str, b: str, method: str,
                      confidence: float | None, cosine: float | None) -> None:
    """Soft, revocable ALIASES edge (never merges nodes). Vocabulary-driven via
    ONT — no hardcoded label/rel literals (bring-your-own-ontology guardrail)."""
    session.run(
        f"MATCH (a:{ONT.entity} {{name: $a}}), (b:{ONT.entity} {{name: $b}}) "
        f"MERGE (a)-[r:{ONT.aliases}]-(b) "
        f"SET r.method = $method, r.confidence = $confidence, r.cosine = $cosine, "
        f"    r.created_by = 'alias_writer', "
        f"    r.created_at = coalesce(r.created_at, datetime())",
        a=a, b=b, method=method, confidence=confidence, cosine=cosine,
    ).consume()


def run_sweep(threshold: float = COSINE_THRESHOLD, k: int = ANN_K,
              limit: int | None = None) -> dict:
    """Full writer: candidate-gen → auto-accept + LLM-adjudicate → write ALIASES
    edges → persist verdicts → refresh alias components. Writes only accepted
    aliases; distinct verdicts are recorded so they are not re-asked."""
    cand = build_candidates(threshold=threshold, k=k)
    conn = psycopg2.connect(PG_CONN, connect_timeout=5)
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    accepted = rejected = 0
    try:
        with driver.session() as session:
            # Tier 1 — normalized-exact auto-accept.
            auto_rows = []
            for c in cand["auto_accept"]:
                _write_alias_edge(session, c["a"], c["b"], "normalized_exact", 1.0, None)
                auto_rows.append((c["a"], c["b"], "alias", "normalized_exact", 1.0,
                                  c["cosine"], c["lexical_jaccard"], c["shared_facts"],
                                  c["domain_disjoint"], "normalized-exact"))
                accepted += 1
            _record_adjudications(conn, auto_rows)

            # Tier 2 — LLM adjudication in batches.
            llm = cand["llm_candidates"]
            if limit is not None:
                llm = llm[:limit]
            for start in range(0, len(llm), LLM_BATCH):
                batch = llm[start:start + LLM_BATCH]
                verdicts = adjudicate_batch(batch)
                rows = []
                for i, c in enumerate(batch):
                    v = verdicts.get(i)
                    if not v:
                        continue                     # unresolved → leave for next sweep
                    verdict = "alias" if v.get("verdict") == "alias" else "distinct"
                    conf = v.get("confidence")
                    conf = float(conf) if isinstance(conf, (int, float)) else None
                    if verdict == "alias":
                        _write_alias_edge(session, c["a"], c["b"], "llm", conf, c["cosine"])
                        accepted += 1
                    else:
                        rejected += 1
                    rows.append((c["a"], c["b"], verdict, "llm", conf, c["cosine"],
                                 c["lexical_jaccard"], c["shared_facts"],
                                 c["domain_disjoint"], str(v.get("rationale", ""))[:500]))
                _record_adjudications(conn, rows)

            stamped = alias_graph.refresh_components(session)
    finally:
        conn.close()
        driver.close()
    return {"aliases_written": accepted, "distinct_recorded": rejected,
            "auto_accept": len(cand["auto_accept"]),
            "llm_candidates": len(cand["llm_candidates"]),
            "components_stamped": stamped}


def main() -> None:
    ap = argparse.ArgumentParser(description="Alias-writer sweep (ADR-017 A).")
    ap.add_argument("--dry-run", action="store_true",
                    help="generate candidates only — no LLM, no writes")
    ap.add_argument("--apply", action="store_true",
                    help="run LLM adjudication and WRITE ALIASES edges")
    ap.add_argument("--json", action="store_true", help="machine-readable candidate dump")
    ap.add_argument("--threshold", type=float, default=COSINE_THRESHOLD)
    ap.add_argument("--limit", type=int, default=None,
                    help="cap LLM-adjudicated candidates (safe incremental rollout)")
    args = ap.parse_args()
    if args.apply:
        print(json.dumps(run_sweep(threshold=args.threshold, limit=args.limit), indent=2))
        return
    result = build_candidates(threshold=args.threshold)
    if args.json:
        print(json.dumps(result, indent=2))
    elif args.dry_run:
        _print_dry_run(result)
    else:
        sys.exit("choose --dry-run (candidates only) or --apply (adjudicate + write)")


if __name__ == "__main__":
    main()
