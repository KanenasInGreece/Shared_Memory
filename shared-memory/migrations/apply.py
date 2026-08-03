#!/usr/bin/env python3
"""Apply pending SQL migrations against the configured Postgres instance.

Usage:
    uv run --with psycopg2-binary python shared-memory/migrations/apply.py            # all pending
    uv run --with psycopg2-binary python shared-memory/migrations/apply.py 023_x.sql  # one file
    uv run --with psycopg2-binary python shared-memory/migrations/apply.py --status   # show state
    uv run --with psycopg2-binary python shared-memory/migrations/apply.py --adopt    # see below

⚠ THIS TOOL USED TO RE-RUN EVERY MIGRATION, EVERY TIME.

It globbed `[0-9]*.sql` and executed all of them on each invocation. Its own
docstring claimed it "runs all pending" while nothing anywhere recorded what had
been applied, so "pending" silently meant "all of them". Most migrations tolerate
that — `IF NOT EXISTS`, `ON CONFLICT DO NOTHING` — which is exactly why it went
unnoticed for twenty-two migrations. But tolerance is not idempotency, and one
migration carried a `DELETE` whose correctness depended on a schema that a LATER
migration changed. Re-running it destroyed 12 live community summaries.

The lesson generalises past that one file: **a migration is written against the
schema as it was at that moment.** Re-running it later runs it against a schema
it was never written for. So the fix is not "make every migration re-runnable",
which is not achievable — it is to run each one exactly once.

THE DATABASE IS ITS OWN LEDGER. `schema_migrations` is a table *in* the database
being migrated — not a file beside the migrations, not state in the repo. That is
what makes the answer to "which migrations has this database had?" a property of
the database itself, so it stays correct across every way a database can move:
restore a backup and the ledger comes back with it, already agreeing with the
schema it describes; clone a deployment and the copy knows its own version; point
the tool at a different host and it reads that host's state, not the last one's.
A ledger kept anywhere else is a second source of truth that can disagree with
the schema it claims to describe — which is the failure this whole file is about.

A migration and its ledger row are written in ONE transaction, so a half-applied
migration can never be recorded as done and then skipped forever after.

ADOPTING AN EXISTING DATABASE. A database created before this ledger has no
record of its migrations — but it has certainly had all of them applied, because
the previous tool ran every migration on every invocation. `--adopt` records the
migration files currently present as already applied, WITHOUT running them. Run
it once per pre-existing deployment, and run it BEFORE adding any new migration
file: anything present at adopt time is marked done without ever having run. A
fresh database needs none of this — with no framework tables present every
migration is genuinely pending, and they are applied in order.
"""

import os
import sys
from pathlib import Path

try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 not available — run with: uv run --with psycopg2-binary python ...")

MIGRATIONS_DIR = Path(__file__).parent

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""

# Presence of this table means the framework schema already exists, so a missing
# ledger means the database predates tracking rather than being a fresh install.
_FRAMEWORK_TABLE = "technical_docs"


def _load_env() -> None:
    # Framework env lives at shared-memory/.env; the repo-root path is the
    # pre-0.6 fallback — same resolution order as the gateway (hive_mind_proxy).
    candidates = [MIGRATIONS_DIR.parent / ".env", MIGRATIONS_DIR.parent.parent / ".env"]
    env_path = next((p for p in candidates if p.exists()), None)
    if env_path is None:
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def _pg_conn() -> str:
    """Build the DSN AFTER _load_env() has populated the environment. Computing
    it at module import (before _load_env runs in main) froze an empty password
    into the DSN and broke auth when PG_PASSWORD lived only in .env."""
    pg_pass = os.environ.get("PG_PASSWORD", "")
    return os.environ.get(
        "PG_CONN",
        f"postgresql://postgres:{pg_pass}@localhost:5432/agent_data",
    )


def migration_files() -> list[Path]:
    """Numbered migrations only — never schema_init.sql (the generated
    fresh-install snapshot, which shares this directory)."""
    return sorted(MIGRATIONS_DIR.glob("[0-9]*.sql"))


def pending(files: list[Path], latest: str | None) -> list[Path]:
    """The migrations after the point the database says it has reached.

    The database records how far it has got; this resumes from there. Selection
    is by POSITION, not set membership: everything ordering after `latest` runs,
    everything at or before it is already in the schema by definition.

    Pure — no I/O — so the rule that replaced "everything, every time" is
    directly testable.

    ⚠ The consequence, stated because it is a real constraint rather than an
    oversight: a file numbered BELOW the mark is treated as already applied and
    will not run. Migration numbers must therefore only ever increase. Inserting
    a lower number after the fact is not a thing the tool can honour — the schema
    has already moved past that point, which is exactly why re-running it there
    would be unsafe.
    """
    if latest is None:
        return list(files)
    return [f for f in files if f.name > latest]


def latest_applied(cur) -> str | None:
    """How far this database has got — the field apply resumes from."""
    cur.execute("SELECT max(filename) FROM schema_migrations")
    row = cur.fetchone()
    return row[0] if row else None


def _applied(cur) -> set[str]:
    """Full history, for --status and for reporting. The selection rule uses
    latest_applied; this is the audit trail behind it."""
    cur.execute("SELECT filename FROM schema_migrations")
    return {r[0] for r in cur.fetchall()}


def _table_exists(cur, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (name,))
    return bool(cur.fetchone()[0])


def apply(conn, sql_file: Path) -> None:
    """Run one migration and record it IN THE SAME TRANSACTION.

    Both or neither: a migration that fails half way must not leave a ledger row
    claiming success, because every later run would skip it and the schema would
    stay permanently short of it.
    """
    sql = sql_file.read_text()
    with conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)"
                " ON CONFLICT (filename) DO NOTHING",
                (sql_file.name,),
            )
    print(f"  applied: {sql_file.name}")


def main() -> int:
    _load_env()
    args = sys.argv[1:]
    adopt = "--adopt" in args
    status = "--status" in args
    force = "--force" in args
    named = [a for a in args if not a.startswith("--")]

    files = migration_files()
    if not files:
        print("No migration files found.")
        return 0

    conn = psycopg2.connect(_pg_conn())
    try:
        with conn.cursor() as cur:
            framework = _table_exists(cur, _FRAMEWORK_TABLE)
        with conn:
            with conn.cursor() as cur:
                cur.execute(LEDGER_DDL)
        with conn.cursor() as cur:
            applied = _applied(cur)
            latest = latest_applied(cur)

        if adopt:
            with conn:
                with conn.cursor() as cur:
                    for f in files:
                        cur.execute(
                            "INSERT INTO schema_migrations (filename) VALUES (%s)"
                            " ON CONFLICT (filename) DO NOTHING", (f.name,))
            print(f"Adopted {len(files)} migration(s) as already applied, "
                  f"WITHOUT running them:")
            for f in files:
                print(f"  {f.name}")
            print(f"\nThis database is now at {files[-1].name}. Add new migration "
                  f"files only AFTER this point — anything present now is "
                  f"recorded as done.")
            return 0

        if status:
            todo = pending(files, latest)
            print(f"this database is at: {latest or '(nothing applied)'}")
            print(f"ledger: {len(applied)} applied, {len(todo)} pending")
            for f in files:
                print(f"  [{'x' if f.name in applied else ' '}] {f.name}")
            return 0

        # A populated database with an EMPTY ledger has had every migration
        # applied already, but do not GUESS: silently adopting would skip a
        # genuinely new file, and silently running would repeat the exact failure
        # this ledger exists to prevent. Make the operator choose, once.
        #
        # ⚠ The condition is "the ledger is empty", NOT "the ledger table is
        # missing". Those are different states and the difference bites: a run
        # that created the table and then failed before recording anything, or a
        # ledger cleared by hand, leaves the table present and empty — and keying
        # on the table's absence let that fall straight through to "fresh
        # install", which re-runs every migration against a populated database.
        # Found by testing the refusal path itself; the schema survived only
        # because migration 002's dedup is separately guarded.
        if framework and not applied:
            print(
                "This database predates migration tracking: the framework schema "
                "exists but no migration ledger does.\n\n"
                "The previous tool re-ran every migration on every invocation, so "
                "these migrations HAVE been applied. Record that once, without "
                "running them again:\n\n"
                "    ... apply.py --adopt\n\n"
                "Then re-run this command. Re-running the migrations instead is "
                "NOT safe: each was written against the schema as it stood at the "
                "time, and one deletes rows on a key a later migration changed.",
                file=sys.stderr)
            return 2

        targets = [MIGRATIONS_DIR / n for n in named] if named else pending(files, latest)

        if named:
            for path in targets:
                if not path.exists():
                    sys.exit(f"File not found: {path}")
                if path.name in applied and not force:
                    print(f"  already applied, skipping: {path.name} "
                          f"(--force runs it again)")
                    continue
                print(f"Applying {path.name} ...")
                apply(conn, path)
            print("Done.")
            return 0

        if not targets:
            print(f"Up to date at {latest} — {len(applied)} migration(s) applied.")
            return 0
        for path in targets:
            print(f"Applying {path.name} ...")
            apply(conn, path)
        print("Done.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
