#!/usr/bin/env python3
"""Register projects from the workspace directories that define them.

THE FILESYSTEM IS THE AUTHORITY ON WHICH PROJECTS EXIST. A project name in this
system is the project FOLDER NAME — that is what the client derives from its
working directory and what every save is validated against. So the set of real
projects is not something to infer from the corpus: it is a directory listing,
and inferring it from stored records instead is how variants, renames and typos
came to look exactly like new projects.

WHAT THIS REPLACES. The registry was originally seeded from the records
themselves, which registers whatever a deployment happens to have been using —
including the misspellings. Seeding from the directories inverts that: names that
match a folder are correct BY DEFINITION, and everything else is surfaced as a
question. On the corpus this was written for, that cut the names needing human
adjudication from 17 to 9, because 8 matched a folder exactly and needed no
thought at all.

WHAT IT WILL NOT DO. It never renames, merges or deletes anything. Registering a
name is additive and safe; deciding that two names are one project is a judgement
about history that belongs to the operator and to normalize_projects.py. This
tool's job is to make that judgement SMALL by removing every name that is
obviously already correct.

TWO CAVEATS IT CANNOT RESOLVE, and reports instead:

  * RENAMED PROJECTS — a stored name close to, but not equal to, a folder. Could
    be a rename, could be a typo, could be two real projects with similar names.
  * REMOTE PROJECTS — work assisted by agents whose folders live on another
    machine. These have no local directory and are NOT dead; treating "no folder"
    as "not a project" would quietly disown them.

Configure the roots with PROJECT_ROOTS (os.pathsep-separated). There is no
default: this deployment's layout is one valid configuration, not the
configuration.

    PROJECT_ROOTS=~/labs/projects python sync_project_registry.py
    PROJECT_ROOTS=~/labs/projects python sync_project_registry.py --apply
"""
import argparse
import difflib
import os
import re
import sys
import unicodedata
from pathlib import Path

import psycopg2

CLOSE_MATCH_CUTOFF = float(os.environ.get("PROJECT_CLOSE_MATCH_CUTOFF", "0.6"))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import secure_env  # noqa: E402


def _load_env() -> None:
    """Delegates to secure_env's split loader (Credential_Custody_Plan PR A4,
    SEC-05-class sweep). This function used to dump every key from
    shared-memory/.env into os.environ via setdefault — AGENT_TOKEN included —
    which is exactly the defect A1 closed for the daemons. Config keys still
    reach os.environ; PG_PASSWORD (and any other secret-classified key) is
    held only in secure_env's in-process store and read back via
    secure_env.get_secret()."""
    secure_env.load_split_env()


_load_env()

from project_axis import PROJECT_SQL, SENTINEL  # noqa: E402
from project_alias import ACTIVE_ALIASES_SQL  # noqa: E402

PG_CONN = secure_env.pg_dsn(
    secure_env.get_secret('PG_PASSWORD', ''),
    user=os.environ.get('PG_USER', 'postgres'),
    host=os.environ.get('PG_HOST', 'localhost'),
    port=os.environ.get('PG_PORT', '5432'),
    dbname=os.environ.get('PG_DATABASE', 'agent_data'),
)


def project_folders(roots: list[str]) -> set[str]:
    """Every immediate subdirectory of every root, by name.

    Dot-directories are skipped — `.claude` and friends are configuration that
    lives beside the projects, not projects.
    """
    names: set[str] = set()
    for root in roots:
        path = Path(root).expanduser()
        if not path.is_dir():
            print(f"  ⚠ root does not exist, skipped: {path}", file=sys.stderr)
            continue
        names |= {
            d.name for d in path.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        }
    return names


def normalise_key(name: str) -> str:
    """The comparison key two spellings of ONE project must share.

    Comparing raw strings is too strict and comparing fuzzily is too loose, so
    the middle step is an explicit key, and what it folds away is a deliberate
    list of things that are NEVER a real difference between project names:

      * unicode composition — 'e' + combining acute vs 'é' are the same letter,
        and which one lands in a name depends on the keyboard that typed it;
      * case — a directory listing is the canonical casing;
      * surrounding whitespace, and separator RUNS;
      * WHICH separator — `_`, `-`, `.` and space are interchangeable in
        practice. `shared_memory_monitor` and `shared-memory-monitor` are one
        project written by two tools, not two projects.

    What it deliberately does NOT fold: extra or missing WORDS. `tier3` and
    `tier3-cloe` keep different keys, and `Shared_Memory` does not collapse into
    `shared-memory-GitHub`. Those are the cases where a rename, an abbreviation
    and a genuinely distinct project are indistinguishable without knowing the
    history — so they stay a QUESTION rather than becoming an automatic answer.
    Folding them here would have silently merged a sister project into the one
    beside it, which is the exact loss this whole axis is guarding against.
    """
    s = unicodedata.normalize("NFKC", name).strip().casefold()
    s = re.sub(r"[\s_.\-]+", "-", s)
    return s.strip("-")


def classify(name: str, folders: set[str]) -> tuple[str, str | None, float]:
    """(verdict, matched folder, similarity) for one stored project name.

    Four verdicts, narrowing: an exact string; a spelling VARIANT that shares the
    normalised key; a fuzzy near-miss; nothing. Only the first two are answers —
    the rest are questions for the operator.
    """
    if name in folders:
        return "exact", name, 1.0
    by_key: dict[str, str] = {}
    for folder in folders:
        # First spelling wins only when two folders collide, which `collisions`
        # reports separately — it is a real problem, not something to resolve here.
        by_key.setdefault(normalise_key(folder), folder)
    key = normalise_key(name)
    if key in by_key:
        return "variant", by_key[key], 1.0
    close = difflib.get_close_matches(key, list(by_key), n=1, cutoff=CLOSE_MATCH_CUTOFF)
    if close:
        ratio = difflib.SequenceMatcher(None, key, close[0]).ratio()
        return "close", by_key[close[0]], ratio
    return "absent", None, 0.0


def collisions(folders: set[str]) -> dict[str, list[str]]:
    """Folder names that normalise to the same key — two directories that this
    system cannot tell apart. Reported, never resolved: it means two real
    directories would compete for one registry row, and only the operator can
    say which is intended."""
    seen: dict[str, list[str]] = {}
    for folder in sorted(folders):
        seen.setdefault(normalise_key(folder), []).append(folder)
    return {k: v for k, v in seen.items() if len(v) > 1}


# The database, never a Python copy of the rule, says which spellings are one project:
# axis_normalize strips every non-alphanumeric (migration 035), which normalise_key above
# does not, and projects.normalized_key is UNIQUE on its result.
SPELLING_OF_REGISTERED_SQL = "SELECT name FROM projects WHERE normalized_key = axis_normalize(%s)"
RETIRED_SPELLING_SQL = ACTIVE_ALIASES_SQL + " AND axis_normalize(a.name) = axis_normalize(%s)"
FOLDER_KEYS_SQL = "SELECT n, axis_normalize(n) FROM unnest(%s::text[]) AS n"
INSERT_PROJECT_SQL = (
    "INSERT INTO projects (name, created_by) VALUES (%s, 'workspace_scan')"
    " ON CONFLICT DO NOTHING RETURNING id"
)

# undefined_table, undefined_column: the alias tables are absent or older than this tool.
_MISSING_ALIAS_SCHEMA = {"42P01", "42703"}


def _alias_schema_missing(exc: Exception) -> bool:
    return getattr(exc, "pgcode", None) in _MISSING_ALIAS_SCHEMA


def classify_new(cur, candidates: list[str]):
    """Split folders absent from the registry by exact name into (new, spellings,
    alias_skipped). `spellings` maps folder -> (kind, resolves-to) for a folder that
    is already settled; `new` keeps ONE folder per database key, the first in sorted
    order. Read-only. A missing alias schema skips only the alias check and sets
    `alias_skipped`; any other error propagates."""
    spellings: dict[str, tuple[str, str]] = {}
    alias_skipped = False
    pending: list[str] = []
    for folder in candidates:
        cur.execute(SPELLING_OF_REGISTERED_SQL, (folder,))
        row = cur.fetchone()
        if row:
            spellings[folder] = ("registered", row[0])
            continue
        if not alias_skipped:
            # A SAVEPOINT keeps a failed alias lookup from poisoning the transaction.
            cur.execute("SAVEPOINT alias_check")
            try:
                cur.execute(RETIRED_SPELLING_SQL, (folder,))
                row = cur.fetchone()
                cur.execute("RELEASE SAVEPOINT alias_check")
            except psycopg2.Error as exc:
                if not _alias_schema_missing(exc):
                    raise
                cur.execute("ROLLBACK TO SAVEPOINT alias_check")
                alias_skipped = True
                row = None
            if row:
                spellings[folder] = ("retired", row[1])
                continue
        pending.append(folder)
    new: list[str] = []
    if pending:
        cur.execute(FOLDER_KEYS_SQL, (pending,))
        key_of = {n: k for n, k in cur.fetchall()}
        winner: dict[str, str] = {}
        for folder in pending:  # sorted, so the first spelling of a key wins
            key = key_of.get(folder, folder)
            if key in winner:
                spellings[folder] = ("same-run", winner[key])
            else:
                winner[key] = folder
                new.append(folder)
    return new, spellings, alias_skipped


def register_projects(conn, names: list[str]):
    """Insert each name under its own SAVEPOINT so one refusal skips that name only.
    Returns (registered, skipped) where skipped is [(folder, reason)]. Never calls
    conn.rollback(): that would drop the rows already inserted. The caller commits."""
    registered: list[str] = []
    skipped: list[tuple[str, str]] = []
    with conn.cursor() as cur:
        for name in names:
            cur.execute("SAVEPOINT sync_project")
            try:
                # Descriptions stay NULL: they are owed from the operator, and a
                # generated sentence would claim one was supplied.
                cur.execute(INSERT_PROJECT_SQL, (name,))
                inserted = cur.fetchone()
                if inserted:
                    registered.append(name)
                else:
                    # A unique index (name or normalized_key) filled between read and write.
                    cur.execute(SPELLING_OF_REGISTERED_SQL, (name,))
                    row = cur.fetchone()
                    skipped.append((name, f"registered meanwhile as {row[0] if row else '?'}"))
                cur.execute("RELEASE SAVEPOINT sync_project")
            except psycopg2.Error as exc:
                cur.execute("ROLLBACK TO SAVEPOINT sync_project")
                skipped.append((name, f"refused by the registry ({getattr(exc, 'pgcode', None)})"))
    return registered, skipped


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="register folder names not yet in the registry")
    ap.add_argument("--roots", default=os.environ.get("PROJECT_ROOTS", ""),
                    help="os.pathsep-separated workspace roots (default: $PROJECT_ROOTS)")
    args = ap.parse_args()

    roots = [r for r in args.roots.split(os.pathsep) if r.strip()]
    if not roots:
        print("No workspace roots — pass --roots or set PROJECT_ROOTS.", file=sys.stderr)
        return 2

    folders = project_folders(roots)
    if not folders:
        print("No project directories found under the given roots.", file=sys.stderr)
        return 2

    conn = psycopg2.connect(PG_CONN, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM projects")
            registered = {r[0] for r in cur.fetchall()}
            # A decision made once must never return to the queue: a name with
            # an ACTIVE alias has already been adjudicated and resolves at
            # ingress, so surfacing it again is asking the operator to redo work.
            cur.execute("SAVEPOINT alias_list")
            try:
                cur.execute(ACTIVE_ALIASES_SQL)
                aliased = {r[0]: r[1] for r in cur.fetchall()}
                cur.execute("RELEASE SAVEPOINT alias_list")
            except psycopg2.Error as exc:
                # A reporting tool must not die because an optional table is missing,
                # but only a missing table or column is excused: any other error
                # would silently put retired spellings back in the queue.
                if not _alias_schema_missing(exc):
                    raise
                cur.execute("ROLLBACK TO SAVEPOINT alias_list")
                aliased = {}
            cur.execute(
                f"SELECT {PROJECT_SQL} AS p, count(*) FROM technical_docs"
                f" WHERE {PROJECT_SQL} IS NOT NULL GROUP BY 1"
            )
            in_use = dict(cur.fetchall())

        # The sentinel is reserved and can never be a project, folder or not.
        folders.discard(SENTINEL)
        with conn.cursor() as cur:
            to_add, spellings, alias_skipped = classify_new(
                cur, sorted(folders - registered))

        dupes = collisions(folders)
        if dupes:
            print("⚠ FOLDER NAMES THAT NORMALISE THE SAME — two directories, one key:")
            for key, names in dupes.items():
                print(f"      {key!r}: {names}")
            print()
        print(f"Project folders found      : {len(folders)}")
        print(f"Already registered         : {len(folders & registered)}")
        print(f"NEW — would be registered  : {len(to_add)}")
        for name in to_add:
            print(f"      + {name}")
        if spellings:
            print("\n── folders that are only a SPELLING of a name already settled"
                  " — this tool changes nothing about them ──")
            for folder, (kind, target) in sorted(spellings.items()):
                if kind == "registered":
                    what = f"spelling of registered project {target!r}"
                elif kind == "retired":
                    what = f"retired spelling of project {target!r}"
                else:
                    what = f"spelling of folder {target!r} — the registry keeps one"
                print(f"      {folder!r}: {what}; rename the folder to {target!r}")
        if alias_skipped:
            print("  ⚠ alias tables missing — retired spellings were not checked.")

        print("\n── stored names that do NOT match a folder — YOUR adjudication surface ──")
        surface = 0
        for name, count in sorted(in_use.items(), key=lambda kv: -kv[1]):
            verdict, folder, ratio = classify(name, folders)
            if verdict == "exact":
                continue
            if name in aliased:
                # Settled already — resolved at ingress to aliased[name].
                continue
            surface += 1
            if verdict == "variant":
                print(f"   {count:5}  {name!r} → folder spelling {folder!r}"
                      f"   (same name, different separators/case)")
            elif verdict == "close":
                print(f"   {count:5}  {name!r} ~{ratio:.0%}~ {folder!r}"
                      f"   RENAME or DISTINCT PROJECT?")
            else:
                print(f"   {count:5}  {name!r}   no local folder"
                      f" — remote/assisted elsewhere, or retired")
        print(f"\n   {surface} name(s) need a decision; the rest match a folder exactly.")

        if not args.apply:
            print("\nDry run — nothing registered. Re-run with --apply.")
            return 0
        if not to_add:
            print("\nNothing to register.")
            return 0

        registered_now, skipped = register_projects(conn, to_add)
        conn.commit()
        for folder, reason in skipped:
            print(f"   skipped: {folder} — {reason}")
        print(f"\nRegistered {len(registered_now)} project(s) from the workspace. "
              f"Descriptions are still owed from the operator.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
