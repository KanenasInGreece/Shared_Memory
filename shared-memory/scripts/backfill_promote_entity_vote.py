#!/usr/bin/env python3
"""Establish a parked fact's project from the projects of its NEIGHBOURS.

Pass 2 of the parked-record repair, after inheritance from citing judgements.
A fact carries entities; the other facts mentioning those entities carry
projects. Where they agree unanimously that is evidence about this fact; where
they merely lean it is evidence about the corpus, and the difference is measured
rather than assumed — see ``entity_vote`` for the holdout that fixed the bands.

⚠ THE POPULATION IS SELECTED FROM POSTGRES, NEVER FROM THE GRAPH EDGE. "Parked"
means the project RESOLUTION is absent or the sentinel. Selecting facts that lack
a ``PROJECT_OF`` edge instead looks equivalent and is not: 28 facts on this corpus
are parked in Postgres while carrying an edge nothing can justify, so an
edge-side selector silently drops exactly the records most in need of repair.
That mistake has now been made twice here — once when the population was measured
as 91 instead of 126, and once in this script's own first draft.

WHAT IS WRITTEN WITHOUT ASKING, and why only that. Unanimity measured 100% correct
on 57 held-out facts. Everything below it is PROPOSED, because every error the
vote made was a SISTER PROJECT absorbed into the larger one — one at 95%
agreement across 20 supporting facts — and a sister project silently losing its
records is not a rounding error, it is the distinction the operator relies on
being deleted in the direction of whatever already dominates.

Every promotion goes through the promotion writer, so a backfill and live ingress
cannot drift into two notions of what promoting means, and every row lands in the
same ledger with the vote's share and support recorded as its evidence.

    python backfill_promote_entity_vote.py                      # report
    python backfill_promote_entity_vote.py --review-file r.md    # + proposals
    python backfill_promote_entity_vote.py --apply               # write unanimous only
"""
import argparse
import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path

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
from ontology import ONT  # noqa: E402
from project_axis import PROJECT_SQL  # noqa: E402
from project_promotion import promote_record, METHOD_OPERATOR  # noqa: E402
from entity_vote import (  # noqa: E402
    tally, vote_band, is_auto, BAND_AUTO, BAND_HIGH, BAND_REVIEW, BAND_NONE,
    MIN_SUPPORT, REVIEW_FLOOR, CLOSE_REVIEW_CEILING,
)
from insight_gate import INSIGHT_HUB_DEGREE_CAP  # noqa: E402

METHOD_VOTE = "entity_vote"

DSN = (
    f"postgresql://{os.environ.get('PG_USER', 'postgres')}:"
    f"{os.environ.get('PG_PASSWORD', '')}@{os.environ.get('PG_HOST', 'localhost')}:"
    f"{os.environ.get('PG_PORT', '5432')}/{os.environ.get('PG_DATABASE', 'agent_data')}"
)
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_AUTH = (os.environ.get("NEO4J_USER", "neo4j"),
              os.environ.get("NEO4J_PASSWORD", ""))

PARKED_FACTS_SQL = f"""
SELECT id FROM technical_docs
 WHERE ({PROJECT_SQL} IS NULL OR {PROJECT_SQL} = 'general_discussion')
   AND metadata->>'type' IS DISTINCT FROM 'decision'
   AND metadata->>'type' IS DISTINCT FROM 'retrospective'
 ORDER BY id
"""

# Neighbours that may vote. Two exclusions, both deliberate:
#  * MEGA-HUBS — an entity linked to everything says nothing about any one fact.
#    Reuses the insight gate's cap so "too connected to mean anything" has ONE
#    definition rather than one per consumer.
#  * AXIS-AS-TOPIC entities (`Project: …`) — these are being removed, and until
#    they are they would let a fact vote on itself through a node that is really
#    the axis wearing an entity's label. Excluding them now also means their
#    deletion cannot change this pass's result afterwards.
VOTE_CYPHER = f"""
MATCH (f:{ONT.fact} {{pg_id: $pg_id}})-[:{ONT.entity_link}|{ONT.entity_link_alias}]->(e:{ONT.entity})
WHERE size([(e)--(x) | x]) <= $hub_cap
  AND NOT e.name STARTS WITH 'Project:'
MATCH (e)<-[:{ONT.entity_link}|{ONT.entity_link_alias}]-(o:{ONT.fact})-[:{ONT.project_of}]->(p:{ONT.project})
WHERE o.pg_id <> $pg_id
RETURN p.name AS proj, count(DISTINCT o) AS n
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
    """Fails closed — an unknown version is not permission to write."""
    return parsed is not None and tuple(parsed) >= MIN_GATEWAY_VERSION


def write_review_file(path: str, groups: dict) -> None:
    """The proposals an operator decides on, grouped by CONFIDENCE and showing
    the competing projects — never only the winner. A sister-project fact leaning
    towards the project beside it must read as a contest, not as a result."""
    lines = [
        "# Parked facts — project proposals awaiting confirmation",
        "",
        f"Unanimous proposals ({BAND_AUTO}) are applied automatically and are NOT listed here.",
        "",
        "⚠ Every error this vote made in validation was a SISTER PROJECT absorbed",
        "into the larger one — one at 95% agreement across 20 supporting facts.",
        "Check the competing column before accepting a row.",
        "",
    ]
    titles = {
        BAND_HIGH: f"## High agreement ({CLOSE_REVIEW_CEILING:.0%}–99%) — quick review",
        BAND_REVIEW: f"## Close ({REVIEW_FLOOR:.0%}–{CLOSE_REVIEW_CEILING:.0%}) — read these carefully",
    }
    for band in (BAND_HIGH, BAND_REVIEW):
        rows = groups.get(band) or []
        lines += [titles[band], "", f"{len(rows)} record(s).", ""]
        if rows:
            lines += ["| pg_id | proposed | share | support | competing | content |",
                      "|---|---|---|---|---|---|"]
            for r in rows:
                competing = ", ".join(
                    f"{p} ({n})" for p, n in sorted(r["all"], key=lambda x: -x[1])
                    if p != r["project"]) or "—"
                snippet = (r["content"] or "").replace("|", "\\|")[:90]
                lines.append(
                    f"| {r['pg_id']} | `{r['project']}` | {r['share']:.0%} | "
                    f"{r['support']} | {competing} | {snippet} |"
                )
        lines.append("")
    Path(path).write_text("\n".join(lines) + "\n")


async def _apply_confirmed(conn, groups, confirm: str, excluded: str) -> int:
    """Write the proposals an operator has confirmed.

    ⚠ THE VOTE IS RE-DERIVED, never read back from the file or the message that
    listed it. Two reasons, both live: promoting a record makes it a VOTING
    NEIGHBOUR, so earlier writes in this same run change later tallies — the
    proposal count moved 62 → 64 for exactly that reason — and an operator may
    confirm from a listing produced minutes or days earlier. A confirmation is
    approval of a JUDGEMENT, not of a stale row, so anything whose proposal no
    longer stands is refused rather than written on the strength of an old
    number.
    """
    skip = {int(x) for x in excluded.split(",") if x.strip().isdigit()}
    proposals = {r["pg_id"]: r for r in groups[BAND_HIGH] + groups[BAND_REVIEW]}
    if confirm.strip().lower() == "proposed":
        wanted = set(proposals)
    else:
        wanted = {int(x) for x in confirm.split(",") if x.strip().isdigit()}
    wanted -= skip

    written = refused = 0
    for pg_id in sorted(wanted):
        row = proposals.get(pg_id)
        if row is None:
            print(f"  refused pg_id={pg_id}: no live proposal — the vote no longer "
                  f"places it in a review band")
            refused += 1
            continue
        async with conn.transaction():
            result = await promote_record(
                conn, pg_id, row["project"],
                method=METHOD_OPERATOR, actor="operator",
                note=(f"operator-confirmed entity vote: {row['share']:.0%} of "
                      f"{row['support']} neighbouring facts; competing "
                      f"{[(p, n) for p, n in row['all'] if p != row['project']] or 'none'}"),
            )
        if result["promoted"]:
            written += 1
        else:
            refused += 1
            print(f"  refused pg_id={pg_id}: {result['reason']}")
    if skip:
        print(f"  withheld by --except: {sorted(skip)}")
    print(f"\nOperator-confirmed: wrote {written}, refused {refused}.")
    return written


async def run(apply: bool, review_file: str | None,
              confirm: str = "", excluded: str = "") -> int:
    import asyncpg
    from neo4j import AsyncGraphDatabase

    conn = await asyncpg.connect(DSN)
    driver = AsyncGraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
    try:
        parked = [r["id"] for r in await conn.fetch(PARKED_FACTS_SQL)]
        groups: dict[str, list] = {BAND_AUTO: [], BAND_HIGH: [],
                                   BAND_REVIEW: [], BAND_NONE: []}
        async with driver.session() as session:
            for pg_id in parked:
                res = await session.run(VOTE_CYPHER, pg_id=pg_id,
                                        hub_cap=INSIGHT_HUB_DEGREE_CAP)
                rows = [{"proj": r["proj"], "n": r["n"]} async for r in res]
                project, share, support = tally(rows)
                band = vote_band(project, share, support)
                content = await conn.fetchval(
                    "SELECT left(content, 200) FROM technical_docs WHERE id = $1", pg_id)
                groups[band].append({
                    "pg_id": pg_id, "project": project, "share": share,
                    "support": support, "content": content,
                    "all": [(r["proj"], r["n"]) for r in rows],
                })

        print(f"Parked facts (selected from POSTGRES, not from the edge): {len(parked)}")
        print(f"  unanimous  → written automatically : {len(groups[BAND_AUTO])}")
        print(f"  {CLOSE_REVIEW_CEILING:.0%}-99%     → confirm (group 1)      : {len(groups[BAND_HIGH])}")
        print(f"  {REVIEW_FLOOR:.0%}-{CLOSE_REVIEW_CEILING:.0%}     → confirm (group 2)      : {len(groups[BAND_REVIEW])}")
        print(f"  below {REVIEW_FLOOR:.0%} or too thin → pass 3 (sentinel)   : {len(groups[BAND_NONE])}")
        print(f"  (support floor {MIN_SUPPORT}, hub cap {INSIGHT_HUB_DEGREE_CAP})")

        if groups[BAND_AUTO]:
            print("\n  unanimous, by project:")
            by: dict[str, int] = {}
            for r in groups[BAND_AUTO]:
                by[r["project"]] = by.get(r["project"], 0) + 1
            for name, n in sorted(by.items(), key=lambda kv: -kv[1]):
                print(f"      {n:>4}  {name}")

        if review_file:
            write_review_file(review_file, groups)
            print(f"\nProposals written to {review_file}")

        if confirm:
            raw, parsed = gateway_version()
            if not gateway_has_promotion_writer(parsed):
                print(f"\nREFUSING: gateway reports {raw!r}.", file=sys.stderr)
                return 3
            await _apply_confirmed(conn, groups, confirm, excluded)
            return 0

        if not apply:
            print("\nDry run — nothing written. Re-run with --apply.")
            return 0
        if not groups[BAND_AUTO]:
            print("\nNothing unanimous to write.")
            return 0

        raw, parsed = gateway_version()
        if not gateway_has_promotion_writer(parsed):
            need = ".".join(str(p) for p in MIN_GATEWAY_VERSION)
            print(f"\nREFUSING: gateway reports {raw!r}; the promotion writer and its "
                  f"ledger exist from {need}. Deploy first.", file=sys.stderr)
            return 3
        print(f"\nRunning gateway is {raw} — has the promotion writer.")

        promoted = refused = 0
        for r in groups[BAND_AUTO]:
            assert is_auto(vote_band(r["project"], r["share"], r["support"]))
            async with conn.transaction():
                result = await promote_record(
                    conn, r["pg_id"], r["project"],
                    method=METHOD_VOTE, actor="backfill_promote_entity_vote",
                    note=(f"entity vote unanimous: {r['share']:.0%} of "
                          f"{r['support']} neighbouring facts"),
                )
            promoted += 1 if result["promoted"] else 0
            refused += 0 if result["promoted"] else 1
            if not result["promoted"]:
                print(f"  refused pg_id={r['pg_id']}: {result['reason']}")
        print(f"\nPromoted {promoted}, refused {refused}.")
        return 0
    finally:
        await driver.close()
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="write the unanimous band (proposals are never written)")
    ap.add_argument("--review-file", default=None,
                    help="path to write the operator proposal file")
    ap.add_argument("--confirm", default="",
                    help="operator-confirmed proposals: 'proposed' for every "
                         "row in the review bands, or a comma-separated id list")
    ap.add_argument("--except", dest="excluded", default="",
                    help="comma-separated pg_ids to withhold from --confirm")
    args = ap.parse_args()
    return asyncio.run(run(args.apply, args.review_file,
                          args.confirm, args.excluded))


if __name__ == "__main__":
    raise SystemExit(main())
