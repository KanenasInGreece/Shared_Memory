"""Read-only: count the facts that still carry a top-level `domain` key, or with --list print their ids.

The guard is migration 044's, word for word. Prints numbers only; update_framework.sh reads the count after the restart.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apply as apply_mod  # noqa: E402

GUARD = """jsonb_typeof(t.metadata) = 'object'
   AND lower(regexp_replace(coalesce(t.metadata->>'type', ''), '^\\s+|\\s+$', '', 'g'))
       NOT IN ('decision', 'retrospective')
   AND NOT coalesce(jsonb_typeof(t.metadata->'decision') = 'object'
                    AND ((SELECT jsonb_agg(v ORDER BY o) FROM (SELECT v, min(ord) o FROM (
          SELECT regexp_replace(e #>> '{}', '^\\s+|\\s+$', '', 'g') v, ord
            FROM jsonb_array_elements(CASE jsonb_typeof(t.metadata->'decision'->'domain') WHEN 'array' THEN t.metadata->'decision'->'domain'
                 WHEN 'string' THEN jsonb_build_array(t.metadata->'decision'->'domain') ELSE '[]'::jsonb END)
                 WITH ORDINALITY a(e, ord)
           WHERE jsonb_typeof(e) = 'string') q WHERE v <> '' GROUP BY v) g) IS NOT NULL
                         OR (SELECT jsonb_agg(v ORDER BY o) FROM (SELECT v, min(ord) o FROM (
          SELECT regexp_replace(e #>> '{}', '^\\s+|\\s+$', '', 'g') v, ord
            FROM jsonb_array_elements(CASE jsonb_typeof(t.metadata->'decision'->'domains') WHEN 'array' THEN t.metadata->'decision'->'domains'
                 WHEN 'string' THEN jsonb_build_array(t.metadata->'decision'->'domains') ELSE '[]'::jsonb END)
                 WITH ORDINALITY a(e, ord)
           WHERE jsonb_typeof(e) = 'string') q WHERE v <> '' GROUP BY v) g) IS NOT NULL), false)"""

COUNT_SQL = "SELECT count(*) FROM technical_docs t WHERE " + GUARD + " AND t.metadata ? 'domain'"
LIST_SQL = "SELECT t.id FROM technical_docs t WHERE " + GUARD + " AND t.metadata ? 'domain' ORDER BY t.id"


def main() -> int:
    apply_mod._load_env()
    conn = apply_mod.psycopg2.connect(apply_mod._pg_conn())
    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            if "--list" in sys.argv[1:]:
                cur.execute(LIST_SQL)
                for (pg_id,) in cur.fetchall():
                    print(pg_id)
            else:
                cur.execute(COUNT_SQL)
                print(cur.fetchone()[0])
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
