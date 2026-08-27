"""Invariants N1–N3 — a rename is whole, it never destroys history, and the
graph half never runs ahead of the Postgres half.

Renaming a project used to be three independent sweeps over the alias map:
rewrite every record and COMMIT, then try to record every alias, then rewire
every graph node. The second sweep could fail on a pair the first had already
committed, and the third ran regardless of both. That produced committed states
no single sweep could describe — records moved onto a name that was not
registered, the old name still in the registry with no alias recorded, the graph
pointing at the new node anyway — and none of them were reported as a failure.

Two foreign keys point at ``projects.name``, so retiring a registry row CAN be
vetoed. That is the point of N1 rather than an argument against it: a veto that
rolls the whole pair back leaves the old name registered and still resolving, so
every save keeps working. The hazard was never the veto, it was the half-write.

⚠ The statement sequence is asserted as a VALUE, not grepped out of the
executor. A guard disabled with `if False and …` leaves its own text in the
file, so a source-reading test passes against a dead guard — twice already here.

No DB or Neo4j required.
"""
import os
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

import normalize_projects as np_mod
from normalize_projects import (
    apply_rename, parse_alias_map, rename_pair, rename_statements,
)

OLD, NEW = "shared_memory", "shared-memory-GitHub"


def _sql_at(index: int) -> str:
    return rename_statements(OLD, NEW)[index][0]


def _index_of(fragment: str) -> int:
    for i, (sql, _) in enumerate(rename_statements(OLD, NEW)):
        if fragment in sql:
            return i
    raise AssertionError(f"no statement contains {fragment!r}")


# ── The shape of a rename ────────────────────────────────────────────────────

def test_a_rename_touches_records_ledger_aliases_and_registry():
    """Every half of a rename is in ONE sequence. A half that lives somewhere
    else is a half that can be committed on its own."""
    joined = " ".join(sql for sql, _ in rename_statements(OLD, NEW))
    for table in ("technical_docs", "project_promotions", "project_aliases",
                  "projects", "aliases"):
        assert table in joined, f"{table} is not part of a rename"


def test_both_record_fields_are_rewritten_separately():
    """Per-field, deliberately NOT through the shared COALESCE: a row carrying
    the old name in the decision blob and a newer one at the top level still
    needs rewriting, and the resolution would shadow it."""
    joined = " ".join(sql for sql, _ in rename_statements(OLD, NEW))
    assert "metadata->>'project' = %s" in joined
    assert "metadata->'decision'->>'project' = %s" in joined
    assert "COALESCE" not in joined


def test_the_old_registry_row_is_retired_before_the_alias_is_interned():
    """A1 — a string is never both a registered project and an alias, and a
    trigger enforces it. Interning first would simply be refused."""
    assert _index_of("DELETE FROM projects") < _index_of("INSERT INTO project_aliases")


def test_the_ledger_and_the_aliases_are_repointed_before_the_row_is_retired():
    """Both carry a foreign key to projects.name. Retiring the row first would
    be vetoed by rows this rename was about to move anyway."""
    retire = _index_of("DELETE FROM projects")
    assert _index_of("UPDATE project_promotions") < retire
    assert _index_of("UPDATE project_aliases") < retire


# ── N2 — a rename never destroys the name a ledger row targeted ──────────────

def test_repointing_the_ledger_preserves_the_original_target():
    """The promotions ledger exists to answer "what was this before". A rename
    that silently rewrote its target would destroy exactly the evidence it
    holds, and a one-way write would become unauditable."""
    i = _index_of("UPDATE project_promotions")
    sql, params = rename_statements(OLD, NEW)[i]
    assert "note" in sql, "the ledger row is repointed without a trail"
    assert "concat_ws" in sql, "an existing note must survive, not be overwritten"
    trail = next(p for p in params if isinstance(p, str) and "to_project was" in p)
    assert OLD in trail and NEW in trail


def test_the_ledger_is_never_repointed_by_a_cascade():
    """ON UPDATE CASCADE would repoint the ledger with no trace at all — the
    same destruction, made invisible by being automatic."""
    joined = " ".join(sql for sql, _ in rename_statements(OLD, NEW))
    assert "CASCADE" not in joined.upper()


# ── A3/A4 — chains collapse at write time, history is left alone ─────────────

def test_only_active_alias_rows_are_repointed():
    """A4 — mappings are superseded, never deleted. Re-pointing a superseded row
    would falsify the history it exists to preserve, so it is left alone and
    will veto the rename instead (see the module docstring)."""
    i = _index_of("UPDATE project_aliases")
    sql, _ = rename_statements(OLD, NEW)[i]
    assert "AND active" in sql


def test_the_alias_string_and_its_mapping_are_written_together():
    """One statement, so there is no round trip in which the alias string exists
    with nothing pointing at it."""
    i = _index_of("INSERT INTO project_aliases")
    sql, _ = rename_statements(OLD, NEW)[i]
    assert "INSERT INTO aliases" in sql and "WITH" in sql


# ── The executor ─────────────────────────────────────────────────────────────

class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append(sql)
        if self.conn.fail_on and self.conn.fail_on in sql and self.conn.fail_budget:
            if self.conn.fail_budget is not True:
                self.conn.fail_budget -= 1
            raise RuntimeError(
                'update or delete on table "projects" violates foreign key '
                'constraint "project_promotions_to_project_fkey"')
        if "SELECT 1 FROM projects" in sql:
            self._row = (1,) if self.conn.registered else None
        elif "count(*)" in sql:
            self._row = (0,)
        else:
            self._row = None

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, registered=True, fail_on=None, fail_times=True):
        self.registered, self.fail_on = registered, fail_on
        # True = fail every time; an int = fail that many times, so a run can be
        # set up where the FIRST pair is vetoed and the rest are not.
        self.fail_budget = fail_times
        self.executed, self.commits, self.rollbacks = [], 0, 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


# ── N1 — one rename, one transaction ─────────────────────────────────────────

def test_a_successful_rename_commits_exactly_once():
    """Once, at the END. The old code committed the record rewrite before the
    registry work had even been attempted, which is what made a half-rename
    reachable."""
    conn = _Conn()
    assert apply_rename(conn, OLD, NEW) is True
    assert conn.commits == 1
    assert conn.rollbacks == 0


def test_a_vetoed_rename_commits_nothing_and_rolls_back():
    """The veto case, which is the one that actually happens: 129 promotion rows
    and 11 alias rows point at projects.name today."""
    conn = _Conn(fail_on="DELETE FROM projects")
    assert apply_rename(conn, OLD, NEW) is False
    assert conn.commits == 0
    assert conn.rollbacks == 1


def test_a_failure_in_the_first_statement_also_commits_nothing():
    """The record rewrite is inside the transaction too — it is not a
    'safe prelude' that may be committed early."""
    conn = _Conn(fail_on="UPDATE technical_docs")
    assert apply_rename(conn, OLD, NEW) is False
    assert conn.commits == 0


def test_an_unregistered_target_is_refused_before_anything_is_written():
    """This check used to run in the second sweep — AFTER the records had been
    committed onto a name that might not be registered at all."""
    conn = _Conn(registered=False)
    assert rename_pair(conn, OLD, NEW, dry_run=False) is False
    assert conn.commits == 0
    assert not any("UPDATE technical_docs" in s for s in conn.executed)


def test_dry_run_writes_nothing():
    conn = _Conn()
    assert rename_pair(conn, OLD, NEW, dry_run=True) is False
    assert conn.commits == 0
    assert not any(s.startswith("UPDATE") or s.startswith("DELETE")
                   for s in conn.executed)


def test_a_pair_that_renames_a_name_to_itself_is_not_a_rename():
    conn = _Conn()
    assert rename_pair(conn, NEW, NEW, dry_run=False) is False
    assert conn.commits == 0
    assert conn.executed == []


# ── N3 — the graph half never runs ahead of the Postgres half ────────────────

class _Session:
    def __init__(self, driver):
        self.driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, cypher, **params):
        self.driver.ran.append((cypher, params))
        return _Single()


class _Single:
    def single(self):
        return {"n": 0}


class _Driver:
    def __init__(self):
        self.ran = []

    def session(self):
        return _Session(self)

    def close(self):
        pass


def _run_main(monkeypatch, conn, driver, argv):
    monkeypatch.setattr(np_mod.psycopg2, "connect",
                        lambda *a, **k: conn)
    monkeypatch.setattr(np_mod.GraphDatabase, "driver",
                        staticmethod(lambda *a, **k: driver))
    monkeypatch.setattr(sys, "argv", ["normalize_projects.py"] + argv)
    return np_mod.main()


def test_a_vetoed_pair_leaves_the_graph_untouched(monkeypatch):
    """The failure the old code could not see: the third sweep rewired every
    node in the map, including the ones whose Postgres half had just failed, so
    the graph moved to a name the registry had refused to retire."""
    conn = _Conn(fail_on="DELETE FROM projects")
    driver = _Driver()
    rc = _run_main(monkeypatch, conn, driver, ["--map", f"{OLD}={NEW}", "--apply"])
    assert rc == 1, "a failed pair must not exit zero"
    assert driver.ran == [], "the graph was rewired for a pair that rolled back"


def test_a_committed_pair_does_reach_the_graph(monkeypatch):
    """The mirror: N3 must not be satisfied by never rewiring anything."""
    conn = _Conn()
    driver = _Driver()
    rc = _run_main(monkeypatch, conn, driver, ["--map", f"{OLD}={NEW}", "--apply"])
    assert rc == 0
    assert any("PROJECT_OF" in c or "MERGE" in c for c, _ in driver.ran)


def test_one_failing_pair_does_not_stop_the_others(monkeypatch):
    """A map is a list of independent renames. The old code's per-alias
    try/except got this right and its committed-first sweep got it wrong; keep
    the right half."""
    conn = _Conn(fail_on="DELETE FROM projects", fail_times=1)
    driver = _Driver()
    rc = _run_main(monkeypatch, conn, driver,
                   ["--map", f"{OLD}={NEW},cadence={NEW}", "--apply"])
    assert rc == 1, "a run with any failed pair exits non-zero"
    assert conn.commits == 1, "the second pair must still have been applied"


# ── O1 (v0.9.69): --apply is required to write anything ──────────────────────

def test_default_invocation_previews_and_writes_nothing(monkeypatch):
    """The flip this release makes: a bare invocation (no --apply, no
    --dry-run — the old flag is gone) must behave exactly like the old
    --dry-run did, not like the old default."""
    conn = _Conn()
    driver = _Driver()
    rc = _run_main(monkeypatch, conn, driver, ["--map", f"{OLD}={NEW}"])
    assert rc == 0
    assert conn.commits == 0, "the default invocation wrote to Postgres"
    assert not any("MERGE" in c for c, _ in driver.ran), (
        "the default invocation rewired the graph"
    )


def test_apply_flag_is_required_to_write(monkeypatch):
    conn = _Conn()
    driver = _Driver()
    _run_main(monkeypatch, conn, driver, ["--map", f"{OLD}={NEW}"])
    assert conn.commits == 0
    _run_main(monkeypatch, conn, driver, ["--map", f"{OLD}={NEW}", "--apply"])
    assert conn.commits == 1


def test_the_map_parser_ignores_malformed_pairs():
    assert parse_alias_map("a=b,,c=,=d,e=f") == {"a": "b", "e": "f"}
    assert parse_alias_map("") == {}
