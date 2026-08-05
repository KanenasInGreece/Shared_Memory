#!/usr/bin/env python3
"""Project-name normalisation — one rename, applied whole or not at all.

Canonical project names equal the project folder name. This tool merges legacy
free-text spellings into the canonical name across BOTH stores and the registry:

  * Postgres ``technical_docs``: rewrites ``metadata->>'project'`` and
    ``metadata->'decision'->>'project'``.
  * Postgres registry: re-points the promotions ledger and the alias mappings,
    retires the old registry row, and records the rename as an alias.
  * Neo4j: rewires every ``PROJECT_OF`` edge from the alias ``:Project`` node to
    the canonical node, then deletes the alias node.

⚠ THE UNIT OF WORK IS ONE RENAME PAIR, AND IT IS ATOMIC (invariant N1).

This used to run as two sweeps over the whole map — rewrite every record and
COMMIT, then try to record every alias — and a third, the graph, which knew
nothing about either. Any failure in the second sweep therefore left a committed
state the first sweep had created and the second could not finish: records moved
to the new name, the old name still registered, no alias recorded, and the graph
rewired regardless. Three ways to end up half-renamed, none of them reported as
such.

Now each pair is one transaction. It commits as a whole or rolls back as a
whole, its graph half runs ONLY if its Postgres half committed (N3), a failed
pair is reported by name with its reason and does not stop the pairs after it,
and the exit status is non-zero when any pair failed. Re-running is a no-op for
the pairs that succeeded.

⚠ A BLOCKED RENAME IS SAFE, AND THAT IS WHY THE ATOMICITY COMES FIRST. Two
foreign keys point at ``projects.name`` (the promotions ledger and the alias
junction), so retiring a registry row can be vetoed. Because the pair rolls back
whole, a veto leaves the old name registered and still resolving — every save
keeps working and nothing is half-moved. The veto is an obstacle to the rename,
never a hazard to the corpus.

⚠ INACTIVE ALIAS ROWS WILL VETO A RENAME, BY DESIGN AND NOT YET BY EXPERIENCE.
A4 says mappings are superseded, never deleted, so a superseded row keeps
pointing at the name it resolved to. Re-pointing it would falsify the history it
exists to preserve, so this tool re-points ACTIVE rows only — and the first
superseded row ever written for a project will therefore block that project's
next rename. Nothing in the codebase sets ``active = false`` today and there are
no inactive rows, so the block is latent; the resolution is registry identity (a
surrogate key), where the mapping refers to the project rather than to the
spelling it had that day, and neither re-pointing nor a veto arises at all.

Usage:
    uv run --with psycopg2-binary --with neo4j python \\
        shared-memory/scripts/normalize_projects.py \\
        --map "shared_memory=shared-memory-GitHub,shared-memory=shared-memory-GitHub" \\
        [--dry-run]

With no --map, the PROJECT_ALIASES environment variable is used. ``--dry-run``
reports every row the rename WOULD touch — records, ledger rows and alias rows —
and writes nothing.
"""

import argparse
import os
import sys
from pathlib import Path

import psycopg2
from neo4j import GraphDatabase

sys.path.insert(0, os.path.dirname(__file__))
from ontology import ONT  # noqa: E402
from project_alias import ALIAS_UPSERT_SQL  # noqa: E402
from project_axis import PROJECT_MATCH_SQL  # noqa: E402


def _load_env() -> None:
    """Populate credentials from the framework .env — this is a standalone CLI
    run in a fresh shell where PG_PASSWORD/NEO4J_PASSWORD are otherwise unset
    (credentials are read from .env, never hardcoded).

    ⚠ THE FRAMEWORK ENV IS ``shared-memory/.env``; the repo root is the
    FALLBACK. This used to read the repo-root path only, and its docstring
    claimed that matched ``migrations/apply.py`` — which tries the framework
    path first. On an install that keeps credentials only where the documented
    setup puts them, this script therefore connected with an empty password and
    died on `fe_sendauth: no password supplied`: a shipped tool that could not
    run at all, reported by nothing until someone tried to use it. Same
    candidate order as apply.py, so there is one answer to "where does the
    password live" rather than one per script.
    """
    here = Path(__file__).resolve().parent
    candidates = [here.parent / ".env", here.parent.parent / ".env"]
    env_path = next((p for p in candidates if p.exists()), None)
    if env_path is None:
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


_load_env()

_pg_pass = os.environ.get("PG_PASSWORD", "")
PG_CONN = os.environ.get(
    "PG_CONN", f"postgresql://postgres:{_pg_pass}@localhost:5432/agent_data"
)
# Env-overridable, never a baked-in literal: our bolt port is one valid
# configuration, not the configuration. Matches backfill_project_of.py.
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_AUTH = (os.environ.get("NEO4J_USER", "neo4j"),
              os.environ.get("NEO4J_PASSWORD", ""))


def parse_alias_map(raw: str) -> dict:
    aliases = {}
    for pair in raw.split(","):
        old, sep, new = pair.partition("=")
        if sep and old.strip() and new.strip():
            aliases[old.strip()] = new.strip()
    return aliases


# ── What a rename IS, as a value ─────────────────────────────────────────────
#
# The statement sequence is a pure function so the invariants can be asserted
# DIRECTLY — the order, the note preservation, the active-only filter — instead
# of grepped out of the executor. A guard disabled with `if False and …` leaves
# its own text in the file, so a test that reads source passes against a dead
# guard; that has happened in this repo twice.

def rename_statements(old: str, new: str) -> list:
    """The ordered (sql, params) pairs that move ``old`` onto ``new``.

    ⚠ THE ORDER IS THE INVARIANT, not a preference:

      1-2. Rewrite the records. Per-field, deliberately NOT through the shared
           COALESCE: a row carrying the old name in the decision blob and a
           newer one at the top level still needs rewriting, and the resolution
           would shadow it.
      3.   Re-point the promotions ledger, PRESERVING the name each row
           originally targeted in its own ``note`` (N2). The ledger's whole
           purpose is to answer "what was this before"; a rename that silently
           rewrote its target would destroy exactly the evidence it holds. Never
           ON UPDATE CASCADE — a cascade would repoint it with no trace at all.
      4.   Re-point every ACTIVE alias already aimed at the old name. This is
           what keeps resolution ONE HOP (A3): a chain a→b→c collapses to a→c
           and b→c at write time, so ingress never walks links and can never
           meet a cycle. Superseded rows are left alone — see the module note.
      5.   DELETE the old registry row BEFORE interning it as an alias. A1 says
           a string is never both a registered project and an alias, and a
           trigger enforces it, so inserting first would simply be refused.
      6.   Only then map old → new, in one statement: the string is interned and
           the mapping inserted together, so there is no round trip in which the
           alias exists with nothing pointing at it.
    """
    trail = (
        "to_project was " + repr(old) + " until it was renamed to " + repr(new)
        + " by normalize_projects"
    )
    return [
        ("UPDATE technical_docs"
         "   SET metadata = jsonb_set(metadata, '{project}', to_jsonb(%s::text))"
         " WHERE metadata->>'project' = %s",
         (new, old)),
        ("UPDATE technical_docs"
         "   SET metadata = jsonb_set(metadata, '{decision,project}', to_jsonb(%s::text))"
         " WHERE metadata->'decision'->>'project' = %s",
         (new, old)),
        # concat_ws skips NULLs, so a row with no note gets the trail alone
        # rather than a leading separator.
        #
        # ⚠ BOTH LEDGER COLUMNS MOVE, AND THEY MOVE FOR DIFFERENT REASONS
        # (migration 027). `to_project_id` is re-pointed because the identity it
        # named is about to be deleted and the foreign key would otherwise veto
        # the rename. `to_project` is re-pointed because a promotion's target is
        # a name a reader looks up — and the name it originally carried is
        # preserved in the note, which is the trade this statement has always
        # made. The id does not make the note redundant: the note says what the
        # target was CALLED, the id says which project it IS.
        ("UPDATE project_promotions"
         "   SET to_project = %s,"
         "       to_project_id = (SELECT id FROM projects WHERE name = %s),"
         "       note = concat_ws(' | ', note, %s || ' on ' || now())"
         " WHERE to_project = %s",
         (new, new, trail, old)),
        ("UPDATE project_aliases"
         "   SET project_id = (SELECT id FROM projects WHERE name = %s)"
         " WHERE project_id = (SELECT id FROM projects WHERE name = %s)"
         "   AND active",
         (new, old)),
        ("DELETE FROM projects WHERE name = %s", (old,)),
        ("WITH interned AS (" + ALIAS_UPSERT_SQL.format(p="%s") + ")"
         " INSERT INTO project_aliases (alias_id, project_id, reason, created_by)"
         " SELECT interned.id, p.id, %s, 'normalize_projects'"
         "   FROM interned, projects p WHERE p.name = %s"
         " ON CONFLICT DO NOTHING",
         (old, f"renamed to {new}", new)),
    ]


# ── What a rename WOULD touch ────────────────────────────────────────────────

# The preflight is code, not a note in a runbook: --dry-run reports the ledger
# and alias rows a rename will re-point, because those are the rows whose
# foreign keys can veto it, and finding that out from a failed run is finding it
# out too late.
_PREFLIGHT = [
    ("records",
     "SELECT count(*) FROM technical_docs WHERE " + PROJECT_MATCH_SQL.format(p="%s"),
     lambda old: (old, old)),
    ("promotion ledger rows",
     "SELECT count(*) FROM project_promotions WHERE to_project = %s",
     lambda old: (old,)),
    ("active alias rows",
     "SELECT count(*) FROM project_aliases pa JOIN projects p ON p.id = pa.project_id"
     " WHERE p.name = %s AND pa.active",
     lambda old: (old,)),
    ("SUPERSEDED alias rows (these will VETO the rename)",
     "SELECT count(*) FROM project_aliases pa JOIN projects p ON p.id = pa.project_id"
     " WHERE p.name = %s AND NOT pa.active",
     lambda old: (old,)),
]


def preflight(conn, old: str) -> dict:
    """What the rename will touch, without touching it."""
    out = {}
    with conn.cursor() as cur:
        for label, sql, params in _PREFLIGHT:
            cur.execute(sql, params(old))
            out[label] = cur.fetchone()[0]
    return out


def apply_rename(conn, old: str, new: str) -> bool:
    """One rename pair, in ONE transaction. True when it committed.

    A registry row that cannot be retired — something still references it — is
    REPORTED and rolled back, never forced: a rename that has to break a
    reference is not a rename, it is a different decision.
    """
    try:
        with conn.cursor() as cur:
            for sql, params in rename_statements(old, new):
                cur.execute(sql, params)
        conn.commit()
        return True
    except Exception as exc:
        conn.rollback()
        print(f"  FAILED {old!r} → {new!r} — {exc}".rstrip())
        print(f"         nothing was written for this pair; "
              f"{old!r} is still registered and still resolves.")
        return False


def rename_pair(conn, old: str, new: str, dry_run: bool) -> bool:
    """Report what the pair touches, then apply it. True when it committed.

    ⚠ The registration check happens BEFORE any write. It used to run in the
    second sweep, after the records had already been committed onto a name that
    might not be registered at all.
    """
    if old == new:
        print(f"  SKIPPED {old!r} — an alias of itself is not a rename")
        return False

    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM projects WHERE name = %s", (new,))
        registered = cur.fetchone() is not None
    if not registered:
        print(f"  SKIPPED {old!r} → {new!r} — {new!r} is not a registered project")
        return False

    counts = preflight(conn, old)
    detail = " · ".join(f"{n} {label}" for label, n in counts.items() if n)
    print(f"  {old!r} → {new!r}: {detail or 'nothing to move'}")

    if dry_run:
        return False
    return apply_rename(conn, old, new)


def rewire_neo4j(driver, old: str, new: str, new_id=None) -> None:
    """The graph half — reached only for a pair whose Postgres half committed.

    ``new_id`` is the surviving project's registry identity (migration 027). It
    is passed rather than looked up here because this function is the graph half
    of a transaction that has already committed in Postgres, and re-reading the
    registry would be reading it at a different instant from the one that
    decided the move. It may be None on a deployment that has not migrated;
    then the node keeps its name-keyed shape, exactly as before.
    """
    with driver.session() as session:
        count = session.run(
            f"MATCH (p:{ONT.project} {{name: $old}})<-[r]-() RETURN count(r) AS n",
            old=old,
        ).single()["n"]
        print(f"  neo4j: Project {old!r} → {new!r}: {count} inbound edge(s)")
        # Rewire PROJECT_OF edges (the only inbound type the ontology writes to
        # Project nodes) to the canonical node. MERGE keeps it idempotent.
        # The surviving node is stamped with the identity it is surviving AS,
        # because this is one of the two places a Project node can be minted and
        # a node minted without an identity is one the insight gate will decline
        # to count — a merge must not leave the graph less foldable than it
        # found it.
        session.run(
            f"MATCH (alias:{ONT.project} {{name: $old}})"
            f" MERGE (canon:{ONT.project} {{name: $new}})"
            f" SET canon.project_id = coalesce($new_id, canon.project_id)"
            f" WITH alias, canon"
            f" MATCH (n)-[r:{ONT.project_of}]->(alias)"
            f" MERGE (n)-[:{ONT.project_of}]->(canon)"
            f" DELETE r",
            old=old, new=new, new_id=new_id,
        )
        # Drop the alias node only when nothing else points at it — an
        # unexpected edge type means a manual look, not a silent delete.
        leftover = session.run(
            f"MATCH (alias:{ONT.project} {{name: $old}})-[r]-() RETURN count(r) AS n",
            old=old,
        ).single()
        if leftover and leftover["n"]:
            print(f"  neo4j: {old!r} kept — {leftover['n']} unexpected edge(s); "
                  f"inspect manually.")
        else:
            session.run(
                f"MATCH (alias:{ONT.project} {{name: $old}}) DELETE alias",
                old=old,
            )


def preview_neo4j(driver, old: str, new: str) -> None:
    with driver.session() as session:
        count = session.run(
            f"MATCH (p:{ONT.project} {{name: $old}})<-[r]-() RETURN count(r) AS n",
            old=old,
        ).single()["n"]
        print(f"  neo4j: Project {old!r} → {new!r}: {count} inbound edge(s)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--map", default=os.environ.get("PROJECT_ALIASES", ""),
                    help="comma-separated old=new pairs (default: $PROJECT_ALIASES)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report affected rows/edges without writing")
    args = ap.parse_args()

    aliases = parse_alias_map(args.map)
    if not aliases:
        sys.exit("No alias map — pass --map or set PROJECT_ALIASES.")

    print(f"Normalising {len(aliases)} project alias(es)"
          + (" [DRY RUN]" if args.dry_run else "") + ":")

    # Connect to BOTH stores before writing to either. A rename whose graph half
    # cannot be reached should fail before the first commit, not after the last.
    conn = psycopg2.connect(PG_CONN, connect_timeout=5)
    driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
    committed, failed = [], []
    try:
        for old, new in aliases.items():
            if rename_pair(conn, old, new, args.dry_run):
                committed.append((old, new))
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM projects WHERE name = %s", (new,))
                    row = cur.fetchone()
                rewire_neo4j(driver, old, new, row[0] if row else None)
            elif old == new:
                continue          # a no-op, not a failure
            elif args.dry_run:
                preview_neo4j(driver, old, new)
            else:
                failed.append((old, new))
    finally:
        conn.close()
        driver.close()

    if args.dry_run:
        print("Dry run complete — nothing written.")
        return 0
    print(f"Done: {len(committed)} renamed, {len(failed)} failed.")
    if failed:
        # Non-zero so a caller that chains on this cannot mistake a partial run
        # for a complete one.
        print("  failed pairs: " + ", ".join(f"{o}→{n}" for o, n in failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
