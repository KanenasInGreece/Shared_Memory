"""Obligations a CHANGE GROUP carries, enforced instead of remembered.

The change groups say that touching one member means reviewing the whole group.
That is a discipline, and disciplines are what fail on the release where someone
is in a hurry. Everything here is a group obligation that can be checked
mechanically — so it is, and the remainder stays honestly a matter for eyes.

Each test names the group it belongs to and the failure it prevents.
"""
import os
import re
import sys

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_SCRIPTS = os.path.join(_ROOT, "shared-memory", "scripts")
_MIGRATIONS = os.path.join(_ROOT, "shared-memory", "migrations")
sys.path.insert(0, _SCRIPTS)


def _read(*parts) -> str:
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# ── GROUP 1 — client surface and its delivery ────────────────────────────────

# The four files the release version lives in. Two are client copies, which is
# why a server-side fix still touches this group.
_VERSION_PINS = {
    ("shared-memory", "scripts", "coordinator.py"): r'^FRAMEWORK_VERSION = "([\d.]+)"',
    ("shared-memory", "scripts", "memory_bridge.py"): r'^VERSION = "([\d.]+)"',
    ("shared-memory-skill", "shared-memory", "scripts", "memory_bridge.py"):
        r'^VERSION = "([\d.]+)"',
    ("mcp", "vector-skill.py"): r'^VERSION = "([\d.]+)"',
}


def test_all_four_version_pins_agree():
    """GROUP 1. The version lives in FOUR files and every release moves all of
    them, so a bump is four edits that nothing has ever checked.

    A missed one ships a client announcing a version the gateway does not
    recognise, and the only symptom is a compatibility warning from a `doctor`
    command nobody runs on a good day — so the divergence survives until someone
    debugs a symptom that has nothing to do with the change that caused it.
    """
    found = {}
    for parts, pattern in _VERSION_PINS.items():
        m = re.search(pattern, _read(*parts), re.M)
        assert m, f"no version pin found in {'/'.join(parts)}"
        found["/".join(parts)] = m.group(1)
    assert len(set(found.values())) == 1, (
        f"the four version pins disagree: {found}. Every release moves all four "
        "— two of them are client copies, which is why even a server-side fix "
        "touches this group. Then run sync_skills.sh."
    )


def test_the_client_copies_pin_the_same_api_version():
    """GROUP 1. `api_version` is the WIRE contract and is compared by the client
    against the gateway. Two copies of the client exist, so they can drift apart
    from each other as easily as from the server."""
    src = re.search(r"^API_VERSION = (\d+)",
                    _read("shared-memory", "scripts", "memory_bridge.py"), re.M)
    shipped = re.search(
        r"^API_VERSION = (\d+)",
        _read("shared-memory-skill", "shared-memory", "scripts", "memory_bridge.py"), re.M)
    assert src and shipped, "API_VERSION pin missing from a client copy"
    assert src.group(1) == shipped.group(1), (
        f"client copies disagree on api_version: source {src.group(1)}, shipped "
        f"{shipped.group(1)} — one of the two front doors is on the wrong contract")


# Marker-delimited constitution blocks. These carry their OWN version line —
# not the release version — because they advance independently of it: a wording
# fix bumps the block, a gateway fix does not. The pin they owe the contract is
# a well-formed, findable marker, since that is what a later upgrade pass
# find-and-replaces instead of appending a second copy of the block.
_SNIPPET_PINS = {
    ("shared-memory", "CONSTITUTION_SNIPPET.md"): "constitution-snippet",
    ("shared-memory-skill", "shared-memory", "CONSTITUTION_SNIPPET.md"):
        "constitution-snippet",
    ("mcp", "CONSTITUTION_SNIPPET_MCP.md"): "mcp-constitution-snippet",
}


def test_every_constitution_snippet_carries_a_findable_version_marker():
    """GROUP 1. Both client surfaces ship a marker-delimited constitution block
    — the CLI skill's, and (as of the MCP install kind) the connector's. A
    malformed or missing marker fails SILENTLY: Phase 8c cannot find the
    installed block, so every upgrade appends a second copy under the first
    instead of replacing it, and the drift check keeps reporting current."""
    for parts, name in _SNIPPET_PINS.items():
        text = _read(*parts)
        where = "/".join(parts)
        opened = re.search(rf"<!--\s*shared-memory:{name}\s+v(\d+)\s*-->", text)
        assert opened, f"{where}: no `shared-memory:{name} vN` opening marker"
        close = f"<!-- /shared-memory:{name} -->"
        assert close in text, f"{where}: no closing marker `{close}`"
        assert text.index(close) > opened.end(), (
            f"{where}: the closing marker precedes the opening one — a "
            "find-and-replace would swallow the rest of the operator's file")


def test_the_two_tracked_cli_snippet_copies_pin_the_same_version():
    """GROUP 1. The CLI snippet exists twice (source + tracked skill copy) and
    Phase 8c compares an INSTALLED block against the copy in the install. Two
    sources that disagree mean the drift check answers about whichever one the
    reader happened to open."""
    pattern = r"<!--\s*shared-memory:constitution-snippet\s+v(\d+)\s*-->"
    src = re.search(pattern, _read("shared-memory", "CONSTITUTION_SNIPPET.md"))
    shipped = re.search(
        pattern, _read("shared-memory-skill", "shared-memory", "CONSTITUTION_SNIPPET.md"))
    assert src and shipped
    assert src.group(1) == shipped.group(1), (
        f"the constitution snippet copies disagree on version: source v{src.group(1)}, "
        f"shipped v{shipped.group(1)} — run sync_skills.sh")


# ── GROUP 4 — storage and schema ─────────────────────────────────────────────

def _migration_files() -> list:
    return sorted(f for f in os.listdir(_MIGRATIONS)
                  if re.match(r"^\d{3}_.*\.sql$", f))


def test_every_table_a_migration_creates_reaches_the_fresh_install():
    """GROUP 4. `schema_init.sql` is the fast path a NEW deployment applies
    INSTEAD of replaying the migration chain, and nothing else reads it — so when
    it is wrong the only person who finds out is a stranger with no baseline to
    compare against.

    The generator that renders it has silently dropped three whole classes of
    object (every CHECK, every FOREIGN KEY, every IDENTITY column), each
    invisible to the entire suite. This cannot catch a missing constraint — that
    needs the live diff `verify_schema_init.py` performs — but it does catch the
    coarsest and most likely omission: a migration adding a table, and nobody
    regenerating the artefact afterwards.
    """
    init = _read("shared-memory", "migrations", "schema_init.sql")
    missing = []
    for fname in _migration_files():
        body = _read("shared-memory", "migrations", fname)
        for table in re.findall(
                r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+([a-z0-9_]+)", body, re.I):
            # A migration may create and later drop a scratch table; only assert
            # tables the live schema still has, which is what the artefact must
            # reproduce.
            if re.search(rf"DROP TABLE(?:\s+IF EXISTS)?\s+{table}\b", body, re.I):
                continue
            if not re.search(rf"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+{table}\b",
                             init, re.I):
                missing.append(f"{table} (from {fname})")
    assert not missing, (
        f"these tables exist in the migration chain but not in schema_init.sql: "
        f"{sorted(set(missing))}. A fresh install would not have them. Run "
        "migrations/generate_schema_init.py, then PROVE it with "
        "verify_schema_init.py — the generator is not trusted, it is checked."
    )


def _schema_init_body() -> str:
    """The fresh-install artefact, read through an env override.

    `SCHEMA_INIT_PATH` exists so the two guards below can be pointed at a
    REGENERATED copy in a scratch directory and shown to pass, without touching
    the tracked artefact. Regeneration needs a live database and is the merger's
    step; proving the guard bites and then un-bites must not.
    """
    override = os.environ.get("SCHEMA_INIT_PATH")
    if override:
        with open(override, encoding="utf-8") as fh:
            return fh.read()
    return _read("shared-memory", "migrations", "schema_init.sql")


def _sql_body(fname: str) -> str:
    """One migration with its `--` comments removed.

    ⚠ THE INSTRUMENT NEEDED THIS BEFORE ITS FINDINGS WERE WORTH ANYTHING. Scanned
    raw, the guards below reported an index named `are`, out of the prose
    sentence "indexes are …" in a comment. A guard that reports things that do
    not exist trains its reader to ignore it, so the comment stripping is not
    tidiness — it is what makes a hit mean something.
    """
    body = _read("shared-memory", "migrations", fname)
    return re.sub(r"--[^\n]*", "", body)


def _chain_drops() -> str:
    """Every migration's SQL, comments stripped, concatenated.

    ⛔ A DROP IS CHAIN-WIDE, NEVER FILE-LOCAL, and assuming otherwise produced
    three false hits on the first run: 002 creates an index that 007 drops, 007
    creates one that 029 drops. An object is in the fresh install if the CHAIN as
    a whole leaves it there, which is exactly what the generator introspects.
    """
    return "\n".join(_sql_body(f) for f in _migration_files())


def test_every_index_a_migration_creates_reaches_the_fresh_install():
    """GROUP 4. The table guard above keys on `CREATE TABLE`, so a migration that
    creates no table — an index, a function, a column, a constraint — passes it
    trivially while `schema_init.sql` silently lacks everything it added. That is
    not hypothetical: it is how a migration whose whole content was an index and
    two functions could ship with the artefact un-regenerated and the entire
    suite green.

    ⚠ WHAT THIS CANNOT SEE, so nobody mistakes it for the real check: it compares
    NAMES in two files. Whether the index in the artefact has the same DEFINITION
    as the live one is a live diff, and that lives in `verify_schema_init.py`
    (which now compares index definitions, after this class of miss).

    ⚠ AN INDEX ALSO DIES WITH ITS COLUMN. 027 drops `project_aliases.project`
    and with it the index 024 built on that column, without any `DROP INDEX`
    statement existing anywhere — so a guard that looked only for `DROP INDEX`
    reported a fourth phantom. A dropped column is checked for too.
    """
    init = _schema_init_body()
    chain = _chain_drops()
    missing = []
    for fname in _migration_files():
        body = _sql_body(fname)
        for match in re.finditer(
                r"CREATE(?:\s+UNIQUE)?\s+INDEX(?:\s+CONCURRENTLY)?"
                r"(?:\s+IF NOT EXISTS)?\s+([a-z0-9_]+)\s+ON\s+([a-z0-9_.]+)"
                r"[^(]*\(([^)]*)\)", body, re.I):
            index, table, cols = match.group(1), match.group(2), match.group(3)
            if re.search(rf"DROP INDEX(?:\s+IF EXISTS)?\s+{index}\b", chain, re.I):
                continue
            table = table.split(".")[-1]
            if any(re.search(
                    rf"ALTER TABLE\s+{table}\s+DROP COLUMN(?:\s+IF EXISTS)?\s+{col}\b",
                    chain, re.I)
                   for col in re.findall(r"[a-z0-9_]+", cols, re.I)):
                continue
            if not re.search(rf"\b{index}\b", init, re.I):
                missing.append(f"{index} (from {fname})")
    assert not missing, (
        f"these indexes exist in the migration chain but not in schema_init.sql: "
        f"{sorted(set(missing))}. A fresh install would not have them — and an "
        "index is how this schema now carries an invariant, not merely a lookup "
        "speed. Run migrations/generate_schema_init.py, then PROVE it with "
        "verify_schema_init.py."
    )


def test_every_function_a_migration_creates_reaches_the_fresh_install():
    """GROUP 4. The same gap, for functions — and this is the half that bites
    hardest, because a function is what a trigger and an index EXPRESSION both
    call. A fresh install missing one does not merely lack a feature; it fails to
    install at all, in a single transaction, creating nothing.
    """
    init = _schema_init_body()
    chain = _chain_drops()
    missing = []
    for fname in _migration_files():
        body = _sql_body(fname)
        for fn in re.findall(
                r"CREATE(?:\s+OR\s+REPLACE)?\s+FUNCTION\s+([a-z0-9_]+)\s*\(",
                body, re.I):
            if re.search(rf"DROP FUNCTION(?:\s+IF EXISTS)?\s+{fn}\b", chain, re.I):
                continue
            if not re.search(rf"FUNCTION\s+(?:public\.)?{fn}\s*\(", init, re.I):
                missing.append(f"{fn}() (from {fname})")
    assert not missing, (
        f"these functions exist in the migration chain but not in "
        f"schema_init.sql: {sorted(set(missing))}. A fresh install would abort "
        "the moment a trigger or an index expression named one. Run "
        "migrations/generate_schema_init.py, then PROVE it with "
        "verify_schema_init.py."
    )


def test_the_generator_emits_functions_before_tables_and_triggers_after():
    """GROUP 4. SECTION ORDER in the generated artefact, asserted on the
    generator's own source rather than on a database.

    Functions used to be emitted LAST, after every table and index, because a
    trigger cannot precede the function it names. That is true of triggers and
    silently false of INDEXES: an index whose expression CALLS a function needs
    the function first, so the artefact aborted on `function ... does not exist`
    — on a fresh install only, the one path nobody re-inspects. The fix is a
    SPLIT, and this asserts the split stays split.
    """
    src = _read("shared-memory", "migrations", "generate_schema_init.py")
    body = src[src.index("def generate("):]
    fn_at = body.index("render_functions(cur)")
    tbl_at = body.index("for table in fetch_tables(cur)")
    trg_at = body.index("render_triggers(cur)")
    assert fn_at < tbl_at, (
        "generate() emits tables (and their indexes) before functions — an index "
        "expression calling a schema function would abort a fresh install")
    assert tbl_at < trg_at, (
        "generate() emits triggers before their tables")


def test_the_migration_chain_has_no_gaps_or_duplicate_numbers():
    """GROUP 4. Migrations are applied in filename order and recorded once each,
    so a duplicated number means two files race for one ledger slot and a gap
    usually means a file was renamed after being applied somewhere."""
    numbers = [int(f[:3]) for f in _migration_files()]
    dupes = {n for n in numbers if numbers.count(n) > 1}
    assert not dupes, f"duplicate migration numbers: {sorted(dupes)}"
    assert numbers == list(range(min(numbers), max(numbers) + 1)), (
        f"the migration chain has gaps: {sorted(set(range(min(numbers), max(numbers) + 1)) - set(numbers))}")


# ── GROUP 5 — install and operate ────────────────────────────────────────────

def test_every_script_the_upgrade_path_names_actually_exists():
    """GROUP 5. The invocation line IS the contract: a documented step naming a
    file that is not there fails at the worst moment, on a stranger's machine,
    while they are following instructions faithfully.

    This checks existence only. Whether the command RUNS with the dependencies it
    lists is not checkable here and stays an operator obligation — it is how
    v0.8.45's verifiers came to be documented with a dependency they silently
    needed and never named.
    """
    agents = _read("AGENTS.md")
    referenced = set(re.findall(r"(shared-memory/(?:scripts|migrations|ops)/[\w./-]+\.(?:py|sh))",
                                agents))
    assert referenced, "no scripts referenced in AGENTS.md — the regex has rotted"
    missing = sorted(p for p in referenced
                     if not os.path.exists(os.path.join(_ROOT, p)))
    assert not missing, (
        f"AGENTS.md names these scripts and they do not exist: {missing}")


def test_agents_md_states_postflights_actual_exit_condition():
    """GROUP 5. AGENTS.md line ~272 said postflight "exits 0 iff assertions
    A1-A5 pass" through v0.9.24, when A8 shipped (a REAL reasoning-backend
    completion through the gateway proxy path) and postflight.md's own spec
    (the authoritative contract -- postflight.sh's header says so explicitly:
    "THE SPEC IS THE CONTRACT... where this script and that document
    disagree, the document wins") moved the exit condition to A1-A5 AND A8.
    AGENTS.md was never updated, so an operating agent reading it would
    believe an A8 failure (or a missing SKIP) does not affect the exit code.

    This pins AGENTS.md's claim against postflight.md's own "Exit code:"
    line rather than a hardcoded string, so the NEXT assertion added to the
    contract (A9, say) fails this test the same way A8 did, instead of
    leaving AGENTS.md to drift silently again.
    """
    spec = _read("shared-memory", "Documentation", "postflight.md")
    m = re.search(r"\*\*Exit code:\*\*\s*`0`\s*iff assertions\s*\*\*([^*]+?)\*\*\s*all pass",
                   spec)
    assert m, "postflight.md's own Exit code line has changed shape — update the regex"
    exit_condition = m.group(1).strip()  # e.g. "A1–A5 and A8"

    agents = _read("AGENTS.md")
    assert exit_condition in agents, (
        f"postflight.md's spec now says the exit condition is {exit_condition!r}, "
        "but AGENTS.md's Phase 9 section does not say the same thing -- "
        "it is quoting a stale assertion range again."
    )


def test_agents_md_pipes_the_right_number_of_answers_into_install_framework():
    """GROUP 5. AGENTS.md's Phase 1 no longer hand-mirrors install_framework.sh
    (D11 fix, fix round) -- it DRIVES it via piped stdin, feeding N
    newline-delimited answers in the order the script's prompts appear. That
    killed the "hand-copied step list drifts from what the script actually
    does" class, but replaced it with an UNPROTECTED coupling: nothing
    checked that N, or the mix of plain-answer vs y/n prompts, still matches
    what the script asks. Unlike a wrong VALUE, a desynced COUNT fails
    SILENTLY -- if the script gains, loses or reorders a prompt, answer 4
    (a password) lands wherever prompt 4 now is, with no error at all.

    Both sides are derived from their own source, never hardcoded here: the
    script side by structurally counting its own `ask`/`ask_secret` call
    sites and top-level `read -r -p` calls (excluding the already-exists
    overwrite prompt, which fires ONLY when shared-memory/.env already
    exists -- a path AGENTS.md's documented flow explicitly skips itself
    for -- and excluding the `read` lines INSIDE the ask()/ask_secret()
    function BODIES, which are counted once per call site below instead of
    once per definition); the doc side by parsing the literal printf format
    string AGENTS.md actually pipes into the script.
    """
    script = _read("shared-memory", "scripts", "install_framework.sh")

    stripped = re.sub(r'if \[ -f "\$ENV_FILE" \]; then\n.*?\nfi\n', "", script,
                       count=1, flags=re.S)
    assert stripped != script, (
        "install_framework.sh's already-exists overwrite block has changed "
        "shape — update this test's regex before trusting its result")

    stripped = re.sub(r"^ask\(\) \{.*?\n\}\n", "", stripped, count=1, flags=re.M | re.S)
    stripped = re.sub(r"^ask_secret\(\) \{.*?\n\}\n", "", stripped, count=1, flags=re.M | re.S)
    assert "read -r -p \"$1 [$2]: \"" not in stripped and "read -r -s -p \"$1: \"" not in stripped, (
        "ask()/ask_secret()'s function bodies were not stripped — update this test's regex")

    script_sequence = []
    for m in re.finditer(r"\$\(ask_secret\b|\$\(ask\b|read -r -s -p |read -r -p ", stripped):
        tok = m.group(0)
        script_sequence.append(
            "confirm" if tok.startswith("read") else
            "secret" if "ask_secret" in tok else "value"
        )
    assert script_sequence, "no prompts extracted from install_framework.sh — the regex has rotted"

    agents = _read("AGENTS.md")
    m = re.search(
        r"printf '([^']*)'[^\n]*\\\n\s*\|\s*bash shared-memory/scripts/install_framework\.sh",
        agents,
    )
    assert m, (
        "AGENTS.md's Phase 1 no longer pipes a printf'd answer sequence into "
        "install_framework.sh the way this test expects — either update the "
        "regex, or Phase 1 has reverted to hand-mirroring the script again "
        "(the exact class the D11 fix this test guards exists to prevent)."
    )
    piped = [t for t in m.group(1).split(r"\n") if t != ""]
    piped_confirm_count = sum(1 for t in piped if t != "%s")
    script_confirm_count = sum(1 for t in script_sequence if t == "confirm")

    assert len(piped) == len(script_sequence), (
        f"install_framework.sh's fresh-.env path now issues {len(script_sequence)} "
        f"prompts (types, in order: {script_sequence}), but AGENTS.md's Phase 1 "
        f"printf line pipes {len(piped)} answers into it. The script's prompt "
        "sequence changed — update AGENTS.md's Phase 1 piped-answer line (and "
        "its explanatory prose) to match, in the same order, or an answer will "
        "silently land in the wrong field."
    )
    assert piped_confirm_count == script_confirm_count, (
        f"install_framework.sh's fresh-.env path now asks {script_confirm_count} "
        f"y/n-style questions, but AGENTS.md's Phase 1 printf line pipes "
        f"{piped_confirm_count} literal y/n answers ('y'/'n', not '%s') — update "
        "AGENTS.md's Phase 1 piped-answer line to match."
    )


def test_agents_md_download_command_carries_no_hardcoded_version():
    """The Before-Phase-0 tarball command is the one line a stranger
    copy-pastes before anything else exists on the host. A pinned version there
    is correct on the day it ships and silently wrong at the next release —
    it would download a stale tag while the `cd` still happens to work.

    AGENTS.md is deliberately NOT in _VERSION_PINS: adding it would make a
    version bump land in five places, and the four-place rule is the operator's
    to change. So the command stays version-free and this guards that.
    """
    src = _read("AGENTS.md")
    urls = re.findall(r"archive/refs/tags/(\S+?)\.tar\.gz", src)
    assert urls, "the tarball download command vanished from AGENTS.md"
    for tag in urls:
        assert not re.fullmatch(r"v\d+\.\d+\.\d+", tag), (
            f"AGENTS.md pins the release tag '{tag}' in a download URL. Nothing "
            f"bumps it, so it goes stale at the next release — use the vX.Y.Z "
            f"placeholder and point the reader at the releases page."
        )
