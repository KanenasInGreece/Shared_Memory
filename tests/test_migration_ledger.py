"""apply.py must run each migration exactly ONCE.

It used to glob every numbered migration and execute all of them on every
invocation, while its docstring claimed it ran "all pending" — nothing recorded
what had been applied, so "pending" meant "all of them". Most migrations tolerate
that; one did not. Its `DELETE` kept one summary per entity, correct when written
and wrong after migration 007 re-keyed summaries on (entity, domain), and
re-running it destroyed 12 live summaries.

The selection rule is a pure function so it can be tested without a database —
which matters here, because everything else in this file is SQL and the suite
proves nothing about SQL.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "migrations"))
import apply as apply_mod


def _files(*names):
    return [Path(n) for n in names]


FILES = _files("001_a.sql", "002_b.sql", "007_c.sql", "021_d.sql", "022_e.sql")


def test_a_fresh_database_runs_everything():
    assert apply_mod.pending(FILES, None) == FILES


def test_a_database_resumes_from_where_it_says_it_reached():
    """The database records how far it got; apply continues from there."""
    assert [f.name for f in apply_mod.pending(FILES, "007_c.sql")] == [
        "021_d.sql", "022_e.sql"
    ]


def test_an_up_to_date_database_runs_nothing():
    """THE regression. Before this, an up-to-date database re-ran all 22
    migrations — including the destructive one."""
    assert apply_mod.pending(FILES, "022_e.sql") == []


def test_the_already_applied_migration_is_never_selected_again():
    """Stated separately from the count because the failure was not 'ran too
    many' — it was 'ran a specific destructive file again'."""
    for mark in ("002_b.sql", "007_c.sql", "021_d.sql", "022_e.sql"):
        chosen = {f.name for f in apply_mod.pending(FILES, mark)}
        assert "002_b.sql" not in chosen, f"the destructive migration re-selected at mark {mark}"


def test_ordering_is_preserved():
    """Migrations must run in order — a later one may depend on an earlier."""
    out = apply_mod.pending(FILES, "001_a.sql")
    assert out == sorted(out, key=lambda p: p.name)


def test_the_ledger_lives_in_the_database_being_migrated():
    """So the answer to 'which migrations has this database had?' travels with
    the database — a restored backup carries a ledger that already agrees with
    the schema it describes."""
    assert "CREATE TABLE IF NOT EXISTS schema_migrations" in apply_mod.LEDGER_DDL
    src = open(os.path.join(os.path.dirname(__file__), "..", "shared-memory",
                            "migrations", "apply.py"), encoding="utf-8").read()
    assert "INSERT INTO schema_migrations" in src


def test_the_migration_and_its_ledger_row_share_one_transaction():
    """A half-applied migration recorded as done would be skipped forever."""
    import inspect
    src = inspect.getsource(apply_mod.apply)
    body = src.split("with conn:")[1]
    assert "cur.execute(sql)" in body and "INSERT INTO schema_migrations" in body


def test_the_destructive_dedup_is_guarded_against_the_later_key():
    """Defence in depth: the ledger is the real fix, but any path that reaches
    002 again must not delete summaries the (entity, domain) key made legal."""
    sql = open(os.path.join(os.path.dirname(__file__), "..", "shared-memory",
                            "migrations", "002_concurrency_hardening.sql"),
               encoding="utf-8").read()
    guard = sql.split("DELETE FROM community_summaries")[0]
    assert "community_summaries_entity_domain_unique" in guard, (
        "the entity-level dedup is not guarded by the presence of the later "
        "(entity, domain) index")
    assert "IF to_regclass" in guard


def test_an_empty_ledger_on_a_populated_database_refuses_rather_than_re_running():
    """The refusal must key on the ledger being EMPTY, not on the table being
    ABSENT. A run that created the table then failed before recording anything —
    or a ledger cleared by hand — leaves it present and empty, and keying on
    absence let that fall through to the fresh-install path, which re-runs every
    migration against a populated database. That is the original bug, reachable
    by a second route.
    """
    src = open(os.path.join(os.path.dirname(__file__), "..", "shared-memory",
                            "migrations", "apply.py"), encoding="utf-8").read()
    guard = src.split("Make the operator choose, once.")[1].split("\n")[0:20]
    guard = "\n".join(guard)
    condition = [l for l in guard.splitlines() if l.strip().startswith("if ")][0]
    assert "not applied" in condition, "the refusal must test the ledger's EMPTINESS"
    assert "had_ledger" not in condition, (
        "the refusal must not key on the ledger TABLE's absence — an empty table "
        "on a populated database would fall through to re-running everything")
