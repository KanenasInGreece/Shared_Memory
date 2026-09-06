#!/usr/bin/env python3
"""Generate schema_init.sql from the migration chain (via a scratch database).

Run this after adding a new migration. The generator spins up a throwaway
database, applies every numbered migration to it (exactly what apply.py does
on a fresh install), introspects the result, then drops the scratch database
and writes schema_init.sql. The output is therefore *equivalent to apply.py
on an empty database by construction* — it can never drift from the migration
chain the way introspecting a long-lived production database would.

Usage:
    uv run --with psycopg2-binary python shared-memory/migrations/generate_schema_init.py
    uv run --with psycopg2-binary python shared-memory/migrations/generate_schema_init.py --dry-run

Reads PG_PASSWORD / PG_CONN from .env at the repo root (same as apply.py).
Requires privileges to CREATE DATABASE / DROP DATABASE (the postgres role has
them by default). Writes shared-memory/migrations/schema_init.sql.
"""

import os
import sys
import textwrap
from pathlib import Path
from urllib.parse import urlparse, urlunparse

try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 not available — run with: uv run --with psycopg2-binary python ...")

MIGRATIONS_DIR = Path(__file__).parent
SCHEMA_FILE = MIGRATIONS_DIR / "schema_init.sql"


def _load_env() -> None:
    # The framework env is shared-memory/.env; the repo root is the FALLBACK.
    # Same candidate order as apply.py — these two are always run back to back
    # (apply, then regenerate), and reading different files made the second half
    # of that pair die on `fe_sendauth: no password supplied` on an install that
    # keeps credentials only where the documented setup puts them.
    candidates = [MIGRATIONS_DIR.parent / ".env", MIGRATIONS_DIR.parent.parent / ".env"]
    env_path = next((p for p in candidates if p.exists()), None)
    if env_path is None:
        return
    for line in env_path.read_text().splitlines():
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


def _pg_conn() -> str:
    pg_pass = os.environ.get("PG_PASSWORD", "")
    return os.environ.get(
        "PG_CONN",
        f"postgresql://postgres:{pg_pass}@localhost:5432/agent_data",
    )


def _dsns() -> tuple[str, str, str]:
    """Return (maintenance_dsn, scratch_dsn, scratch_name).

    Maintenance points at the `postgres` database (for CREATE/DROP DATABASE);
    scratch is a per-process database we build, introspect, then drop.
    """
    u = urlparse(_pg_conn())
    scratch_name = f"smf_schemagen_{os.getpid()}"
    maint = urlunparse(u._replace(path="/postgres"))
    scratch = urlunparse(u._replace(path=f"/{scratch_name}"))
    return maint, scratch, scratch_name


# ── Migration replay (mirrors apply.py) ───────────────────────────────────────

def _numbered_migrations() -> list[Path]:
    """The NNN_*.sql files only — never schema_init.sql or this script."""
    return sorted(MIGRATIONS_DIR.glob("[0-9]*.sql"))


def _apply_migrations(conn) -> None:
    for path in _numbered_migrations():
        with conn:
            with conn.cursor() as cur:
                cur.execute(path.read_text())


# ── Introspection ─────────────────────────────────────────────────────────────

def fetch_tables(cur) -> list[str]:
    """All base tables in public — discovered, not hardcoded, so a migration
    that adds a table is captured automatically."""
    cur.execute("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """)
    return [row[0] for row in cur.fetchall()]


def fetch_columns(cur, table: str) -> list[dict]:
    cur.execute("""
        SELECT column_name, data_type, udt_name, is_nullable, column_default,
               is_identity, identity_generation
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
    """, (table,))
    return [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]


def fetch_indexes(cur, table: str) -> list[dict]:
    cur.execute("""
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = %s
        ORDER BY indexname
    """, (table,))
    return [{"name": row[0], "def": row[1]} for row in cur.fetchall()]


def fetch_pk_columns(cur, table: str) -> set[str]:
    cur.execute("""
        SELECT pg_get_constraintdef(oid) AS condef
        FROM pg_constraint
        WHERE conrelid = %s::regclass AND contype = 'p'
    """, (table,))
    row = cur.fetchone()
    if not row:
        return set()
    inner = row[0].replace("PRIMARY KEY (", "").rstrip(")")
    return {s.strip() for s in inner.split(",")}


def fetch_table_constraints(cur, table: str) -> list[str]:
    """Table-level CHECK constraints, rendered as they were declared.

    Without this the generator silently DROPPED them: the fresh-install fast path
    rebuilt every table from columns and indexes alone, so a constraint that a
    migration had added existed on upgraded deployments and on NO new one. That
    is the worst shape a schema divergence can take — the guarantee holds
    everywhere it was tested and nowhere it was not.

    NOT NULL and PRIMARY KEY are excluded because render_column already emits
    them. FOREIGN KEYS are excluded HERE but not from the file — see
    fetch_foreign_keys, which emits them after every table exists. Inline was
    never an option: tables are rendered in name order, and `project_promotions`
    sorts before the `projects` it references, so an inline REFERENCES would
    break the very install path this file serves.
    """
    cur.execute("""
        SELECT conname, pg_get_constraintdef(oid) AS condef
        FROM pg_constraint
        WHERE conrelid = %s::regclass AND contype = 'c'
        ORDER BY conname
    """, (table,))
    return [f"CONSTRAINT {name} {defn}" for name, defn in cur.fetchall()]


def fetch_foreign_keys(cur) -> list[tuple[str, str, str]]:
    """Every FOREIGN KEY in the schema, as (table, constraint name, definition).

    ⚠ THE GENERATOR USED TO EMIT NONE AT ALL. It rendered primary keys and, since
    the CHECK fix, table CHECKs — and dropped foreign keys on the floor, with a
    comment claiming the schema did not use any. It already did:
    `technical_docs.superseded_by` has referenced `technical_docs(id)` since fact
    supersession shipped, so every fresh install has been missing it while every
    upgraded deployment had it. That is the same divergence the CHECK fix was
    written for, in a second dimension, and it stayed invisible because the only
    thing that reads this file is an install nobody re-inspects.

    Emitted as ALTER TABLE after all tables exist, never inline: tables render in
    name order, and a referencing table can sort before the one it points at.
    """
    cur.execute("""
        SELECT rel.relname, con.conname, pg_get_constraintdef(con.oid)
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
        WHERE con.contype = 'f' AND nsp.nspname = 'public'
        ORDER BY rel.relname, con.conname
    """)
    return [(t, n, d) for t, n, d in cur.fetchall()]


def render_foreign_keys(cur) -> list[str]:
    """ALTER TABLE statements, each guarded so the file stays re-runnable.

    Postgres has no ADD CONSTRAINT IF NOT EXISTS, and this file promises
    idempotency throughout — so the guard is explicit rather than assumed.
    """
    fks = fetch_foreign_keys(cur)
    if not fks:
        return []
    out = [
        "-- ─── Foreign keys ──────────────────────────────────────────────────────────",
        "-- Added after every table exists: a referencing table can sort before its",
        "-- target, so these cannot be inline column constraints.",
        "",
    ]
    for table, name, defn in fks:
        out.append(
            "DO $$ BEGIN\n"
            f"    ALTER TABLE {table} ADD CONSTRAINT {name} {defn};\n"
            "EXCEPTION WHEN duplicate_object THEN NULL;\n"
            "END $$;\n"
        )
    return out


def fetch_functions(cur) -> list[str]:
    """Every function this schema DEFINES, rendered as it was declared.

    ⚠ Extension-owned functions are excluded, and that filter is the whole
    difficulty: `CREATE EXTENSION vector` alone installs ~150 functions into
    `public`, so emitting everything would rebuild pgvector's API by hand and
    then conflict with the extension itself. `pg_depend` with deptype 'e' is
    what distinguishes "installed by an extension" from "written by us".
    """
    cur.execute("""
        SELECT pg_get_functiondef(p.oid)
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND NOT EXISTS (
              SELECT 1 FROM pg_depend d
              WHERE d.objid = p.oid AND d.deptype = 'e'
          )
        ORDER BY p.proname
    """)
    return [row[0] for row in cur.fetchall()]


def fetch_triggers(cur) -> list[tuple[str, str, str]]:
    """(trigger name, table, definition) for every non-internal trigger.

    ⚠ THE GENERATOR EMITTED NONE OF THESE EITHER. That is the third class of
    object it has silently dropped, after CHECKs and foreign keys, and it was
    about to matter: this schema's cross-table alias rule — an alias and a
    canonical project name must never be the same string — is enforceable only
    by a trigger, because no single CHECK can see two tables. Without this it
    would have held on every upgraded deployment and on no new one.

    A NOTE ON WHAT THIS DOES *NOT* FIX, because the first reading was wrong. A
    long-lived deployment may also carry triggers that no migration creates,
    added by hand before the chain existed. Those are LOCAL DRIFT, not a gap in
    this file: the generator builds its scratch database from the migrations, so
    it can only emit what the chain produces, and a hand-made trigger is
    invisible to it by construction. Such a trigger belongs in a migration or
    nowhere — and before assuming it is load-bearing, check whether the
    application already does the same work, because a duplicate is the more
    likely explanation for something nobody wrote down.

    The general lesson is not "add triggers": a generator that renders SOME
    kinds of object will keep missing the kinds nobody has needed yet, so the
    check that matters is building the database from this file and comparing it
    against a migrated one.
    """
    cur.execute("""
        SELECT t.tgname, c.relname, pg_get_triggerdef(t.oid)
        FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE NOT t.tgisinternal AND n.nspname = 'public'
        ORDER BY c.relname, t.tgname
    """)
    return [(r[0], r[1], r[2]) for r in cur.fetchall()]


def render_functions(cur) -> list[str]:
    """Every schema-defined function, emitted BEFORE any table or index.

    ⛔ THE POSITION IS THE POINT, and it was wrong until v0.9.52. Functions used
    to be emitted at the END of the file, after every table and every index, on
    the reasoning that a TRIGGER cannot be created before the function it names.
    That reasoning is right about triggers and silently wrong about INDEXES: an
    index whose expression CALLS a function needs the function to already exist,
    so the moment this schema grew its first functional index the regenerated
    file aborted on `function ... does not exist` — and because the whole file is
    one transaction, a fresh install created NOTHING while an upgraded
    deployment was fine. The failure lives only on the path nobody re-inspects.

    ⚠ The fix is a SPLIT, not a hoist, because the two orderings pull opposite
    ways: a function must precede an index that calls it, and a trigger must
    follow the table it is attached to. So `generate()` emits functions →
    tables+indexes → foreign keys → triggers, and this function and
    `render_triggers` are the two halves that used to be one.
    """
    functions = fetch_functions(cur)
    if not functions:
        return []
    out = [
        "-- ─── Functions ─────────────────────────────────────────────────────────────",
        "-- FIRST, before any table or index: an index expression may CALL one of",
        "-- these, and the whole file is a single transaction.",
        "",
    ]
    # pg_get_functiondef already emits CREATE OR REPLACE, so this is
    # re-runnable without a guard.
    out.extend(f"{fn};\n" for fn in functions)
    return out


def render_triggers(cur) -> list[str]:
    """Every trigger, emitted LAST — after the tables they are attached to and
    after the functions they call."""
    triggers = fetch_triggers(cur)
    if not triggers:
        return []
    out = [
        "-- ─── Triggers ──────────────────────────────────────────────────────────────",
        "-- LAST: after the tables they are attached to and the functions they call.",
        "-- DROP-then-CREATE because Postgres has no CREATE TRIGGER IF NOT EXISTS and",
        "-- this file promises idempotency.",
        "",
    ]
    for name, table, defn in triggers:
        out.append(f"DROP TRIGGER IF EXISTS {name} ON {table};\n{defn};\n")
    return out


def fetch_extensions(cur) -> list[str]:
    cur.execute("SELECT extname FROM pg_extension WHERE extname <> 'plpgsql' ORDER BY extname")
    return [row[0] for row in cur.fetchall()]


def fetch_vector_dims(cur, table: str, column: str) -> int | None:
    """pgvector stores the dimension directly in atttypmod (unlike varchar)."""
    cur.execute("""
        SELECT atttypmod
        FROM pg_attribute
        WHERE attrelid = %s::regclass AND attname = %s
    """, (table, column))
    row = cur.fetchone()
    return row[0] if row and row[0] > 0 else None


# ── Rendering ──────────────────────────────────────────────────────────────────

def col_type(cur, table: str, col: dict) -> str:
    dt = col["data_type"]
    udt = col["udt_name"]
    if dt == "USER-DEFINED":
        if udt == "vector":
            dims = fetch_vector_dims(cur, table, col["column_name"])
            return f"vector({dims})" if dims else "vector"
        return udt
    if dt == "integer":
        return "INTEGER"
    if dt == "bigint":
        return "BIGINT"
    if dt == "boolean":
        return "BOOLEAN"
    if dt in ("text", "character varying"):
        return "TEXT"
    if dt == "jsonb":
        return "JSONB"
    if dt == "ARRAY":
        return f"{udt.lstrip('_').upper()}[]"
    if dt == "timestamp with time zone":
        return "TIMESTAMPTZ"
    return dt.upper()


def render_column(cur, table: str, col: dict, pk_cols: set[str]) -> str:
    name = col["column_name"]
    ctype = col_type(cur, table, col)
    default = col["column_default"] or ""

    # Collapse SERIAL/BIGSERIAL back from the sequence default.
    if "nextval" in default:
        ctype = "BIGSERIAL" if ctype == "BIGINT" else "SERIAL"
        default = ""

    # ⚠ AN IDENTITY COLUMN HAS NO `column_default` — it is generated, not
    # defaulted, and information_schema reports it in two separate columns. A
    # renderer that reads only the default emits a bare `BIGINT PRIMARY KEY`,
    # which is valid DDL, applies without error, and leaves a fresh install
    # unable to INSERT a single row: every write must then supply the key it was
    # the database's job to issue. That is the THIRD class of DDL this generator
    # has been found dropping — after every CHECK and every FOREIGN KEY — and
    # all three shared one shape: invisible to the whole suite, because the only
    # thing that reads this file is an install nobody re-inspects.
    identity = ""
    if col.get("is_identity") == "YES":
        identity = f"GENERATED {col.get('identity_generation') or 'BY DEFAULT'} AS IDENTITY"

    parts = [f"    {name:<16} {ctype}"]
    if name in pk_cols:
        parts.append("PRIMARY KEY")
    elif col["is_nullable"] == "NO":
        parts.append("NOT NULL")
    if identity:
        parts.append(identity)
    if default and "nextval" not in default:
        parts.append(f"DEFAULT {default}")
    return " ".join(parts)


def render_table(cur, table: str) -> str:
    cols = fetch_columns(cur, table)
    pk_cols = fetch_pk_columns(cur, table)
    col_lines = [render_column(cur, table, col, pk_cols) for col in cols]
    col_lines += [f"    {c}" for c in fetch_table_constraints(cur, table)]
    body = ",\n".join(col_lines)
    return f"CREATE TABLE IF NOT EXISTS {table} (\n{body}\n);"


def render_indexes(cur, table: str) -> list[str]:
    out = []
    for idx in fetch_indexes(cur, table):
        if idx["name"] == f"{table}_pkey":   # PK index is implied by the column def
            continue
        defn = idx["def"]
        defn = defn.replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ")
        defn = defn.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ")
        out.append(defn + ";")
    return out


def generate(conn) -> str:
    cur = conn.cursor()
    sections = [
        textwrap.dedent("""\
        -- schema_init.sql — full schema for a fresh install.
        --
        -- AUTO-GENERATED from the migration chain — do NOT edit by hand. The
        -- generator applies every NNN_*.sql migration to a throwaway database and
        -- introspects the result, so this file is equivalent to running apply.py
        -- on an empty database by construction.
        --
        -- USE THIS for new installs: creates the complete schema in one shot.
        -- Idempotent (IF NOT EXISTS throughout).
        --
        -- Upgrading an existing install? Use apply.py — it only runs pending migrations.
        --
        -- Regenerate after every new migration:
        --   uv run --with psycopg2-binary python shared-memory/migrations/generate_schema_init.py
        --
        -- EMBEDDING DIMENSION: vector columns default to 1024-dim for BGE-M3. To use
        -- a different model, change vector(1024) in 000_base_schema.sql, then
        -- regenerate. The invariant is that ALL agents share ONE model via the
        -- gateway — not the specific dimension.
        --
        -- Also run neo4j_init.cypher to initialise the Neo4j constraint set.
        --
        -- Usage:
        --   psql -U postgres agent_data < shared-memory/migrations/schema_init.sql
        """),
        "BEGIN;\n",
    ]

    exts = fetch_extensions(cur)
    if exts:
        sections.append("-- ─── Extensions ────────────────────────────────────────────────────────────")
        for ext in exts:
            sections.append(f"CREATE EXTENSION IF NOT EXISTS {ext};\n")

    # ⛔ SECTION ORDER IS LOAD-BEARING: functions → tables+indexes → foreign
    # keys → triggers. A function must exist before an index whose expression
    # calls it; a trigger must come after the table it is on. See
    # render_functions() for the fresh-install failure the old order produced.
    sections.extend(render_functions(cur))

    for table in fetch_tables(cur):
        sections.append(f"-- ─── {table} {'─' * max(1, 75 - len(table))}")
        sections.append(render_table(cur, table))
        idx_lines = render_indexes(cur, table)
        if idx_lines:
            sections.append("")
            sections.extend(idx_lines)
        sections.append("")

    sections.extend(render_foreign_keys(cur))
    sections.extend(render_triggers(cur))

    sections.append("COMMIT;")
    return "\n".join(sections) + "\n"


def main() -> None:
    _load_env()
    dry_run = "--dry-run" in sys.argv
    maint_dsn, scratch_dsn, scratch_name = _dsns()

    # 1. Create a clean scratch database.
    maint = psycopg2.connect(maint_dsn)
    maint.autocommit = True
    try:
        with maint.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{scratch_name}"')
            cur.execute(f'CREATE DATABASE "{scratch_name}"')
    finally:
        maint.close()

    try:
        # 2. Apply the migration chain, then introspect.
        scratch = psycopg2.connect(scratch_dsn)
        try:
            _apply_migrations(scratch)
            ddl = generate(scratch)
        finally:
            scratch.close()
    finally:
        # 3. Always drop the scratch database.
        maint = psycopg2.connect(maint_dsn)
        maint.autocommit = True
        try:
            with maint.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{scratch_name}"')
        finally:
            maint.close()

    if dry_run:
        print(ddl)
        return

    SCHEMA_FILE.write_text(ddl)
    print(f"Written: {SCHEMA_FILE}")
    print("Commit schema_init.sql together with the new migration file.")


if __name__ == "__main__":
    main()
