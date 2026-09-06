#!/usr/bin/env python3
"""Prove that the live Neo4j carries every constraint neo4j_init.cypher declares.

WHY THIS EXISTS — AND WHY IT IS THE INVERSE OF THE POSTGRES CASE.
``verify_schema_init.py`` catches a guarantee that holds on every UPGRADED
deployment and on no NEW one: the shipped fast-path file had silently lost whole
classes of object, and only a stranger's fresh install would ever find out.

Neo4j fails the other way round. ``neo4j_init.cypher`` is a one-time manual step
— nothing applies it on startup, and no migration chain replays it — so a fresh
install gets all of it and a long-lived instance gets whatever was true the day
someone ran it. This deployment declares seven uniqueness constraints and, when
this script was written, was enforcing exactly ONE of them (``fact_pg_id``). The
guarantee held for every new install and not for the corpus that mattered.

Nothing detects that, because a missing uniqueness constraint is silent by
construction: MERGE keeps working, writes keep succeeding, and the only symptom
is a duplicate node appearing under a race — at which point the constraint that
would have prevented it is the thing you no longer have.

WHAT THIS PROVES, AND WHAT IT CANNOT.
Neo4j Community runs a single database, so — unlike the Postgres verifier —
this cannot build a throwaway instance and diff against it. It does the two
checks that are actually available, which together cover the failure above:

  * DECLARED vs LIVE. Every constraint in the .cypher file must exist in the
    live instance. This is the check that was missing.
  * CAN IT EVEN BE CREATED. For each missing constraint, count the duplicates
    that would make ``CREATE CONSTRAINT`` fail. A constraint reported as
    "missing" is one thing; a constraint that CANNOT be added because the data
    already violates it is a different and much more urgent thing, and the
    difference must not be discovered halfway through an apply.

WHAT THIS DOES TO YOUR DATA: NOTHING, unless you pass --apply. The default is a
read-only report. ``--apply`` creates only the constraints the file declares and
the instance lacks, and refuses any whose duplicate count is non-zero.

FOREIGN SCHEMA IS EXPECTED, NOT A DIVERGENCE. A Neo4j instance may be shared
with another system, so live constraints the framework never declared are
REPORTED for information and never touched. Removing them is not this script's
call to make.

Usage:
    uv run --with neo4j python shared-memory/migrations/verify_neo4j_init.py
    ... --apply     create the declared constraints that are missing
Exit status is 1 when a declared constraint is missing, so CI can gate on it.
"""
import os
import re
import sys
from pathlib import Path

from neo4j import GraphDatabase

HERE = Path(__file__).resolve().parent
INIT_CYPHER = HERE / "neo4j_init.cypher"

# CREATE CONSTRAINT <name> IF NOT EXISTS FOR (n:<Label>) REQUIRE n.<prop> IS UNIQUE
_DECLARED = re.compile(
    r"CREATE\s+CONSTRAINT\s+(?P<name>\w+)\s+IF\s+NOT\s+EXISTS\s+"
    r"FOR\s*\(\s*\w+\s*:\s*(?P<label>\w+)\s*\)\s*"
    r"REQUIRE\s+\w+\.(?P<prop>\w+)\s+IS\s+UNIQUE",
    re.IGNORECASE,
)

# A label the framework never writes is somebody else's schema, and this
# instance is allowed to be shared. Listed so the report can say "foreign, and
# deliberately left alone" rather than leaving a reader to guess — a check that
# reports a known-benign difference every run is one people learn to ignore.
FOREIGN_LABELS = {
    "Conversation", "Message", "Preference", "ReasoningTrace", "ReasoningStep",
    "ToolCall", "User", "Tool", "MemoryReadAudit", "ConsolidationRun",
}


def _load_env() -> None:
    """Load the framework env WITHOUT depending on python-dotenv.

    ⚠ THIS USED TO IMPORT `dotenv` AND `return` SILENTLY WHEN IT WAS ABSENT, and
    that is a worse failure than it looks. Nothing was loaded, so the very next
    connection attempted came back `fe_sendauth: no password supplied` — a
    CREDENTIALS error for what is actually a missing dependency, sending the
    reader to check passwords, roles and pg_hba while the real cause was the
    invocation. Worst of all in THIS file, whose whole job is to prove a
    property: a checker that dies for a reason it misreports teaches the wrong
    lesson twice.

    So it parses the file itself, exactly as apply.py does — no dependency, no
    silent path. The framework env is `shared-memory/.env`; the repo root is
    only a FALLBACK, and the candidate-list form is what keeps a correctly
    installed machine working (three scripts once read the root alone and died).

    First definition wins, and the real environment always wins: values already
    exported must not be overwritten by a file, or an operator pointing the tool
    at another database with an env var would silently be given this one.
    """
    for candidate in (HERE.parent / ".env", HERE.parent.parent / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if not key:
                # A pasted banner line ("=== ... ===") has no key, and
                # os.environ.setdefault("", ...) raises OSError [Errno 22].
                # Skip it, exactly as secure_env.py's loader does.
                continue
            os.environ.setdefault(key, val.strip())

def declared_constraints(text: str) -> dict:
    """{name: (label, property)} for every constraint the shipped file declares."""
    return {m.group("name"): (m.group("label"), m.group("prop"))
            for m in _DECLARED.finditer(text)}


def live_constraints(session) -> dict:
    """{name: (label, property)} for every uniqueness constraint in force.

    Keyed on the NAME as well as the pair, because a constraint enforcing the
    right thing under a different name is still the right guarantee — the report
    below compares the pair, not the name.
    """
    out = {}
    for r in session.run("SHOW CONSTRAINTS"):
        labels, props = r.get("labelsOrTypes") or [], r.get("properties") or []
        if labels and props:
            out[r["name"]] = (labels[0], props[0])
    return out


def conflicting_index(session, label: str, prop: str) -> str | None:
    """A plain index on the SAME label+property, blocking the constraint.

    ⚠ THIS IS AN UPGRADE-PATH TRAP, AND IT IS INVISIBLE UNTIL YOU HIT IT.
    Neo4j refuses ``CREATE CONSTRAINT`` while a non-constraint index covers the
    same key — *"There already exists an index (:Entity {name}). A constraint
    cannot be created until the index has been dropped."* A fresh install never
    sees this, because ``neo4j_init.cypher`` runs before anything creates
    indexes. A long-lived instance that added a lookup index by hand — this one
    had ``entity_name_idx`` on the single most important key in the graph — is
    blocked forever, and the only symptom is the constraint quietly not being
    there.

    Dropping it is safe and is NOT a loss of the access path: a uniqueness
    constraint creates its own backing RANGE index on the same key, so lookups
    keep exactly the index they had. Only an index owned by NO constraint is
    ever reported here; a constraint's own backing index must never be dropped.
    """
    for r in session.run("SHOW INDEXES"):
        if (r.get("owningConstraint") is None
                and (r.get("labelsOrTypes") or []) == [label]
                and (r.get("properties") or []) == [prop]):
            return r["name"]
    return None


def duplicate_count(session, label: str, prop: str) -> int:
    """How many values of ``label.prop`` are held by more than one node.

    This is what decides whether a missing constraint can simply be added or
    whether it is reporting real damage that has to be repaired first.
    """
    return session.run(
        f"MATCH (n:`{label}`) WHERE n.`{prop}` IS NOT NULL"
        f" WITH n.`{prop}` AS v, count(*) AS c WHERE c > 1"
        f" RETURN count(*) AS dupes"
    ).single()["dupes"]


def main() -> int:
    _load_env()
    apply = "--apply" in sys.argv
    if not INIT_CYPHER.is_file():
        print(f"neo4j_init.cypher not found at {INIT_CYPHER}", file=sys.stderr)
        return 2

    declared = declared_constraints(INIT_CYPHER.read_text())
    if not declared:
        print("neo4j_init.cypher declares no constraints — refusing to report a "
              "clean run against an empty expectation.", file=sys.stderr)
        return 2

    driver = GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"),
              os.environ.get("NEO4J_PASSWORD", "")),
    )
    try:
        with driver.session() as session:
            live = live_constraints(session)
            live_pairs = set(live.values())

            missing, blocked = [], []
            print(f"declared in neo4j_init.cypher : {len(declared)}")
            print(f"live uniqueness constraints   : {len(live)}\n")
            for name, (label, prop) in sorted(declared.items()):
                if (label, prop) in live_pairs:
                    print(f"  ✅ {label}.{prop:12} enforced")
                    continue
                dupes = duplicate_count(session, label, prop)
                clash = conflicting_index(session, label, prop)
                mark = "⛔" if dupes else "❌"
                note = (f"{dupes} duplicated value(s) — MUST BE REPAIRED FIRST"
                        if dupes else "0 duplicates, safe to create")
                if clash and not dupes:
                    note += (f"; blocked by plain index {clash!r}, which --apply "
                             f"will drop (the constraint re-creates it)")
                print(f"  {mark} {label}.{prop:12} MISSING ({name}) — {note}")
                (blocked if dupes else missing).append((name, label, prop, clash))

            foreign = {n: lp for n, lp in live.items()
                       if lp not in set(declared.values())}
            if foreign:
                print("\n  foreign schema on this instance (reported, never "
                      "touched — the instance may be shared):")
                for n, (label, prop) in sorted(foreign.items()):
                    tag = "" if label in FOREIGN_LABELS else "  ⚠ UNRECOGNISED"
                    print(f"    · {label}.{prop} ({n}){tag}")

            if apply and missing:
                print()
                for name, label, prop, clash in missing:
                    if clash:
                        # Order matters and is one-way: with 0 duplicates
                        # already proven above, the CREATE that follows cannot
                        # fail, so the key is never left without an index.
                        session.run(f"DROP INDEX {clash} IF EXISTS")
                        print(f"  dropped plain index {clash} on {label}.{prop}")
                    session.run(f"CREATE CONSTRAINT {name} IF NOT EXISTS "
                                f"FOR (n:`{label}`) REQUIRE n.`{prop}` IS UNIQUE")
                    print(f"  created {name} on {label}.{prop}")
                live_pairs = set(live_constraints(session).values())
                still = [f"{l}.{p}" for _, l, p, _ in missing
                         if (l, p) not in live_pairs]
                if still:
                    print(f"\n❌ still missing after --apply: {still}")
                    return 1
                missing = []

            if blocked:
                print("\n⛔ CONSTRAINTS THAT CANNOT BE CREATED — the data already "
                      "violates them. Repair the duplicates, then re-run.")
                return 1
            if missing:
                print("\n❌ DECLARED CONSTRAINTS ARE NOT IN FORCE on this instance. "
                      "Re-run with --apply (or apply neo4j_init.cypher).")
                return 1
            print("\n✅ Every constraint neo4j_init.cypher declares is in force.")
            return 0
    finally:
        driver.close()


if __name__ == "__main__":
    sys.exit(main())
