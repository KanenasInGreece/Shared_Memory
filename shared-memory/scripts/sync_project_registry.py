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


def _load_env() -> None:
    here = Path(__file__).resolve().parent
    env_path = next((p for p in (here.parent / ".env", here.parent.parent / ".env")
                     if p.exists()), None)
    if env_path is None:
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


_load_env()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from project_axis import PROJECT_SQL, SENTINEL  # noqa: E402

PG_CONN = (
    f"postgresql://{os.environ.get('PG_USER', 'postgres')}:"
    f"{os.environ.get('PG_PASSWORD', '')}@{os.environ.get('PG_HOST', 'localhost')}:"
    f"{os.environ.get('PG_PORT', '5432')}/{os.environ.get('PG_DATABASE', 'agent_data')}"
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
            cur.execute(
                f"SELECT {PROJECT_SQL} AS p, count(*) FROM technical_docs"
                f" WHERE {PROJECT_SQL} IS NOT NULL GROUP BY 1"
            )
            in_use = dict(cur.fetchall())

        # The sentinel is reserved and can never be a project, folder or not.
        folders.discard(SENTINEL)
        to_add = sorted(folders - registered)

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

        print("\n── stored names that do NOT match a folder — YOUR adjudication surface ──")
        surface = 0
        for name, count in sorted(in_use.items(), key=lambda kv: -kv[1]):
            verdict, folder, ratio = classify(name, folders)
            if verdict == "exact":
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

        with conn.cursor() as cur:
            # Descriptions stay NULL: they are owed from the operator, and a
            # generated sentence would claim one was supplied.
            cur.executemany(
                "INSERT INTO projects (name, created_by) VALUES (%s, 'workspace_scan')"
                " ON CONFLICT (name) DO NOTHING",
                [(n,) for n in to_add],
            )
        conn.commit()
        print(f"\nRegistered {len(to_add)} project(s) from the workspace. "
              f"Descriptions are still owed from the operator.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
