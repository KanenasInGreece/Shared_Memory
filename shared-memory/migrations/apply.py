#!/usr/bin/env python3
"""Apply a SQL migration file against the configured Postgres instance.

Usage:
    uv run --with psycopg2-binary python shared-memory/migrations/apply.py 001_multiagent_schema.sql
    uv run --with psycopg2-binary python shared-memory/migrations/apply.py  # runs all pending

Reads PG_PASSWORD / PG_CONN from the environment (or .env at repo root).
"""

import os
import sys
import glob
from pathlib import Path

try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 not available — run with: uv run --with psycopg2-binary python ...")

MIGRATIONS_DIR = Path(__file__).parent

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
    """Build the DSN AFTER _load_env() has populated the environment. Computing
    it at module import (before _load_env runs in main) froze an empty password
    into the DSN and broke auth when PG_PASSWORD lived only in .env."""
    pg_pass = os.environ.get("PG_PASSWORD", "")
    return os.environ.get(
        "PG_CONN",
        f"postgresql://postgres:{pg_pass}@localhost:5432/agent_data",
    )


def apply(sql_file: Path) -> None:
    sql = sql_file.read_text()
    conn = psycopg2.connect(_pg_conn())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
        print(f"  applied: {sql_file.name}")
    finally:
        conn.close()


def main() -> None:
    _load_env()

    if len(sys.argv) > 1:
        targets = [MIGRATIONS_DIR / sys.argv[1]]
    else:
        targets = sorted(MIGRATIONS_DIR.glob("*.sql"))

    if not targets:
        print("No migration files found.")
        return

    for path in targets:
        if not path.exists():
            sys.exit(f"File not found: {path}")
        print(f"Applying {path.name} ...")
        apply(path)

    print("Done.")


if __name__ == "__main__":
    main()
