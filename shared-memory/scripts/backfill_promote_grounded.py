#!/usr/bin/env python3
"""Establish the project of PARKED facts from the judgements that cite them.

Pass 1 of the parked-record repair. A fact written before the project axis was
required carries no project, but a decision or retrospective that CITES it as
evidence does — and when every judgement citing a fact names the same project,
that agreement is the best evidence available without asking a human.

IT GOES THROUGH THE PROMOTION WRITER, not through SQL of its own. That is the
whole point: the transition has one writer, so a backfill and the live ingress
path cannot drift into two different notions of what promoting means, and every
row this produces lands in the same ledger with the same refusals applied. A
SQL-only version would also silently UNDER-REACH — only a minority of judgements
carry `grounded_in` in Postgres — and would bypass the ledger entirely.

WHAT IT WILL NOT DO. Two judgements naming two projects leave the fact parked.
Parked is visible and repairable; a plausible wrong project is neither, and the
writer is one-way, so a wrong answer here cannot be corrected through the
supported path. Abstentions — judgements with no project of their own — are
ignored rather than counted as disagreement.

⚠ A KNOWN, ACCEPTED CONSEQUENCE. Stamping a fact with its citing judgement's
project means that fact can never later evidence a CROSS-project link, because
by construction it now matches. That is accepted deliberately: the gate that
would read such links does not exist yet, and getting every record onto the axis
comes first. It bounds the effect to the facts this pass reaches — the entity
vote in the next pass is independent evidence and is not affected.

Dry-run by default.

    python backfill_promote_grounded.py                 # report only
    python backfill_promote_grounded.py --apply         # promote
"""
import argparse
import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path

# The first version carrying the promotion writer and its ledger table.
MIN_GATEWAY_VERSION = (0, 8, 36)
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8888")


def _load_env() -> None:
    here = Path(__file__).resolve().parent
    env_path = next((p for p in (here.parent / ".env", here.parent.parent / ".env")
                     if p.exists()), None)
    if env_path is None:
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


_load_env()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from project_axis import PROJECT_SQL  # noqa: E402
from project_promotion import (  # noqa: E402
    promote_record, sole_project, METHOD_GROUNDING,
)

DSN = (
    f"postgresql://{os.environ.get('PG_USER', 'postgres')}:"
    f"{os.environ.get('PG_PASSWORD', '')}@{os.environ.get('PG_HOST', 'localhost')}:"
    f"{os.environ.get('PG_PORT', '5432')}/{os.environ.get('PG_DATABASE', 'agent_data')}"
)

# Parked facts, with the distinct projects of every judgement citing them.
# `grounded_in` is a JSON array of pg_ids on the judgement, so the join is a
# containment test rather than an equality — which is also why this cannot be a
# migrations/ file: apply.py globs [0-9]*.sql and would run it as schema.
CANDIDATES_SQL = f"""
SELECT f.id                                   AS pg_id,
       array_agg(DISTINCT {PROJECT_SQL.replace('metadata', 'j.metadata')})
                                              AS judgement_projects,
       array_agg(DISTINCT j.id)               AS judgement_ids
  FROM technical_docs f
  JOIN technical_docs j
    ON j.metadata->>'type' IN ('decision', 'retrospective')
   AND j.metadata->'grounded_in' @> to_jsonb(f.id)
 WHERE ({PROJECT_SQL.replace('metadata', 'f.metadata')} IS NULL
        OR {PROJECT_SQL.replace('metadata', 'f.metadata')} = 'general_discussion')
   AND f.metadata->>'type' IS DISTINCT FROM 'decision'
   AND f.metadata->>'type' IS DISTINCT FROM 'retrospective'
 GROUP BY f.id
 ORDER BY f.id
"""


def gateway_version() -> tuple[str, tuple[int, ...] | None]:
    try:
        with urllib.request.urlopen(f"{GATEWAY_URL}/health", timeout=10) as r:
            raw = json.load(r).get("version", "")
    except Exception as exc:
        return f"unreachable ({exc})", None
    try:
        return raw, tuple(int(p) for p in raw.split(".")[:3])
    except ValueError:
        return raw, None


def gateway_has_promotion_writer(parsed) -> bool:
    """Fails closed — an unknown version is not permission to write. The ledger
    table does not exist before this version, so a promotion against an older
    deployment would half-apply: metadata and outbox written, evidence lost."""
    return parsed is not None and tuple(parsed) >= MIN_GATEWAY_VERSION


async def run(apply: bool) -> int:
    import asyncpg

    conn = await asyncpg.connect(DSN)
    try:
        rows = await conn.fetch(CANDIDATES_SQL)
        decided, ambiguous = [], []
        for r in rows:
            agreed = sole_project(list(r["judgement_projects"]))
            (decided if agreed else ambiguous).append((r, agreed))

        print(f"Parked facts cited by a judgement : {len(rows)}")
        print(f"  judgements agree on one project : {len(decided)}")
        print(f"  ambiguous — left parked         : {len(ambiguous)}")
        for r, agreed in decided:
            print(f"      pg_id {r['pg_id']:>5} → {agreed!r}"
                  f"   (cited by {list(r['judgement_ids'])})")
        for r, _ in ambiguous:
            print(f"      pg_id {r['pg_id']:>5} AMBIGUOUS "
                  f"{sorted(set(r['judgement_projects']))} — left parked")

        if not apply:
            print("\nDry run — nothing promoted. Re-run with --apply.")
            return 0
        if not decided:
            print("\nNothing to do.")
            return 0

        raw, parsed = gateway_version()
        if not gateway_has_promotion_writer(parsed):
            need = ".".join(str(p) for p in MIN_GATEWAY_VERSION)
            print(f"\nREFUSING: the running gateway reports {raw!r}; the promotion "
                  f"writer and its ledger exist from {need}. Deploy first.",
                  file=sys.stderr)
            return 3
        print(f"\nRunning gateway is {raw} — has the promotion writer.")

        promoted = refused = 0
        for r, agreed in decided:
            async with conn.transaction():
                result = await promote_record(
                    conn, r["pg_id"], agreed,
                    method=METHOD_GROUNDING,
                    actor="backfill_promote_grounded",
                    note=f"judgements {sorted(r['judgement_ids'])} agree",
                )
            if result["promoted"]:
                promoted += 1
            else:
                refused += 1
                print(f"  refused pg_id={r['pg_id']}: {result['reason']}")
        print(f"\nPromoted {promoted}, refused {refused}. "
              f"The gateway's outbox worker applies the graph half.")
        return 0
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="perform the promotions (default: report only)")
    args = ap.parse_args()
    return asyncio.run(run(args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
