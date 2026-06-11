#!/usr/bin/env python3
"""Generate schema_init.sql from the live database schema.

Run this after apply.py whenever a new migration is added. The output
replaces schema_init.sql with a fresh, idempotent CREATE TABLE / INDEX file
that exactly matches the current database state — so new installs stay in
sync with the migration chain without hand-editing two files.

Usage:
    uv run --with psycopg2-binary python shared-memory/migrations/generate_schema_init.py
    uv run --with psycopg2-binary python shared-memory/migrations/generate_schema_init.py --dry-run

Reads PG_PASSWORD / PG_CONN from .env at the repo root (same as apply.py).
Writes shared-memory/migrations/schema_init.sql.
"""

import os
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    sys.exit("psycopg2 not available — run with: uv run --with psycopg2-binary python ...")

MIGRATIONS_DIR = Path(__file__).parent
SCHEMA_FILE = MIGRATIONS_DIR / "schema_init.sql"

# Tables to include, in creation order (dependencies first).
TABLES = ["technical_docs", "community_summaries", "neo4j_outbox"]


def _load_env() -> None:
    env_path = MIGRATIONS_DIR.parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def _pg_conn() -> str:
    pg_pass = os.environ.get("PG_PASSWORD", "")
    return os.environ.get(
        "PG_CONN",
        f"postgresql://postgres:{pg_pass}@localhost:5432/agent_data",
    )


def fetch_columns(cur, table: str) -> list[dict]:
    cur.execute("""
        SELECT
            column_name,
            data_type,
            udt_name,
            character_maximum_length,
            is_nullable,
            column_default
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
    """, (table,))
    return [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]


def fetch_indexes(cur, table: str) -> list[dict]:
    cur.execute("""
        SELECT
            indexname,
            indexdef
        FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = %s
        ORDER BY indexname
    """, (table,))
    return [{"name": row[0], "def": row[1]} for row in cur.fetchall()]


def fetch_constraints(cur, table: str) -> list[dict]:
    cur.execute("""
        SELECT
            conname,
            contype,
            pg_get_constraintdef(oid) AS condef
        FROM pg_constraint
        WHERE conrelid = %s::regclass
          AND contype IN ('p', 'u')          -- primary key + unique only
        ORDER BY conname
    """, (table,))
    return [{"name": row[0], "type": row[1], "def": row[2]} for row in cur.fetchall()]


def fetch_extensions(cur) -> list[str]:
    cur.execute("SELECT extname FROM pg_extension WHERE extname <> 'plpgsql' ORDER BY extname")
    return [row[0] for row in cur.fetchall()]


def col_type(col: dict) -> str:
    """Reconstruct the SQL type string from information_schema columns."""
    dt = col["data_type"]
    udt = col["udt_name"]
    if dt == "USER-DEFINED":
        # e.g. vector(1024) — stored as udt_name='vector'; dimensions in atttypmod
        return udt  # will be augmented below with dimensions
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
        # e.g. integer[]
        element = udt.lstrip("_")
        return f"{element.upper()}[]"
    if dt == "timestamp with time zone":
        return "TIMESTAMPTZ"
    return dt.upper()


def fetch_vector_dims(cur, table: str, column: str) -> int | None:
    """Read the vector dimension from pg_attribute.atttypmod."""
    cur.execute("""
        SELECT atttypmod
        FROM pg_attribute
        WHERE attrelid = %s::regclass AND attname = %s
    """, (table, column))
    row = cur.fetchone()
    if row and row[0] > 0:
        return row[0]
    return None


def render_column(cur, table: str, col: dict, pk_cols: set[str]) -> str:
    name = col["column_name"]
    ctype = col_type(col)

    # Augment vector type with dimensions
    if ctype == "vector":
        dims = fetch_vector_dims(cur, table, name)
        if dims:
            ctype = f"vector({dims})"

    # Reconstruct SERIAL from sequences
    default = col["column_default"] or ""
    if "nextval" in default:
        if ctype in ("INTEGER", "int4"):
            ctype = "SERIAL"
        elif ctype in ("BIGINT", "int8"):
            ctype = "BIGSERIAL"
        default = ""

    parts = [f"    {name:<16} {ctype}"]
    if name in pk_cols:
        parts.append("PRIMARY KEY")
    elif col["is_nullable"] == "NO" and not default:
        parts.append("NOT NULL")
    elif col["is_nullable"] == "NO" and default:
        parts.append("NOT NULL")

    if default and "nextval" not in default:
        parts.append(f"DEFAULT {default}")

    # UNIQUE inline only for content_hash (single-column unique with no partial predicate)
    # All other unique constraints come out as separate indexes.

    return " ".join(parts)


def render_table(cur, table: str) -> str:
    cols = fetch_columns(cur, table)
    constraints = fetch_constraints(cur, table)
    pk_cols = set()
    for c in constraints:
        if c["type"] == "p":
            # extract column names from PRIMARY KEY (col1, col2, ...)
            inner = c["def"].replace("PRIMARY KEY (", "").rstrip(")")
            pk_cols = {s.strip() for s in inner.split(",")}

    col_lines = [render_column(cur, table, col, pk_cols) for col in cols]

    lines = [f"CREATE TABLE IF NOT EXISTS {table} ("]
    lines += [line + "," for line in col_lines[:-1]]
    lines += [col_lines[-1]]
    lines += [");"]
    return "\n".join(lines)


def render_indexes(cur, table: str) -> list[str]:
    """
    Emit indexes. Skip the primary key index (handled inline).
    Rewrite as IF NOT EXISTS so the output is idempotent.
    """
    idxs = fetch_indexes(cur, table)
    out = []
    for idx in idxs:
        name = idx["name"]
        defn = idx["def"]
        # Skip auto-generated primary key index
        if name == f"{table}_pkey":
            continue
        # INSERT IF NOT EXISTS
        defn = defn.replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ")
        defn = defn.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ")
        out.append(defn + ";")
    return out


def generate(conn) -> str:
    cur = conn.cursor()

    # Version header
    version_line = f"-- schema_init.sql — full schema (auto-generated {datetime.now(timezone.utc).strftime('%Y-%m-%d')})"

    sections = [
        textwrap.dedent(f"""\
        {version_line}
        --
        -- USE THIS for new installs: creates the complete schema in one shot without
        -- replaying the incremental migration chain. Equivalent to running apply.py
        -- on an empty database. Idempotent (IF NOT EXISTS throughout).
        --
        -- Upgrading an existing install? Use apply.py — it only runs pending migrations.
        --
        -- Regenerate after every new migration:
        --   uv run --with psycopg2-binary python shared-memory/migrations/generate_schema_init.py
        --
        -- Usage:
        --   psql -U postgres agent_data < shared-memory/migrations/schema_init.sql
        """),
    ]

    sections.append("BEGIN;\n")

    # Extensions
    exts = fetch_extensions(cur)
    if exts:
        sections.append("-- ─── Extensions ────────────────────────────────────────────────────────────")
        for ext in exts:
            sections.append(f"CREATE EXTENSION IF NOT EXISTS {ext};\n")

    # Tables + their indexes
    for table in TABLES:
        sections.append(f"-- ─── {table} {'─' * max(1, 75 - len(table))}")
        sections.append(render_table(cur, table))
        idx_lines = render_indexes(cur, table)
        if idx_lines:
            sections.append("")
            sections.extend(idx_lines)
        sections.append("")

    sections.append("COMMIT;")
    return "\n".join(sections) + "\n"


def main() -> None:
    _load_env()
    dry_run = "--dry-run" in sys.argv

    conn = psycopg2.connect(_pg_conn())
    try:
        ddl = generate(conn)
    finally:
        conn.close()

    if dry_run:
        print(ddl)
        return

    SCHEMA_FILE.write_text(ddl)
    print(f"Written: {SCHEMA_FILE}")
    print("Commit schema_init.sql together with the new migration file.")


if __name__ == "__main__":
    main()
