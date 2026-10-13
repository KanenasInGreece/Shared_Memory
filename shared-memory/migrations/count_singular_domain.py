"""Read-only: print how many facts still carry a top-level `domain` key.

The count is migration 044's guard (a fact-like record whose `decision` blob names no section) plus the
presence of the key. Prints one number and nothing else; update_framework.sh reads it after the restart.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apply as apply_mod  # noqa: E402

COUNT_SQL = """
SELECT count(*) FROM technical_docs t
 WHERE jsonb_typeof(t.metadata) = 'object'
   AND lower(regexp_replace(coalesce(t.metadata->>'type', ''), '^\\s+|\\s+$', '', 'g'))
       NOT IN ('decision', 'retrospective')
   AND NOT coalesce(jsonb_typeof(t.metadata->'decision') = 'object'
                    AND t.metadata->'decision' ?| array['domain', 'domains'], false)
   AND t.metadata ? 'domain'
"""


def main() -> int:
    apply_mod._load_env()
    conn = apply_mod.psycopg2.connect(apply_mod._pg_conn())
    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute(COUNT_SQL)
            print(cur.fetchone()[0])
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
