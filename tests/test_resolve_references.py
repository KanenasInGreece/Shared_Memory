"""resolve_references.py, the operator-run citation backfill, against fake stores.

What is proved here is what the script ASKS the stores to do: which edge type it merges,
that a retrospective endpoint is counted and not planned, that the re-type of earlier
INFORMED_BY edges and the write share one transaction, that a dry-run writes nothing, and
where the connection settings come from. Whether the Cypher does that on a real graph is
checked live, on the operator's --apply run (before/after counts).
"""
import os
import re
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
import resolve_references as rref  # noqa: E402
from ontology import ONT  # noqa: E402

ROWS = [
    (10, "decision", "This builds on decision 20 and refines fact 30."),
    (20, "decision", "An earlier decision."),
    (30, None, "A plain fact cites retrospective 40."),
    (40, "retrospective", "The outcome of decision 20."),
    (50, "Decision", "Supersedes nothing; see decision 10."),
    (60, "Retrospective", "Rated after fact 30."),
]
# What the fake graph reports for the apply transaction: 19 re-typed, 5 matched, 3 created.
RETYPED, MATCHED, CREATED = 19, 5, 3


class _Result:
    def __init__(self, record=None, created=0, deleted=0):
        self._record = record
        self._counters = SimpleNamespace(relationships_created=created,
                                         relationships_deleted=deleted)

    def single(self):
        return self._record

    def consume(self):
        return SimpleNamespace(counters=self._counters)


class _Tx:
    def __init__(self, log):
        self.log = log

    def run(self, query, **params):
        self.log.append(("tx", query, params))
        if "rows" in params:
            return _Result({"matched": MATCHED, "ok": True}, created=CREATED)
        return _Result({"n": 0}, deleted=RETYPED)


class _Session:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **params):
        self.log.append(("session", query, params))
        return _Result({"n": 0, "ok": True, "matched": 1}, created=1)

    def execute_write(self, fn, *args):
        self.log.append(("execute_write", None, None))
        return fn(_Tx(self.log), *args)


class _Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, *args):
        self.sql = sql

    def fetchall(self):
        if "content" in self.sql and "metadata" in self.sql:
            return list(ROWS)
        if "content" in self.sql:
            return [(i, c) for i, _, c in ROWS]
        return [(i, t) for i, t, _ in ROWS]


@pytest.fixture
def stores(monkeypatch):
    log, seen = [], {}

    def connect(*args, **kwargs):
        seen["pg"] = (args, kwargs)
        cur = _Cursor()
        return SimpleNamespace(cursor=lambda: cur, close=lambda: None)

    def driver(uri, auth):
        seen["neo4j"] = (uri, auth)
        return SimpleNamespace(session=lambda: _Session(log), close=lambda: None)

    monkeypatch.setattr(rref.psycopg2, "connect", connect)
    monkeypatch.setattr(rref, "GraphDatabase", SimpleNamespace(driver=driver))
    monkeypatch.setattr(rref.secure_env, "load_split_env", lambda *a, **k: None)
    monkeypatch.setattr(rref.secure_env, "get_secret", lambda key, default="": "pw")
    return log, seen


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["resolve_references.py", *argv])
    return rref.main()


def test_every_citation_is_merged_as_references(stores, monkeypatch):
    """Decision→Decision included: a citation is never grounding (INFORMED_BY)."""
    log, _ = stores
    assert _run(monkeypatch, "--apply") == 0
    merged = [m for _, q, _ in log if q for m in re.findall(r"MERGE \(s\)-\[r:(\w+)\]", q)]
    assert merged and set(merged) == {ONT.references}
    written = [(r["s"], r["t"]) for _, q, p in log if p and "rows" in p for r in p["rows"]]
    assert (10, 20) in written


def test_a_retrospective_endpoint_is_counted_not_planned():
    plan, skipped = rref.plan_edges(ROWS)
    # 50 is typed "Decision": still a Decision, so 50 → 10 is planned.
    assert [(s, t) for s, t, _ in plan] == [(10, 20), (10, 30), (50, 10)]
    assert skipped == 3  # 30 → retrospective 40, 40 → decision 20, "Retrospective" 60 → fact 30


def test_apply_retypes_earlier_informed_by_edges_in_the_write_transaction(stores, monkeypatch):
    log, _ = stores
    _run(monkeypatch, "--apply")
    assert [k for k, _, _ in log].count("execute_write") == 1
    tx = [q for k, q, _ in log if k == "tx"]
    assert len(tx) == 2
    assert f"[old:{ONT.informed_by}]" in tx[0] and "resolved_from = 'content'" in tx[0]
    assert "old.asserted_by IS NULL" in tx[0]  # never the operator's own grounding
    assert "DELETE old" in tx[0] and "ON CREATE" not in tx[0]
    assert f"r.retyped_from = '{ONT.informed_by}'" in tx[0]
    assert "UNWIND $rows" in tx[1]


def test_the_apply_report_splits_new_present_and_unwritten(stores, monkeypatch, capsys):
    _run(monkeypatch, "--apply")
    out = capsys.readouterr().out
    planned = len(rref.plan_edges(ROWS)[0])
    assert f"re-typed {RETYPED} {ONT.informed_by}" in out
    assert f"{CREATED} new {ONT.references} edge(s), {MATCHED - CREATED} already present, " \
           f"{planned - MATCHED} not written" in out


def test_a_dry_run_writes_nothing(stores, monkeypatch):
    log, _ = stores
    assert _run(monkeypatch) == 0
    assert "execute_write" not in [k for k, _, _ in log]
    queries = [q for _, q, _ in log if q]
    assert queries == [rref._COUNT_LEGACY]  # the re-type count, read-only, and nothing else
    assert "MERGE" not in queries[0] and "DELETE" not in queries[0]


def test_connection_settings_come_from_the_environment(stores, monkeypatch):
    _, seen = stores
    for key, value in {"PG_HOST": "db.example", "PG_PORT": "6543", "PG_USER": "alice",
                       "PG_DATABASE": "memory", "NEO4J_URI": "bolt://graph.example:7999",
                       "NEO4J_USER": "bob"}.items():
        monkeypatch.setenv(key, value)
    _run(monkeypatch)
    assert seen["pg"][1] == {"host": "db.example", "port": "6543", "user": "alice",
                             "password": "pw", "dbname": "memory"}
    assert seen["neo4j"] == ("bolt://graph.example:7999", ("bob", "pw"))
