"""schema_init.sql is the FRESH-INSTALL fast path, and it is generated.

Anything the generator does not know how to emit simply vanishes from it — and
vanishes only for NEW deployments, because upgraded ones got it from the
migration. That is the worst shape a schema divergence can take: the guarantee
holds everywhere it was tested and nowhere it was not.

It happened. The generator rebuilt tables from columns and indexes alone, so it
dropped every table-level CHECK constraint: the projects sentinel reservation,
and seven constraints on relation_adjudications that had been missing from fresh
installs since that table was introduced.

These tests read the artefact, not the generator's source — a generator that
still contains the right function but no longer calls it would pass a source
check.
"""
import os
import re

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_MIGRATIONS = os.path.join(_ROOT, "shared-memory", "migrations")
_SCHEMA_INIT = os.path.join(_MIGRATIONS, "schema_init.sql")


def _schema_init() -> str:
    with open(_SCHEMA_INIT, encoding="utf-8") as f:
        return f.read()


def _create_table_blocks(sql: str) -> dict[str, str]:
    """table name -> the text of its CREATE TABLE (...) body."""
    blocks = {}
    for m in re.finditer(
        r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\n\);", sql, re.S | re.I
    ):
        blocks[m.group(1).lower()] = m.group(2)
    return blocks


def test_the_sentinel_reservation_survives_into_a_fresh_install():
    """The reservation is STRUCTURAL on purpose — a code path can be bypassed by
    the next writer, a constraint cannot. If it only exists on deployments that
    ran the migration, it is not structural at all."""
    body = _create_table_blocks(_schema_init()).get("projects")
    assert body is not None, "projects table missing from schema_init.sql"
    assert "general_discussion" in body and "CHECK" in body.upper(), (
        "the sentinel reservation is absent from the fresh-install path — a new "
        "deployment could register 'general_discussion' as a real project. "
        "Re-run generate_schema_init.py."
    )


def test_no_table_loses_its_check_constraints_in_the_fresh_install_path():
    """The general form, so the next dropped constraint fails here rather than
    on a stranger's install: any table a migration gives a CHECK must still have
    one after generation."""
    migration_sql = ""
    for name in sorted(os.listdir(_MIGRATIONS)):
        if re.match(r"^\d+.*\.sql$", name):
            with open(os.path.join(_MIGRATIONS, name), encoding="utf-8") as f:
                migration_sql += f.read() + "\n"

    # Tables whose CREATE TABLE in a migration declares a CHECK.
    constrained = set()
    for m in re.finditer(
        r"CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)\s*\((.*?)\n\);",
        migration_sql, re.S | re.I,
    ):
        if "CHECK" in m.group(2).upper():
            constrained.add(m.group(1).lower())

    assert constrained, "parsed no constrained tables — the check would pass vacuously"

    blocks = _create_table_blocks(_schema_init())
    missing = [
        t for t in sorted(constrained)
        if t in blocks and "CHECK" not in blocks[t].upper()
    ]
    assert not missing, (
        f"these tables declare CHECK constraints in a migration but carry none in "
        f"the fresh-install path: {missing}. Re-run generate_schema_init.py."
    )
