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
    # Guard names the historical (entity, domain) index from migration 007/009.
    # Migration 029 replaces that index with community_summaries_axis_level_unique;
    # 002 still guards on the old name so a re-run of 002 refuses against any DB
    # that ever had the intermediate key. Both names are acceptable evidence
    # that a later unique index on community_summaries is required.
    assert (
        "community_summaries_entity_domain_unique" in guard
        or "community_summaries_axis_level_unique" in guard
    ), (
        "the entity-level dedup is not guarded by the presence of a later "
        "community_summaries unique index")
    assert "IF to_regclass" in guard


def test_an_empty_ledger_on_a_populated_database_refuses_rather_than_re_running():
    """The refusal must key on the ledger being EMPTY, not on the table being
    ABSENT. A run that created the table then failed before recording anything —
    or a ledger cleared by hand — leaves it present and empty, and keying on
    absence let that fall through to the fresh-install path, which re-runs every
    migration against a populated database. That is the original bug, reachable
    by a second route.

    Asserted on BEHAVIOUR: the first version of this test read the condition's
    source text, and a mutation disabling the guard with `if False and ...` left
    that text intact and survived.
    """
    assert apply_mod.needs_adoption(True, set()) is True
    assert apply_mod.needs_adoption(True, {"001_a.sql"}) is False


def test_a_fresh_database_is_never_asked_to_adopt():
    """No framework schema means the migrations genuinely have not run."""
    assert apply_mod.needs_adoption(False, set()) is False
    assert apply_mod.pending(FILES, None) == FILES


# ── Forward-only: a database that has gone somewhere this code cannot follow ──
#
# `pending` answers "what has this database not had yet". It cannot answer "has
# this database already passed this code", because its selection is by POSITION:
# a ledger at 031 against a checkout topping out at 022 orders after every file,
# so pending is EMPTY — byte-identical to an up-to-date database. The tool then
# reports success and an older gateway starts against a newer schema.
#
# The state is derived from the ledger that travels inside the dump, so it needs
# no manifest field and no version stamp — nothing that could disagree with the
# schema it describes.

AHEAD_LEDGER = {"001_a.sql", "002_b.sql", "007_c.sql", "021_d.sql", "022_e.sql",
                "030_f.sql", "031_g.sql"}


def test_a_database_ahead_of_the_checkout_is_detected():
    """The restore case: a dump from a newer deployment, replayed onto this tree."""
    assert apply_mod.ahead(FILES, AHEAD_LEDGER) == {"030_f.sql", "031_g.sql"}


def test_the_ahead_state_is_invisible_to_the_selection_rule():
    """THE regression, and the reason `ahead` has to exist as its own rule.

    Both databases produce an EMPTY pending list. Only set membership tells them
    apart, so each side is asserted by VALUE — pinning the two pending results
    equal to each other would pass just as happily if both were wrong.
    """
    up_to_date = {f.name for f in FILES}

    assert apply_mod.pending(FILES, "022_e.sql") == []       # genuinely finished
    assert apply_mod.pending(FILES, "031_g.sql") == []       # a dozen releases ahead

    assert apply_mod.ahead(FILES, up_to_date) == set()
    assert apply_mod.ahead(FILES, AHEAD_LEDGER) == {"030_f.sql", "031_g.sql"}


def test_an_up_to_date_database_is_not_ahead():
    assert apply_mod.ahead(FILES, {f.name for f in FILES}) == set()


def test_a_database_behind_the_checkout_is_not_ahead():
    """Behind is the ordinary case every upgrade is: pending has work, and
    nothing in the ledger is unknown to this tree."""
    behind = {"001_a.sql", "002_b.sql"}
    assert apply_mod.ahead(FILES, behind) == set()
    assert [f.name for f in apply_mod.pending(FILES, "002_b.sql")] == [
        "007_c.sql", "021_d.sql", "022_e.sql"
    ]


def test_a_fresh_database_is_not_ahead():
    """An empty ledger is the adoption question's territory, never this one."""
    assert apply_mod.ahead(FILES, set()) == set()
