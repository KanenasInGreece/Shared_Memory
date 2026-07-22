#!/usr/bin/env python3
"""Fragment-rate telemetry (decision 890, fact 889) — read-only, safe to run
anytime. Reports the word-count distribution of every live :Entity name and
how many would be rejected by a tightened sanitize_entity_name() word-count
gate, so MAX_ENTITY_NAME_WORDS is calibrated against real graph data instead
of guessed. Also reports ALIASES edges where either endpoint would be
rejected — the "bad alias rate" fact 889 is actually about.

Usage (on the gateway host):
    uv run --with neo4j --with python-dotenv python shared-memory/scripts/entity_fragment_rate.py [max_words]
"""
import os
import sys
from pathlib import Path

from neo4j import GraphDatabase

sys.path.insert(0, os.path.dirname(__file__))
from ontology import sanitize_entity_name, ONT  # noqa: E402


def _load_env() -> None:
    here = Path(__file__).resolve()
    candidates = [here.parent.parent / ".env", here.parent.parent.parent / ".env"]
    env_path = next((p for p in candidates if p.exists()), None)
    if env_path is None:
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def word_count(name: str) -> int:
    return len(name.split(" "))


def main() -> int:
    max_words = int(sys.argv[1]) if len(sys.argv) > 1 else int(
        os.environ.get("MAX_ENTITY_NAME_WORDS", "4"))

    _load_env()
    password = os.environ.get("NEO4J_PASSWORD", "")
    if not password:
        print("ERROR: NEO4J_PASSWORD not set (shared-memory/.env).", file=sys.stderr)
        return 2

    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as session:
            names = [r["name"] for r in session.run(
                f"MATCH (e:{ONT.entity}) RETURN e.name AS name")]

            # Only names that already pass today's gate are in scope for the
            # NEW word-count rule (numeric/noise/empty names are a separate,
            # already-solved problem).
            live_names = [n for n in names if sanitize_entity_name(n) is not None]

            histogram: dict[int, int] = {}
            would_reject = []
            for n in live_names:
                wc = word_count(n)
                bucket = wc if wc < 6 else 6
                histogram[bucket] = histogram.get(bucket, 0) + 1
                if wc > max_words:
                    would_reject.append((wc, n))

            print(f"Entities scanned: {len(names)} total, {len(live_names)} "
                  f"already pass today's gate\n")
            print("Word-count histogram (of names passing today's gate):")
            for wc in sorted(histogram):
                label = f"{wc}" if wc < 6 else "6+"
                print(f"  {label:>3} word(s): {histogram[wc]}")

            print(f"\nWith MAX_ENTITY_NAME_WORDS={max_words}: "
                  f"{len(would_reject)}/{len(live_names)} "
                  f"({100*len(would_reject)/len(live_names):.2f}%) would be rejected")
            if would_reject:
                print("  worst offenders:")
                for wc, n in sorted(would_reject, reverse=True)[:15]:
                    print(f"    [{wc}w] {n!r}")

            # ALIASES edges where either endpoint would be rejected — the
            # actual headline number fact 889 is about.
            reject_set = {n for _, n in would_reject}
            if reject_set:
                bad_aliases = session.run(
                    f"MATCH (a:{ONT.entity})-[r:{ONT.aliases}]-(b:{ONT.entity}) "
                    f"WHERE a.name IN $names OR b.name IN $names "
                    f"RETURN DISTINCT a.name AS a, b.name AS b, r.confidence AS conf",
                    names=list(reject_set),
                ).data()
                print(f"\nALIASES edges with a fragment-shaped endpoint: "
                      f"{len(bad_aliases)}")
                for row in bad_aliases[:15]:
                    print(f"  {row['a']!r} <-> {row['b']!r} "
                          f"(confidence={row['conf']})")
            else:
                print("\nALIASES edges with a fragment-shaped endpoint: 0")
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
