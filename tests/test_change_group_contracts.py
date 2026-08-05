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
    ("vector-skill.py",): r'^VERSION = "([\d.]+)"',
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
