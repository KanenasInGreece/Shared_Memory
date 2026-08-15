#!/usr/bin/env python3
"""Backfill the ``DOMAIN_OF`` edge for records written before the domain axis.

A record's sections live in its Postgres metadata from the moment it is saved;
the graph only learned to carry them with migration 028. Every record older than
that has its domain in Tier 1 and nothing in the graph, so nothing graph-side can
be gated on the axis while the gap is open.

**It enqueues outbox rows; it never writes Neo4j.** The gateway's outbox worker
applies them, so outbox atomicity holds and a partial run leaves durable work
rather than half a graph.

⚠ IT ENQUEUES A NARROW ``domain_of`` ROW, not an ordinary record row. Replaying a
fact row would also re-run that row's ``MENTIONS`` merges and resurrect every
enrichment edge a later sweep deliberately deleted. A repair must touch only what
it repairs.

TWO POPULATIONS, because two rules produce a record's sections:

* **Facts and decisions ASSERT their own** — resolved here with
  ``domain_axis.resolve_domains``, the same function ingress uses, so the tool
  and the gateway can never disagree about what a record claims.
* **Judgements that assert none INHERIT** — a decision takes the sections of the
  facts it grounds in, a retrospective takes its target decision's. Those rows
  carry ``inherit: true`` and the worker re-runs the gateway's own inheritance
  query, which declines on any record that named its own. The alternative —
  computing the inherited set here — would be a second expression of the rule,
  free to drift from the one the write path uses.

⚠⚠ **The gateway must already be running the code that HANDLES this row type.**
An older worker does not recognise ``domain_of`` and falls through to its
ordinary fact branch, which runs ``SET f.content = $content`` with the content
this row does not carry — blanking the content of every fact it touches. That is
silent, graph-side data loss, so the version check below is a GUARD, not a
convenience: enqueue only after the deploy, never before.

Records whose domain is not a REGISTERED section are reported and skipped: the
worker would drop them anyway (there is no name-keyed Domain node), and
registering on their behalf is the operator's judgement, not this tool's.

Dry-run by default. Idempotent: skips any record with a row already pending, and
the worker's apply replaces the record's edge set rather than accumulating.

    python backfill_domain_of.py                 # report only
    python backfill_domain_of.py --apply         # enqueue
"""
import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

import psycopg2

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from domain_axis import resolve_domains  # noqa: E402
from project_axis import SENTINEL, resolve_project  # noqa: E402
import secure_env  # noqa: E402

# The first version whose outbox worker handles a 'domain_of' row.
MIN_GATEWAY_VERSION = (0, 8, 47)
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8888")

# Every record that could carry a section, under either key and in either of the
# two places an axis value lives. Deliberately WIDER than the resolver: this
# picks candidates, `resolve_domains` decides, and a filter narrower than the
# resolver would hide records from the very tool meant to find them.
CANDIDATE_SQL = """
    SELECT id, coalesce(metadata->>'type', 'fact') AS rtype, metadata
      FROM technical_docs
     WHERE metadata ? 'domain' OR metadata ? 'domains'
        OR metadata->'decision' ? 'domain' OR metadata->'decision' ? 'domains'
     ORDER BY id
"""

# A retrospective inherits from its target decision, so it needs a row exactly
# when that decision ends up with one.
RETRO_SQL = """
    SELECT id, (metadata->>'target_pg_id')::bigint AS target
      FROM technical_docs
     WHERE metadata->>'type' = 'retrospective'
       AND metadata->>'target_pg_id' IS NOT NULL
     ORDER BY id
"""


def _load_env() -> None:
    # Delegates to secure_env's split loader (Credential_Custody_Plan PR A4,
    # SEC-05-class sweep) instead of parsing shared-memory/.env by hand: config
    # keys still reach os.environ via setdefault, exactly as before, but
    # PG_PASSWORD/PG_CONN/NEO4J_PASSWORD (and anything else secret-classified)
    # are held only in secure_env's in-process store — never os.environ — and
    # must be read back through secure_env.get_secret(). Same candidate order
    # (shared-memory/.env, then the pre-0.6 repo-root fallback), no library
    # dependency (secure_env parses the file itself).
    secure_env.load_split_env()


def _pg_dsn() -> str:
    return (
        f"postgresql://{os.environ.get('PG_USER', 'postgres')}:"
        f"{secure_env.get_secret('PG_PASSWORD', '')}@"
        f"{os.environ.get('PG_HOST', 'localhost')}:"
        f"{os.environ.get('PG_PORT', '5432')}/"
        f"{os.environ.get('PG_DATABASE', 'agent_data')}"
    )


def gateway_version() -> tuple[str, tuple[int, ...] | None]:
    """(raw, parsed) version of the RUNNING gateway. parsed is None whenever the
    answer is not knowable — unreachable, refused, or unparseable."""
    try:
        with urllib.request.urlopen(f"{GATEWAY_URL}/health", timeout=10) as r:
            raw = json.load(r).get("version", "")
    except Exception as exc:
        return f"unreachable ({exc})", None
    try:
        return raw, tuple(int(p) for p in raw.split(".")[:3])
    except ValueError:
        return raw, None


def gateway_handles_domain_of(parsed) -> bool:
    """Whether it is SAFE to enqueue against the running gateway.

    Fails closed: an unknown version is not permission to write. Getting this
    backwards is not a missed optimisation — it blanks fact content.
    """
    return parsed is not None and tuple(parsed) >= MIN_GATEWAY_VERSION


def registry(conn) -> dict:
    """{(project_name, domain_name): domain_id} — the sections that exist."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.name, d.name FROM project_domains d"
            "  JOIN projects p ON p.id = d.project_id"
        )
        return {(r[0], r[1]) for r in cur.fetchall()}


def already_queued(conn) -> set:
    """pg_ids with a domain_of row still pending, so a re-run before the worker
    drains does not enqueue the same repair twice."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT pg_id FROM neo4j_outbox"
            " WHERE cypher_params->>'type' = 'domain_of'"
        )
        return {r[0] for r in cur.fetchall()}


def plan(rows, retro_rows, known, pending):
    """Split the corpus into what this tool will enqueue and what it will not.

    Pure — every judgement the tool makes is visible here and testable without a
    database.

    Returns (explicit, inherit, unregistered, parked), where `explicit` is
    [(pg_id, project, [domain, …])] and `inherit` is [(pg_id, anchor), …].

    ⛔ A RETROSPECTIVE IS NEVER GIVEN AN EXPLICIT ROW, however its metadata
    reads. It does not control this axis — it inherits from the decision it
    judges — and the gateway refuses one that tries at ingress. A record can
    still CARRY the field: an older corpus predates the rule, and a bulk data
    operation can set it without meaning to. Reading the value here anyway
    would let this tool write, through the back door, exactly the edge the
    front door refuses. Measured the hard way: one retrospective in this corpus
    acquired a domain from a project-wide backfill and was enqueued as
    self-asserting; it came out correct only because an inherit row happened to
    be applied after it, which is ordering, not design.
    """
    explicit, unregistered, parked = [], [], []
    have_domains = set()
    for pg_id, rtype, metadata in rows:
        if pg_id in pending or rtype == "retrospective":
            continue
        domains = resolve_domains(metadata)
        if not domains:
            continue
        project = resolve_project(metadata)
        if not project or project == SENTINEL:
            # A section of no project is not a section. Left alone: the fix is a
            # project, and that is a decision about the record, not an edge.
            parked.append(pg_id)
            continue
        good = [d for d in domains if (project, d) in known]
        missing = [d for d in domains if (project, d) not in known]
        if missing:
            unregistered.append((pg_id, project, missing))
        if good:
            explicit.append((pg_id, project, good))
            have_domains.add(pg_id)

    inherit = [
        pg_id for pg_id, target in retro_rows
        if target in have_domains and pg_id not in pending
    ]
    return explicit, inherit, unregistered, parked


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="enqueue the outbox rows (default: report only)")
    args = ap.parse_args()

    _load_env()
    conn = psycopg2.connect(_pg_dsn(), connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute(CANDIDATE_SQL)
            rows = cur.fetchall()
            cur.execute(RETRO_SQL)
            retro_rows = cur.fetchall()
        known = registry(conn)
        pending = already_queued(conn)

        explicit, inherit, unregistered, parked = plan(
            rows, retro_rows, known, pending)

        print(f"records naming a domain            : {len(rows)}")
        print(f"  already queued for repair        : {len(pending)}")
        print(f"  no resolvable project (left)     : {len(parked)}")
        print(f"  naming an UNREGISTERED section   : {len(unregistered)}")
        for pg_id, project, missing in unregistered:
            print(f"      pg_id {pg_id}: {missing} not registered under {project!r}")
        print(f"  TO BACKFILL (asserted)           : {len(explicit)}")
        print(f"  TO BACKFILL (retro, inherited)   : {len(inherit)}")
        by_domain: dict = {}
        for _pg, project, domains in explicit:
            for d in domains:
                by_domain[(project, d)] = by_domain.get((project, d), 0) + 1
        for (project, d), n in sorted(by_domain.items(), key=lambda kv: -kv[1]):
            print(f"      {n:>4}  {project} / {d}")

        if not args.apply:
            print("\nDry run — nothing enqueued. Re-run with --apply.")
            return 0
        if not explicit and not inherit:
            print("\nNothing to do.")
            return 0

        # GUARD, not a courtesy — see the module docstring.
        raw, parsed = gateway_version()
        if not gateway_handles_domain_of(parsed):
            need = ".".join(str(p) for p in MIN_GATEWAY_VERSION)
            print(f"\nREFUSING to enqueue: the running gateway reports {raw!r}, and "
                  f"these rows are only handled from {need}.\n"
                  f"An older worker would fall through to its ordinary fact branch "
                  f"and BLANK the content of every record it touched.\n"
                  f"Deploy first (restart the gateway on this code), then re-run.",
                  file=sys.stderr)
            return 3
        print(f"\nRunning gateway is {raw} — handles these rows.")

        with conn.cursor() as cur:
            for pg_id, project, domains in explicit:
                cur.execute(
                    "INSERT INTO neo4j_outbox (pg_id, cypher_params, status)"
                    " VALUES (%s, %s, 'pending')",
                    (pg_id, json.dumps({"type": "domain_of", "project": project,
                                        "domains": domains})),
                )
            for pg_id in inherit:
                cur.execute(
                    "INSERT INTO neo4j_outbox (pg_id, cypher_params, status)"
                    " VALUES (%s, %s, 'pending')",
                    (pg_id, json.dumps({"type": "domain_of", "inherit": True,
                                        "anchor": "Retrospective"})),
                )
        conn.commit()
        print(f"\nEnqueued {len(explicit)} asserted + {len(inherit)} inherited "
              f"domain_of row(s). The gateway's outbox worker applies them; each "
              f"row is deleted on success.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
