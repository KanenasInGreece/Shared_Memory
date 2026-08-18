#!/usr/bin/env python3
"""Prove that a FRESH install built from schema_init.sql matches the live schema.

WHY THIS EXISTS. ``schema_init.sql`` is the fast path for a new deployment: it is
applied instead of replaying every migration. Nothing else reads it, so when it
is wrong the only person who finds out is a stranger setting the framework up
for the first time — and they have no baseline to compare against. A guarantee
that holds on every upgraded deployment and on no new one is the worst shape a
schema divergence can take.

``generate_schema_init.py`` introspects the live database and renders it, and it
has now silently dropped three whole classes of object:

  * every CHECK constraint      (found late, after it had already shipped)
  * every FOREIGN KEY           (found later still, with a code comment
                                 asserting the schema had none while one had
                                 existed for months)
  * every FUNCTION and TRIGGER  (found later again)
  * every COLUMN's underlying TYPE (found by the migration 033 security
                                 review — `col_type()` in
                                 generate_schema_init.py collapses `character
                                 varying(n)` to unconstrained `TEXT`, so a
                                 migration adding a genuinely length-capped
                                 VARCHAR column would render, on a fresh
                                 install, as a column with no cap at all:
                                 valid DDL, no error, and a row the live
                                 install would reject sails straight through)

Each omission was invisible to the entire test suite. So the generator is not
trusted; it is CHECKED, by building a database from its output and diffing.

WHAT THIS DOES TO YOUR DATA: NOTHING. It creates a throwaway database, applies
schema_init.sql to that, reads catalogues from both, drops the throwaway, and
exits. The live database is opened READ-ONLY and is never written, never
migrated, and never dropped. The throwaway name is generated with a fixed
prefix and a timestamp, and this script refuses to drop anything that does not
carry that prefix.

⚠ TWO REFINEMENTS LEARNED RUNNING THIS BY HAND:

  * A UNIQUE constraint legitimately reports as "missing", because the generator
    re-emits it as ``CREATE UNIQUE INDEX``. Functionally identical — ``ON
    CONFLICT`` still works. So unique constraints are reconciled against the
    fresh database's INDEXES before anything is reported, i.e. diff BEHAVIOUR,
    not catalogue rows.
  * Comparing DDL text is not the same as comparing what the database will
    enforce. Constraint definitions are normalised (whitespace, and the
    generated names Postgres assigns) before comparison.

Usage:
    uv run --with psycopg2-binary python shared-memory/migrations/verify_schema_init.py
    ... --keep      keep the throwaway database for inspection
Exit status is 1 when the fresh install would differ, so CI can gate on it.
"""
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

HERE = Path(__file__).resolve().parent
SCHEMA_INIT = HERE / "schema_init.sql"

# Every throwaway database this script creates carries this prefix, and it
# refuses to drop a name lacking it. The guard is the point: a bug in name
# construction must fail closed rather than drop the corpus.
SCRATCH_PREFIX = "sm_schema_verify_"

# LEGITIMATELY absent from schema_init.sql — not a divergence.
#
# `schema_migrations` is the migration ledger, and `apply.py` owns it: it runs
# its own CREATE TABLE IF NOT EXISTS before reading, so a fresh install gets the
# ledger from apply.py rather than from the schema dump. Emitting it here would
# also be actively wrong — a dumped ledger would arrive either empty (and be
# indistinguishable from a database that predates tracking) or pre-stamped with
# THIS deployment's history.
#
# Anything added to this set needs the reason written beside it. A check that
# reports a known-benign difference on every run is a check people learn to
# ignore, which costs more than the check is worth.
EXPECTED_ABSENT_TABLES = {"schema_migrations"}


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
            os.environ.setdefault(key.strip(), val.strip())

def _dsn(dbname: str) -> str:
    user = os.environ.get("PG_USER", "postgres")
    host = os.environ.get("PG_HOST", "localhost")
    port = os.environ.get("PG_PORT", "5432")
    pw = os.environ.get("PG_PASSWORD", "")
    return f"postgresql://{user}:{pw}@{host}:{port}/{dbname}"


def _norm(text: str) -> str:
    """Normalise a constraint definition so two spellings of one rule compare
    equal: collapse whitespace and drop the ::type casts Postgres echoes back."""
    return re.sub(r"\s+", " ", (text or "")).strip()


def _constraints(conn) -> dict:
    """{(table, contype, normalised_definition)} — keyed on what the database
    will ENFORCE, never on the constraint's generated name, because Postgres
    names an unnamed constraint differently in a differently-built database."""
    cur = conn.cursor()
    cur.execute("""
        SELECT c.conrelid::regclass::text AS tbl,
               c.contype,
               pg_get_constraintdef(c.oid)  AS def
          FROM pg_constraint c
          JOIN pg_namespace n ON n.oid = c.connamespace
         WHERE n.nspname = 'public'
    """)
    out = {}
    for tbl, contype, definition in cur.fetchall():
        out.setdefault((tbl, contype), set()).add(_norm(definition))
    return out


def _unique_indexes(conn) -> set:
    """(table, normalised column list) for every UNIQUE index. A UNIQUE
    CONSTRAINT in one database and a UNIQUE INDEX in the other enforce the same
    thing, and the generator re-emits constraints as indexes."""
    cur = conn.cursor()
    cur.execute("""
        SELECT t.relname AS tbl, pg_get_indexdef(i.indexrelid) AS def
          FROM pg_index i
          JOIN pg_class t ON t.oid = i.indrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
         WHERE n.nspname = 'public' AND i.indisunique
    """)
    out = set()
    for tbl, definition in cur.fetchall():
        cols = re.search(r"\((.*)\)\s*(WHERE .*)?$", definition or "")
        out.add((tbl, _norm(cols.group(1)) if cols else _norm(definition)))
    return out


def _routines(conn) -> set:
    cur = conn.cursor()
    cur.execute("""SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                    WHERE n.nspname = 'public'""")
    return {r[0] for r in cur.fetchall()}


def _triggers(conn) -> set:
    cur = conn.cursor()
    cur.execute("""SELECT c.relname, t.tgname FROM pg_trigger t
                     JOIN pg_class c ON c.oid = t.tgrelid
                     JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND NOT t.tgisinternal""")
    return set(cur.fetchall())


def _tables(conn) -> set:
    cur = conn.cursor()
    cur.execute("""SELECT tablename FROM pg_tables WHERE schemaname = 'public'""")
    return {r[0] for r in cur.fetchall()}


def _column_generation(conn) -> dict:
    """{(table, column): "identity BY DEFAULT" | "default nextval(…)" | ""}.

    WHO ISSUES A KEY IS PART OF THE SCHEMA, and until this check existed nothing
    compared it. Constraints, functions and triggers were all diffed while the
    column-level half — IDENTITY and DEFAULT — was not, so a generator that
    rendered `id BIGINT PRIMARY KEY` where the live column is
    `GENERATED BY DEFAULT AS IDENTITY` produced a file that applies without
    error and leaves the install unable to INSERT a row. Valid DDL, matching
    constraints, and a database no write path can use.

    Only the two generating forms are compared, deliberately: an ordinary
    literal default is cosmetic drift, while a MISSING generator is a fresh
    install that cannot write.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT table_name, column_name, is_identity, identity_generation,
               column_default
          FROM information_schema.columns
         WHERE table_schema = 'public'
    """)
    out = {}
    for table, column, is_identity, generation, default in cur.fetchall():
        if is_identity == "YES":
            out[(table, column)] = f"identity {generation or 'BY DEFAULT'}"
        elif default and "nextval" in default:
            out[(table, column)] = "sequence default"
    return out


def _column_types(conn) -> dict:
    """{(table, column): (data_type, character_maximum_length)} for every
    column.

    Diffing PRESENCE (`_column_generation`, constraints, functions, triggers)
    is not the same as diffing the TYPE a column actually enforces.
    `generate_schema_init.py`'s `col_type()` collapses `character varying` to
    plain `TEXT` for every ordinary column — dropping the length — because
    house style has never NEEDED the cap rendered before. That collapse is
    silent and was never a problem while every VARCHAR-capped column in this
    schema happened to get replaced by TEXT anyway; the day a migration adds
    one that must actually cap its length, a regenerated schema_init.sql
    would apply without error and simply not enforce it. Comparing the type
    is what turns that into a reported divergence instead of a row a fresh
    install silently accepts and a live one would have rejected.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT table_name, column_name, data_type, character_maximum_length
          FROM information_schema.columns
         WHERE table_schema = 'public'
    """)
    return {(t, c): (dt, length) for t, c, dt, length in cur.fetchall()}


def main() -> int:
    _load_env()
    keep = "--keep" in sys.argv
    live_db = os.environ.get("PG_DATABASE", "agent_data")
    if not SCHEMA_INIT.is_file():
        print(f"schema_init.sql not found at {SCHEMA_INIT}", file=sys.stderr)
        return 2

    scratch = SCRATCH_PREFIX + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    admin = psycopg2.connect(_dsn(os.environ.get("PG_MAINTENANCE_DB", "postgres")))
    admin.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    acur = admin.cursor()
    acur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(scratch)))
    print(f"fresh database  : {scratch}")
    print(f"live database   : {live_db}  (opened read-only, never written)")

    try:
        fresh = psycopg2.connect(_dsn(scratch))
        fresh.cursor().execute(SCHEMA_INIT.read_text())
        fresh.commit()

        live = psycopg2.connect(_dsn(live_db))
        # Belt and braces: this session cannot write even if a later edit tried.
        live.cursor().execute("SET default_transaction_read_only = on")

        problems = []

        missing_tables = _tables(live) - _tables(fresh) - EXPECTED_ABSENT_TABLES
        if missing_tables:
            problems.append(f"TABLES missing from schema_init.sql: {sorted(missing_tables)}")

        live_c, fresh_c = _constraints(live), _constraints(fresh)
        # Constraints on an expected-absent table are absent for the same reason.
        live_c = {k: v for k, v in live_c.items()
                  if k[0].split(".")[-1] not in EXPECTED_ABSENT_TABLES}
        fresh_uniques = _unique_indexes(fresh)
        labels = {"c": "CHECK", "f": "FOREIGN KEY", "p": "PRIMARY KEY", "u": "UNIQUE"}
        for key, defs in sorted(live_c.items()):
            tbl, contype = key
            missing = defs - fresh_c.get(key, set())
            if contype == "u" and missing:
                # Reconcile against the fresh side's UNIQUE INDEXES before reporting.
                still = set()
                for d in missing:
                    cols = re.search(r"UNIQUE\s*\((.*)\)", d)
                    if not (cols and (tbl, _norm(cols.group(1))) in fresh_uniques):
                        still.add(d)
                missing = still
            if missing:
                problems.append(
                    f"{labels.get(contype, contype)} on {tbl} missing from a fresh install:\n"
                    + "\n".join(f"      {d}" for d in sorted(missing)))

        live_gen, fresh_gen = _column_generation(live), _column_generation(fresh)
        for (tbl, col), how in sorted(live_gen.items()):
            if tbl in EXPECTED_ABSENT_TABLES:
                continue
            if fresh_gen.get((tbl, col)) != how:
                problems.append(
                    f"KEY GENERATION on {tbl}.{col} missing from a fresh install: "
                    f"live is {how}, fresh is {fresh_gen.get((tbl, col)) or 'nothing'}"
                    f" — every INSERT would have to supply the key itself")

        live_types, fresh_types = _column_types(live), _column_types(fresh)
        for (tbl, col), (dtype, length) in sorted(live_types.items()):
            if tbl in EXPECTED_ABSENT_TABLES:
                continue
            fresh_val = fresh_types.get((tbl, col))
            if fresh_val is None:
                # A column missing entirely is a different, unrelated
                # problem — nothing in this repo diffs column SETS yet, and
                # conflating the two would blur which fix is actually owed.
                continue
            if (dtype, length) != fresh_val:
                fresh_dtype, fresh_length = fresh_val
                live_render = f"{dtype}({length})" if length else dtype
                fresh_render = f"{fresh_dtype}({fresh_length})" if fresh_length else fresh_dtype
                problems.append(
                    f"COLUMN TYPE on {tbl}.{col} differs: live is {live_render}, "
                    f"fresh is {fresh_render}")

        missing_routines = _routines(live) - _routines(fresh)
        if missing_routines:
            problems.append(f"FUNCTIONS missing: {sorted(missing_routines)}")
        missing_triggers = _triggers(live) - _triggers(fresh)
        if missing_triggers:
            problems.append(f"TRIGGERS missing: {sorted(missing_triggers)}")

        # ⚠ COUNT WHAT WAS CHECKED, NOT WHAT EXISTS. Printing the raw totals
        # rendered as "tables 14/15" — which reads as ONE MISSING TABLE from a
        # tool whose whole job is to prove nothing is missing. It cost an
        # investigation twice, both times ending at the same answer: the
        # fifteenth table is `schema_migrations`, apply.py's own ledger, which
        # schema_init.sql is CORRECT never to carry (see EXPECTED_ABSENT_TABLES).
        # The comparison already excluded it; only this line did not, so the
        # summary contradicted the verdict directly beneath it. A number that
        # has to be explained every time is a defect in the instrument.
        live_tables    = _tables(live) - EXPECTED_ABSENT_TABLES
        fresh_tables   = _tables(fresh) - EXPECTED_ABSENT_TABLES
        absent_note    = (f"  (+{len(EXPECTED_ABSENT_TABLES)} expected-absent: "
                          f"{', '.join(sorted(EXPECTED_ABSENT_TABLES))} — apply.py's "
                          f"ledger, never dumped)") if EXPECTED_ABSENT_TABLES else ""
        print(f"\ntables {len(fresh_tables)}/{len(live_tables)} · "
              f"functions {len(_routines(fresh))}/{len(_routines(live))} · "
              f"triggers {len(_triggers(fresh))}/{len(_triggers(live))} (fresh/live)"
              f"{absent_note}")

        if problems:
            print("\n❌ A FRESH INSTALL WOULD DIFFER FROM THIS DATABASE:\n")
            for p in problems:
                print(f"  - {p}")
            print("\nRegenerate schema_init.sql and re-run. Do NOT ship until this is empty.")
            return 1
        print("\n✅ A fresh install from schema_init.sql matches the live schema.")
        return 0
    finally:
        try:
            live.close()
        except Exception:
            pass
        try:
            fresh.close()
        except Exception:
            pass
        if keep:
            print(f"\n--keep: throwaway database {scratch} left in place.")
        else:
            # Refuses to drop anything not built by this script.
            assert scratch.startswith(SCRATCH_PREFIX), "refusing to drop a non-scratch database"
            acur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(scratch)))
        admin.close()


if __name__ == "__main__":
    sys.exit(main())
