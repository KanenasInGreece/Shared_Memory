"""Unit tests for the insight fold (Dreaming Cycle Plan to v2, §2.2-§2.5,
§3.2, §4.3 Path B — C2/C3/C4).

C4 (this file's main subject): the fold is now JUDGEMENT-INCLUSIVE — a
component's FULL ordered reach (decisions AND retrospectives), never
decision-only. `source_pg_ids` on the written insight is exactly that
judgement set (I9); the thematic summary(ies) it rests on live in the
SEPARATE `summary_ids` field (§3.2); `domains`/`entities` are derived from
the judgement rows themselves (multi-valued domains — the walk can cross
domains); `cypher_query` defers graph depth to read time. The embedded TEXT
is restricted to STRICTLY each judgement's own Title+Rationale (its `content`
verbatim) — no confidence, no alternatives, no retrospective-evidence line,
no grounding-edge line; all of that is deferred to the graph walk.

Insights are always-INSERT kind='insight' community_summaries; supersession
is the dedup; the ledger's decision and retrospective rows flip to
'consolidated' transactionally with the insight and close (by row id) after
the graph marking (now Decision OR Retrospective — criterion C, the PR #226
seam fix).

All Postgres/Neo4j/LLM I/O is stubbed — no live infrastructure required.
"""
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
# ⚠ Module reference captured HERE, at COLLECTION time — `test_rem_loop.py`
# dynamically re-execs consolidation_loop.py and OVERWRITES
# `sys.modules["consolidation_loop"]` at ITS OWN collection time (never
# restored). A LOCAL `import consolidation_loop as cl` done later, inside a
# test function body, would silently rebind to that swapped-in copy — a
# DIFFERENT module object than the one `ConsolidationDaemon` below was
# defined in, so monkeypatching it would patch the wrong module and the
# daemon would fall through to REAL (module-global) functions / a REAL
# `psycopg2.connect`. Every test in this file that needs to patch
# module-level names uses THIS `cl`, never a fresh runtime import.
import consolidation_loop as cl
from consolidation_loop import (
    ConsolidationDaemon,
    append_insight_references,
    close_ledger_rows_by_id,
    fetch_active_insight_rows,
    fetch_active_thematic_summary_id,
    fetch_insight_outbox_rows,
    fetch_judgement_types,
    fetch_open_retro_decision_ids,
    fetch_refold_insights,
    fetch_reversal_context,
    fetch_unreconciled_insights,
    insight_cypher_query,
    supersede_covered_summaries,
    write_insight_summary,
)


# ── Stubs (extends the test_outbox_ledger pattern with fetchone) ─────────────

class StubCursor:
    def __init__(self, script, executed):
        self._script = script
        self.executed = executed
        self._current = {"rowcount": 0, "rows": []}

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        self._current = self._script.pop(0) if self._script else {"rowcount": 0, "rows": []}

    @property
    def rowcount(self):
        return self._current["rowcount"]

    def fetchall(self):
        return self._current["rows"]

    def fetchone(self):
        rows = self._current["rows"]
        return rows[0] if rows else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class StubConn:
    def __init__(self, script=None):
        self._script = script or []
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return StubCursor(self._script, self.executed)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _AsyncCtx:
    def __init__(self, val):
        self._val = val
    async def __aenter__(self):
        return self._val
    async def __aexit__(self, *_):
        pass


class FakeResult:
    def __init__(self, rows=None):
        self._rows = rows or []
    async def data(self):
        return self._rows


class FakeSession:
    """Captures every (query, params) run against the fake Neo4j driver."""
    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])
    async def run(self, query, **params):
        self.calls.append((" ".join(query.split()), params))
        return self._results.pop(0) if self._results else FakeResult()


def daemon_with_fake_graph(results=None):
    daemon = ConsolidationDaemon()
    session = FakeSession(results)
    daemon.driver = MagicMock()
    daemon.driver.session = MagicMock(return_value=_AsyncCtx(session))
    return daemon, session


# ── fetch_open_retro_decision_ids ─────────────────────────────────────────────

def test_open_retro_ids_selects_retro_type_only():
    conn = StubConn(script=[{"rowcount": 2, "rows": [(245,), (267,)]}])
    assert fetch_open_retro_decision_ids(conn) == [245, 267]
    sql, _ = conn.executed[0]
    # v2 rows carry the retro's OWN pg_id; the DECISION id lives in
    # target_pg_id (legacy rows carry both, equal) — COALESCE keys on it.
    assert "COALESCE((cypher_params->>'target_pg_id')::bigint, pg_id)" in sql
    assert "= 'retrospective'" in sql


def test_open_retro_ids_excludes_pending_and_failed_rows():
    # pending/failed rows still owe the outbox worker a Neo4j write — the
    # HAD_OUTCOME edge does not exist yet, so they are not fold triggers.
    conn = StubConn(script=[{"rowcount": 0, "rows": []}])
    fetch_open_retro_decision_ids(conn)
    sql, _ = conn.executed[0]
    assert "status IN ('applied', 'rem_reviewed')" in sql


# ── fetch_refold_insights (C4: carries `metadata` for summary_ids/project) ────

def test_refold_targets_active_insights_overlapping_retro_ids():
    row = (70, "OutboxPattern", [245, 267], "old insight text",
           {"summary_ids": [12], "project": "shared-memory-GitHub"})
    conn = StubConn(script=[{"rowcount": 1, "rows": [row]}])
    assert fetch_refold_insights(conn, [245]) == [row]
    sql, params = conn.executed[0]
    assert "metadata->>'kind' = 'insight'" in sql
    assert "NOT superseded" in sql
    assert "source_pg_ids &&" in sql
    assert "content, metadata" in sql
    assert params == ([245],)


def test_refold_no_retro_ids_runs_no_query():
    conn = StubConn()
    assert fetch_refold_insights(conn, []) == []
    assert conn.executed == []


# ── fetch_insight_outbox_rows ─────────────────────────────────────────────────

def test_consumable_rows_snapshot_by_id_decision_and_retro_types():
    conn = StubConn(script=[{"rowcount": 2, "rows": [(101,), (102,)]}])
    assert fetch_insight_outbox_rows(conn, [245, 267]) == [101, 102]
    sql, _ = conn.executed[0]
    assert "SELECT id FROM neo4j_outbox" in sql
    assert "IN ('decision', 'retrospective')" in sql
    assert "status IN ('applied', 'rem_reviewed')" in sql
    # v2 retro rows carry their own pg_id — they are consumed via target_pg_id.
    assert "(cypher_params->>'target_pg_id')::bigint = ANY(%s)" in sql


def test_consumable_rows_empty_pg_ids_runs_no_query():
    conn = StubConn()
    assert fetch_insight_outbox_rows(conn, []) == []
    assert conn.executed == []


# ── fetch_judgement_types (C4 — per-id type for dead-letter key parity) ───────

def test_fetch_judgement_types_maps_ids_to_declared_type():
    conn = StubConn(script=[{"rowcount": 2, "rows": [(245, "decision"), (900, "retrospective")]}])
    assert fetch_judgement_types(conn, [245, 900]) == {245: "decision", 900: "retrospective"}


def test_fetch_judgement_types_empty_runs_no_query():
    conn = StubConn()
    assert fetch_judgement_types(conn, []) == {}
    assert conn.executed == []


# ── fetch_active_insight_rows (C4 — id + set + metadata for §2.5) ────────────

def test_active_insight_rows_returns_id_set_and_metadata():
    conn = StubConn(script=[{"rowcount": 1, "rows": [
        (70, [245, 267], {"summary_ids": [12], "domains": ["architecture"]}),
    ]}])
    out = fetch_active_insight_rows(conn)
    assert out == [(70, {245, 267}, {"summary_ids": [12], "domains": ["architecture"]})]
    sql, _ = conn.executed[0]
    assert "metadata->>'kind' = 'insight'" in sql
    assert "NOT superseded" in sql


def test_active_insight_rows_null_metadata_defaults_to_empty_dict():
    conn = StubConn(script=[{"rowcount": 1, "rows": [(70, [245], None)]}])
    assert fetch_active_insight_rows(conn) == [(70, {245}, {})]


# ── fetch_active_thematic_summary_id (C4) ─────────────────────────────────────

def test_active_thematic_summary_id_query_contract():
    conn = StubConn(script=[{"rowcount": 1, "rows": [(173,)]}])
    assert fetch_active_thematic_summary_id(conn, "shared-memory-GitHub", "architecture") == 173
    sql, params = conn.executed[0]
    assert "kind', 'thematic') <> 'insight'" in sql
    assert "NOT superseded" in sql
    assert "level', 'entity') = %s" in sql
    assert params == ("shared-memory-GitHub", "architecture", "domain")


def test_active_thematic_summary_id_none_when_no_active_row():
    conn = StubConn(script=[{"rowcount": 0, "rows": []}])
    assert fetch_active_thematic_summary_id(conn, "p", "d") is None


# ── append_insight_references (C4 — §2.5 identity 'same' case) ───────────────

def test_append_insight_references_adds_new_summary_and_domain():
    conn = StubConn(script=[
        {"rowcount": 1, "rows": [({"summary_ids": [12], "domains": ["architecture"]},)]},
        {"rowcount": 1, "rows": []},
    ])
    assert append_insight_references(conn, 70, 44, "infrastructure") is True
    update_sql, params = conn.executed[1]
    assert update_sql.startswith("UPDATE community_summaries SET metadata")
    written = json.loads(params[0])
    assert written["summary_ids"] == [12, 44]
    assert written["domains"] == ["architecture", "infrastructure"]
    assert params[1] == 70


def test_append_insight_references_deduplicates():
    conn = StubConn(script=[
        {"rowcount": 1, "rows": [({"summary_ids": [12], "domains": ["architecture"]},)]},
        {"rowcount": 1, "rows": []},
    ])
    append_insight_references(conn, 70, 12, "architecture")
    _, params = conn.executed[1]
    written = json.loads(params[0])
    assert written["summary_ids"] == [12]
    assert written["domains"] == ["architecture"]


def test_append_insight_references_none_summary_id_only_appends_domain():
    conn = StubConn(script=[
        {"rowcount": 1, "rows": [({"summary_ids": [], "domains": []},)]},
        {"rowcount": 1, "rows": []},
    ])
    append_insight_references(conn, 70, None, "infrastructure")
    _, params = conn.executed[1]
    written = json.loads(params[0])
    assert written["summary_ids"] == []
    assert written["domains"] == ["infrastructure"]


def test_append_insight_references_returns_false_when_retired_meanwhile():
    conn = StubConn(script=[{"rowcount": 0, "rows": []}])
    assert append_insight_references(conn, 70, 12, "architecture") is False
    assert len(conn.executed) == 1   # no UPDATE issued


# ── fetch_reversal_context (criterion D) ──────────────────────────────────────

def test_reversal_context_empty_when_no_open_ledger_row():
    conn = StubConn(script=[{"rowcount": 0, "rows": []}])
    assert fetch_reversal_context(conn, [201, 202]) == []
    assert len(conn.executed) == 1   # second query short-circuits


def test_reversal_context_finds_reverted_decision_and_its_retrospective():
    conn = StubConn(script=[
        {"rowcount": 1, "rows": [(199,)]},
        {"rowcount": 1, "rows": [(199, "Old decision title", 900, "It failed under load")]},
    ])
    out = fetch_reversal_context(conn, [201, 202])
    assert out == [{"decision_id": 199, "decision_title": "Old decision title",
                    "retro_id": 900, "retro_content": "It failed under load"}]
    sql1, params1 = conn.executed[0]
    assert "status = 'open'" in sql1 and "summary_kind = 'insight'" in sql1
    assert "trigger_kind = 'technical_docs'" in sql1
    assert params1 == ([201, 202],)
    sql2, _ = conn.executed[1]
    assert "rating' = 'reversed'" in sql2
    assert "COALESCE(d.superseded, false) = true" in sql2


def test_reversal_context_empty_judgement_ids_runs_no_query():
    conn = StubConn()
    assert fetch_reversal_context(conn, []) == []
    assert conn.executed == []


# ── write_insight_summary ─────────────────────────────────────────────────────

def test_insight_write_is_always_insert_never_upsert():
    # Resurrection trap (decision 276): a conflict-UPDATE would resurrect a
    # superseded row in place — the insight INSERT must carry NO ON CONFLICT.
    conn = StubConn(script=[{"rowcount": 1, "rows": [(77,)]}, {"rowcount": 2, "rows": []}])
    sid = write_insight_summary(conn, "insight", "{}", [0.1], [245, 267], [101, 102])
    assert sid == 77
    insert_sql, _ = conn.executed[0]
    assert insert_sql.startswith("INSERT INTO community_summaries")
    assert "ON CONFLICT" not in insert_sql


def test_insight_write_flips_consumed_rows_in_same_transaction():
    conn = StubConn(script=[{"rowcount": 1, "rows": [(77,)]}, {"rowcount": 2, "rows": []}])
    write_insight_summary(conn, "insight", "{}", [0.1], [245, 267], [101, 102])
    flip_sql, flip_params = conn.executed[1]
    assert "SET status = 'consolidated'" in flip_sql
    assert "id = ANY(%s)" in flip_sql          # by row id, never by pg_id
    assert flip_params == ([101, 102],)
    assert conn.commits == 0                   # commit is the caller's job


def test_insight_write_no_rows_skips_flip():
    conn = StubConn(script=[{"rowcount": 1, "rows": [(78,)]}])
    write_insight_summary(conn, "insight", "{}", [0.1], [245], [])
    assert len(conn.executed) == 1


# ── supersede_covered_summaries ───────────────────────────────────────────────

def _supersession_conn(old_rows):
    return StubConn(script=[{"rowcount": len(old_rows), "rows": old_rows}]
                    + [{"rowcount": 1, "rows": []} for _ in old_rows])


def test_equal_source_set_supersedes_prior_insight():
    # A re-fold writes the SAME source_pg_ids — the equal set must ride the
    # covered-subset rule and replace its predecessor.
    # Row shape after PR 7: (id, source_pg_ids, level, kind)
    # U5: the insight call site passes kind="insight" explicitly (kind
    # isolation is now unconditional, never a side effect of `level`).
    conn = _supersession_conn([(70, [245, 267], "entity", "insight")])
    assert supersede_covered_summaries(conn, 77, [245, 267], kind="insight") == [70]


def test_strict_subset_supersedes():
    conn = _supersession_conn([(70, [245], "entity", "insight")])
    assert supersede_covered_summaries(conn, 77, [245, 267], kind="insight") == [70]


def test_disjoint_and_superset_sources_survive():
    conn = _supersession_conn([
        (70, [1, 2, 3], "entity", "insight"),
        (71, [245, 267, 999], "entity", "insight"),
    ])
    assert supersede_covered_summaries(conn, 77, [245, 267], kind="insight") == []
    assert conn.commits == 0


# ── U5: kind isolation is UNCONDITIONAL, not a side effect of `level` ─────────

def test_kind_isolation_applies_even_with_no_level():
    # I13 / §5's "still unfixed" note: an insight fold (level=None, the real
    # call site's shape) must NEVER supersede a THEMATIC row, even when its
    # source_pg_ids happens to be a covered subset — `technical_docs` is one
    # shared id sequence across facts/decisions/retrospectives, so this is a
    # real collision risk, not a hypothetical one.
    conn = _supersession_conn([(70, [245, 267], "entity", "thematic")])
    assert supersede_covered_summaries(conn, 77, [245, 267], kind="insight") == []
    assert conn.commits == 0


def test_kind_isolation_default_is_thematic():
    # The fact-fold call site relies on the default kind="thematic" — an
    # insight row must never be swept up by it even at the same level name.
    conn = _supersession_conn([(70, [245, 267], "domain", "insight")])
    assert supersede_covered_summaries(conn, 77, [245, 267], level="domain") == []
    assert conn.commits == 0


def test_p12_same_level_only_when_level_passed():
    """Entity-level fold must not retire a domain-level summary (P12)."""
    conn = _supersession_conn([
        (70, [1, 2], "domain", "thematic"),   # coarser — different level
        (71, [1], "entity", "thematic"),      # same level, subset
    ])
    assert supersede_covered_summaries(
        conn, 99, [1, 2, 3], level="entity") == [71]


# ── close_ledger_rows_by_id ───────────────────────────────────────────────────

def test_close_by_id_deletes_only_consolidated_rows():
    conn = StubConn(script=[{"rowcount": 2, "rows": [(101, 245), (102, 267)]}])
    assert close_ledger_rows_by_id(conn, [101, 102]) == 2
    sql, params = conn.executed[0]
    assert sql.startswith("DELETE FROM neo4j_outbox")
    assert "status = 'consolidated'" in sql
    assert "id = ANY(%s)" in sql
    assert params == ([101, 102],)
    assert conn.commits == 1


def test_close_by_id_empty_list_is_noop():
    conn = StubConn()
    assert close_ledger_rows_by_id(conn, []) == 0
    assert conn.executed == []


# ── fetch_unreconciled_insights ───────────────────────────────────────────────

def test_unreconciled_insights_query_contract():
    row = (77, "OutboxPattern", [245, 267])
    conn = StubConn(script=[{"rowcount": 1, "rows": [row]}])
    assert fetch_unreconciled_insights(conn) == [row]
    sql, _ = conn.executed[0]
    assert "cs.metadata->>'kind' = 'insight'" in sql
    assert "o.status = 'consolidated'" in sql
    assert "IN ('decision', 'retrospective')" in sql
    assert "NOT cs.superseded" in sql


# ── v2 insight gate (Dreaming Cycle Plan to v2, §2.2-§2.4; C2) ────────────────
#
# insight_cluster_cypher's 1-hop shared-Entity match (with its ≥2-projects
# rule and hub-degree cap) is GONE — see test_project_identity.py's retired-
# P22 note and test_project_axis.py's decision_cycles rewrite. The pure walk/
# gate/component/identity functions themselves (I1-I10) are unit-tested in
# tests/test_insight_gate.py; these two tests cover the INTEGRATION —
# `_find_fresh_insight_clusters` wiring `_find_grounded_fact_groups`
# (G1, reused) to `insight_gate.walk_group_reached_set` /
# `passes_insight_gate` (G2+G3) end to end.

_GATING_GROUP_ROWS = [
    {"pg_id": 101, "content": "f101", "project": "proj", "domain": "dom"},
    {"pg_id": 102, "content": "f102", "project": "proj", "domain": "dom"},
    {"pg_id": 103, "content": "f103", "project": "proj", "domain": "dom"},
]


@pytest.mark.asyncio
async def test_fresh_insight_clusters_returns_shape_for_a_gating_group():
    """A (project, domain) group with 3 grounded facts (G1, DENSITY_THRESHOLD)
    whose walk reaches one fresh Decision and one fresh Retrospective (G2+G3)
    yields exactly one component — returned with no entity anchor (I1),
    both the decision-only view AND (C4) the full judgement reach plus a
    per-id type map for the caller's dead-letter key."""
    daemon, session = daemon_with_fake_graph([
        FakeResult(_GATING_GROUP_ROWS),          # _find_grounded_fact_groups
        FakeResult([                              # walk layer 1 (the 3 facts)
            # Both ground on fact 101 (I5: shared fact -> same component).
            {"src": 101, "dst": 201, "dst_label": "Decision", "dst_consolidated": False},
            {"src": 101, "dst": 202, "dst_label": "Retrospective", "dst_consolidated": False},
        ]),
        FakeResult([]),                            # walk layer 2 — fixpoint
    ])
    out = await daemon._find_fresh_insight_clusters()
    assert len(out) == 1
    cluster = out[0]
    # D3 (fact:1189): an honest project/domain DISPLAY label — never a gate
    # predicate (I1 is about GATING, not this string; the walk/gate/identity
    # assertions below are untouched by it).
    assert cluster["entity"] == "proj/dom"
    assert cluster["decision_ids"] == [201]
    assert cluster["projects"] == ["proj"]
    assert cluster["domain"] == "dom"
    assert cluster["judgement_ids"] == [201, 202]
    assert cluster["judgement_types"] == {201: "Decision", 202: "Retrospective"}
    assert cluster["has_retrospective"] is True
    # The walk step query itself carries the closed relation set and the I10
    # exclusion — not the old entity/HAD_OUTCOME-existence gate.
    walk_query, _ = session.calls[1]
    assert "GROUNDED_IN" in walk_query and "HAD_OUTCOME" in walk_query
    assert "coalesce(m.superseded, false) = true" in walk_query
    assert "project_ids" not in walk_query   # I2 — no project count anywhere


@pytest.mark.asyncio
async def test_fresh_insight_clusters_skips_a_group_with_no_retrospective_reached():
    """G2: a gating group whose walk reaches only Decisions yields nothing —
    however many decisions it has (I6, exercised end-to-end here; the pure
    predicate itself is mutation-checked in test_insight_gate.py)."""
    daemon, _session = daemon_with_fake_graph([
        FakeResult(_GATING_GROUP_ROWS),
        FakeResult([
            {"src": 101, "dst": 201, "dst_label": "Decision", "dst_consolidated": False},
            {"src": 102, "dst": 203, "dst_label": "Decision", "dst_consolidated": False},
        ]),
        FakeResult([]),
    ])
    out = await daemon._find_fresh_insight_clusters()
    assert out == []


@pytest.mark.asyncio
async def test_generate_insight_mock_mode(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, _ = daemon_with_fake_graph()
    out = await daemon.generate_insight("OutboxPattern", ["[DECISION] a", "[DECISION] b"])
    assert "OutboxPattern" in out and "2" in out


@pytest.mark.asyncio
async def test_generate_insight_mock_mode_echoes_reversal_lines(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, _ = daemon_with_fake_graph()
    out = await daemon.generate_insight(
        "OutboxPattern", ["[DECISION] a"],
        reversal_lines=["Decision pg_id=199 (\"Old title\") was REVERTED. Reversing "
                        "retrospective pg_id=900: it failed"])
    assert "was REVERTED" in out and "it failed" in out


# ── _fold_insight (stubbed end-to-end) — C4 judgement-inclusive rewrite ───────

def _fold_script():
    """Postgres responses in _fold_insight's execution order (C4 shape)."""
    return [
        # 1. judgement content fetch — (id, content, project, type, metadata)
        {"rowcount": 2, "rows": [
            (245, "Decision A\n\nrationale A", "shared-memory-GitHub", "decision",
             {"entities": ["OutboxPattern"]}),
            (267, "Decision B\n\nrationale B", "shared-memory-GitHub", "decision", {}),
        ]},
        # 2. fetch_reversal_context leg 1 (open ledger rows) — none
        {"rowcount": 0, "rows": []},
        # 3. fetch_insight_outbox_rows snapshot
        {"rowcount": 2, "rows": [(101,), (102,)]},
        # 4. write_insight_summary INSERT
        {"rowcount": 1, "rows": [(77,)]},
        # 5. write_insight_summary ledger flip
        {"rowcount": 2, "rows": []},
        # 6. supersession SELECT (id, source_pg_ids, level, kind) — PR 7 shape
        {"rowcount": 1, "rows": [(70, [245, 267], "entity", "insight")]},
        # 7. supersession UPDATE
        {"rowcount": 1, "rows": []},
        # 8. close_ledger_rows_by_id DELETE
        {"rowcount": 2, "rows": [(101, 245), (102, 267)]},
    ]


@pytest.mark.asyncio
async def test_fold_insight_full_path(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, session = daemon_with_fake_graph()
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)
    conn = StubConn(script=_fold_script())

    assert await daemon._fold_insight(
        conn, "OutboxPattern", [245, 267], summary_ids=[173],
        project="shared-memory-GitHub") is True

    sqls = [s for s, _ in conn.executed]
    insert = next(s for s in sqls if s.startswith("INSERT INTO community_summaries"))
    assert "ON CONFLICT" not in insert
    # metadata carries the §3.2 contract
    meta = json.loads(conn.executed[3][1][1])
    assert meta["kind"] == "insight"
    assert meta["project"] == "shared-memory-GitHub"           # singular, per §3.2
    assert "projects" not in meta                                # old field is GONE
    assert meta["domains"] == []                                  # no domain metadata on either row here
    assert meta["entities"] == ["OutboxPattern"]
    # ⛔ I9 — judgement pg_ids ONLY.
    assert meta["source_pg_ids"] == [245, 267]
    # ⛔ §3.2 — summary_ids is a SEPARATE field, caller-supplied here.
    assert meta["summary_ids"] == [173]
    assert "cypher_query" in meta and "245" in meta["cypher_query"]
    # The ONLY Neo4j calls are the WRITE-side graph marking + SUPERSEDES edge
    # after the Postgres commit (C4 removed the READ-side
    # `_fetch_outcome_edges`/`_fetch_grounding_edges` the pre-C4 prompt
    # used) — and the marking now matches EITHER label (criterion C, the
    # PR #226 seam fix).
    assert len(session.calls) == 2
    mark_query, mark_params = session.calls[0]
    assert "(d:Decision OR d:Retrospective)" in mark_query
    assert mark_params["judgement_ids"] == [245, 267]
    assert conn.commits == 3


@pytest.mark.asyncio
async def test_fold_insight_blocks_are_strictly_title_and_rationale(monkeypatch):
    """§3.2 — the block for each judgement is its own content VERBATIM, with
    NO confidence line, NO alternatives line, NO retrospective-outcome line,
    NO grounding-edge line — all of that is gone from the pre-C4 prompt."""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, session = daemon_with_fake_graph()
    daemon.generate_insight = AsyncMock(
        return_value="Decision A rationale A; Decision B rationale B.")
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)
    conn = StubConn(script=_fold_script())

    assert await daemon._fold_insight(conn, "OutboxPattern", [245, 267]) is True

    blocks = daemon.generate_insight.call_args.args[1]
    d245 = next(b for b in blocks if b.startswith("[DECISION pg_id=245"))
    assert d245 == "[DECISION pg_id=245 project=shared-memory-GitHub]\nDecision A\n\nrationale A"
    assert "CONFIDENCE" not in d245
    assert "ALTERNATIVE" not in d245
    assert "RETROSPECTIVE" not in d245
    assert "GROUNDING" not in d245
    # No Neo4j READ ran to build the blocks (C4 removed the outcome/
    # grounding-edge fetches the pre-C4 prompt used) — only the write-side
    # marking + SUPERSEDES edge after commit.
    assert not any(
        rel in q for q in (c[0] for c in session.calls)
        for rel in ("HAD_OUTCOME", "GROUNDED_IN", "CONSIDERED", "REJECTED"))


@pytest.mark.asyncio
async def test_fold_insight_retrospective_block_labelled_and_ordered(monkeypatch):
    """A retrospective in the judgement set gets its own [RETROSPECTIVE] block
    (its content verbatim — retro-as-record), in ascending pg_id order
    alongside the decision it evaluates — never folded into the decision's
    own block."""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, session = daemon_with_fake_graph()
    daemon.generate_insight = AsyncMock(
        return_value="Decision A held under load; validated by the retrospective.")
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)
    conn = StubConn(script=[
        {"rowcount": 2, "rows": [
            (245, "Decision A\n\nrationale A", "shared-memory-GitHub", "decision", {}),
            (900, "held under load", "shared-memory-GitHub", "retrospective", {}),
        ]},
        {"rowcount": 0, "rows": []},                       # reversal leg 1
        {"rowcount": 2, "rows": [(101,), (102,)]},
        {"rowcount": 1, "rows": [(77,)]},
        {"rowcount": 2, "rows": []},
        {"rowcount": 0, "rows": []},
        {"rowcount": 2, "rows": [(101, 245), (102, 900)]},
    ])

    assert await daemon._fold_insight(conn, "OutboxPattern", [245, 900]) is True

    blocks = daemon.generate_insight.call_args.args[1]
    assert blocks[0].startswith("[DECISION pg_id=245")
    assert blocks[1] == "[RETROSPECTIVE pg_id=900 project=shared-memory-GitHub]\nheld under load"


@pytest.mark.asyncio
async def test_fold_insight_domains_and_entities_union_across_judgements(monkeypatch):
    """§3.2 domains — MULTI-VALUED: a judgement grounded in a different
    domain than another still contributes its own domain to the union.
    entities similarly union across every judgement's own list."""
    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, _ = daemon_with_fake_graph()
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)
    conn = StubConn(script=[
        {"rowcount": 2, "rows": [
            (245, "Decision A", "shared-memory-GitHub", "decision",
             {"domain": "architecture", "entities": ["OutboxPattern"]}),
            (267, "Decision B", "shared-memory-GitHub", "decision",
             {"domain": "infrastructure", "entities": ["GPUPool"]}),
        ]},
        {"rowcount": 0, "rows": []},
        {"rowcount": 2, "rows": [(101,), (102,)]},
        {"rowcount": 1, "rows": [(77,)]},
        {"rowcount": 2, "rows": []},
        {"rowcount": 0, "rows": []},
        {"rowcount": 2, "rows": [(101, 245), (102, 267)]},
    ])

    assert await daemon._fold_insight(conn, "OutboxPattern", [245, 267]) is True
    meta = json.loads(conn.executed[3][1][1])
    assert meta["domains"] == ["architecture", "infrastructure"]
    assert meta["entities"] == ["GPUPool", "OutboxPattern"]


@pytest.mark.asyncio
async def test_fold_insight_summary_ids_never_mixed_with_source_pg_ids(monkeypatch):
    """⛔ I9 / §3.2 — the two id sequences must never be conflated: a
    thematic summary id sitting inside source_pg_ids would resolve, e.g.,
    summary 173 against technical_docs row 173 and render a WRONG
    provenance record, silently."""
    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, _ = daemon_with_fake_graph()
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)
    conn = StubConn(script=_fold_script())

    await daemon._fold_insight(conn, "OutboxPattern", [245, 267], summary_ids=[173])
    meta = json.loads(conn.executed[3][1][1])
    assert 173 not in meta["source_pg_ids"]
    assert meta["source_pg_ids"] == [245, 267]
    assert meta["summary_ids"] == [173]


@pytest.mark.asyncio
async def test_fold_insight_reversal_note_injected_and_anchored(monkeypatch):
    """Criterion D — when this fold's constituents close an OPEN
    refold_ledger row whose trigger was a reversed decision, the payload
    must state what was reverted and why. Independent of the walk/gate."""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, _ = daemon_with_fake_graph()
    captured = {}
    async def fake_generate_insight(entity, blocks, previous_insight=None,
                                    corrective=None, reversal_lines=None):
        captured["reversal_lines"] = reversal_lines
        return ("Decision A holds. This supersedes the reverted decision "
                "(\"Old decision title\") because it failed under load.")
    daemon.generate_insight = fake_generate_insight
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)
    conn = StubConn(script=[
        {"rowcount": 2, "rows": [
            (245, "Decision A\n\nrationale A", "shared-memory-GitHub", "decision", {}),
            (267, "Decision B\n\nrationale B", "shared-memory-GitHub", "decision", {}),
        ]},
        {"rowcount": 1, "rows": [(199,)]},                                     # reversal leg 1
        {"rowcount": 1, "rows": [(199, "Old decision title", 900,
                                  "it failed under load")]},                  # reversal leg 2
        {"rowcount": 2, "rows": [(101,), (102,)]},
        {"rowcount": 1, "rows": [(77,)]},
        {"rowcount": 2, "rows": []},
        {"rowcount": 0, "rows": []},
        {"rowcount": 2, "rows": [(101, 245), (102, 267)]},
    ])

    assert await daemon._fold_insight(conn, "OutboxPattern", [245, 267]) is True
    assert captured["reversal_lines"]
    assert "199" in captured["reversal_lines"][0]
    assert "Old decision title" in captured["reversal_lines"][0]
    assert "it failed under load" in captured["reversal_lines"][0]


@pytest.mark.asyncio
async def test_fold_insight_aborts_when_llm_fails(monkeypatch):
    # No insight → no Postgres write, ledger rows stay open (durable retry).
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, _ = daemon_with_fake_graph()
    daemon.generate_insight = AsyncMock(return_value=None)
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)
    conn = StubConn(script=_fold_script())

    assert await daemon._fold_insight(conn, "OutboxPattern", [245, 267]) is False

    assert not any(s.startswith("INSERT INTO community_summaries")
                   for s, _ in conn.executed)
    # judgement fetch + reversal-context leg1 + outbox snapshot ran; the
    # read-transaction commit is the only commit before the LLM aborted.
    assert conn.commits == 1


@pytest.mark.asyncio
async def test_fold_insight_skips_singleton_cluster(monkeypatch):
    # Fewer than two source judgements found in Postgres → no fold (a
    # solitary judgement round-trip is pure duplication — decision 245).
    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, _ = daemon_with_fake_graph()
    conn = StubConn(script=[{"rowcount": 1, "rows": [
        (245, "Decision A", "p1", "decision", {})]}])

    assert await daemon._fold_insight(conn, "OutboxPattern", [245, 999]) is False
    assert len(conn.executed) == 1  # only the content fetch ran
    assert conn.commits == 0


@pytest.mark.asyncio
async def test_fold_insight_project_falls_back_to_mode_of_judgement_rows(monkeypatch):
    """When the caller does not pass `project` explicitly (e.g. a re-fold
    whose previous metadata carried none), fall back to the most common
    project among the fetched judgement rows rather than leaving it blank."""
    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, _ = daemon_with_fake_graph()
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)
    conn = StubConn(script=[
        {"rowcount": 2, "rows": [
            (245, "Decision A", "shared-memory-GitHub", "decision", {}),
            (267, "Decision B", "shared-memory-GitHub", "decision", {}),
        ]},
        {"rowcount": 0, "rows": []},
        {"rowcount": 2, "rows": [(101,), (102,)]},
        {"rowcount": 1, "rows": [(77,)]},
        {"rowcount": 2, "rows": []},
        {"rowcount": 0, "rows": []},
        {"rowcount": 2, "rows": [(101, 245), (102, 267)]},
    ])
    await daemon._fold_insight(conn, "OutboxPattern", [245, 267])
    meta = json.loads(conn.executed[3][1][1])
    assert meta["project"] == "shared-memory-GitHub"


# ── run_insight_cycle wiring (regression for the fresh-cluster call site) ──────

@pytest.mark.asyncio
async def test_run_insight_cycle_calls_fold_with_compatible_signature(monkeypatch):
    """Regression — the fresh-cluster path in run_insight_cycle must call
    _fold_insight with arguments its signature accepts.

    A real bug (fixed 2026-06-23) passed projects=c.get("projects") to
    _fold_insight(), which has no such parameter, raising a TypeError that
    the cycle's try/except swallowed — so EVERY fresh fold crashed silently
    and insight stayed at 0 for ~12 days. The isolated _fold_insight tests
    above could not catch it because run_insight_cycle is the only place that
    call site is wired. create_autospec enforces the real signature, so
    reintroducing an unexpected kwarg makes the call raise (swallowed),
    dropping await_count to 0 and failing this test."""
    from unittest.mock import create_autospec

    class _Conn(StubConn):
        def close(self):
            pass

    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(cl, "fetch_unreconciled_insights", lambda conn: [])
    monkeypatch.setattr(cl, "fetch_open_retro_decision_ids", lambda conn: [])
    monkeypatch.setattr(cl, "fetch_refold_insights", lambda conn, ids: [])
    monkeypatch.setattr(cl, "fetch_active_insight_rows", lambda conn: [])
    monkeypatch.setattr(cl, "fetch_active_thematic_summary_id", lambda conn, p, d: None)

    daemon, _ = daemon_with_fake_graph()
    daemon._find_fresh_insight_clusters = AsyncMock(return_value=[
        {"entity": "OutboxPattern", "decision_ids": [245, 267],
         "judgement_ids": [245, 267], "judgement_types": {245: "Decision", 267: "Decision"},
         "projects": ["shared-memory-GitHub"], "domain": "architecture"},
    ])
    daemon._fold_insight = create_autospec(daemon._fold_insight, return_value=True)

    await daemon.run_insight_cycle()

    assert daemon._fold_insight.await_count == 1
    assert "projects" not in daemon._fold_insight.await_args.kwargs
    assert daemon._fold_insight.await_args.args[2] == [245, 267]   # judgement_ids, not decision_ids


@pytest.mark.asyncio
async def test_run_insight_cycle_dead_lettered_cluster_excluded_from_eligible_census(monkeypatch):
    """D1 (fact:1189, decision:1121/I7) — the insight cycle's own copy of
    the same defect: rec.eligible_clusters used to be computed over ALL
    fresh clusters BEFORE the NREM_FOLD_FAIL_CAP dead-letter filter, so a
    permanently dead-lettered cluster counted as eligible backlog forever
    and _consolidation_stall_verdict (coordinator.py) could never clear.
    Two fresh clusters this pass; one is dead-lettered. eligible_clusters
    must report 1 (not 2), and the exclusion is visible separately as
    dead_lettered_clusters=1 — a NEW key, not folded into eligible_clusters'
    own meaning."""
    monkeypatch.setattr(cl, "NREM_FOLD_FAIL_CAP", 1)

    class _Conn(StubConn):
        def close(self):
            pass

    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(cl, "fetch_unreconciled_insights", lambda conn: [])
    monkeypatch.setattr(cl, "fetch_open_retro_decision_ids", lambda conn: [])
    monkeypatch.setattr(cl, "fetch_refold_insights", lambda conn, ids: [])
    monkeypatch.setattr(cl, "fetch_active_insight_rows", lambda conn: [])
    monkeypatch.setattr(cl, "fetch_active_thematic_summary_id", lambda conn, p, d: None)

    dead_ids = [345, 367]
    dead_types = {345: "Decision", 367: "Decision"}
    # The dead-lettered cluster's own content-derived identity (C4's
    # per-judgement-type key — decision 882's fold-key/label split).
    dead_key = cl._judgement_fold_identity(dead_ids, dead_types)
    monkeypatch.setattr(cl, "fetch_fold_dead_letter_counts", lambda: {dead_key: 1})

    daemon, _ = daemon_with_fake_graph()
    daemon._find_fresh_insight_clusters = AsyncMock(return_value=[
        {"entity": "shared-memory-GitHub/architecture", "decision_ids": [245, 267],
         "judgement_ids": [245, 267], "judgement_types": {245: "Decision", 267: "Decision"},
         "projects": ["shared-memory-GitHub"], "domain": "architecture"},
        {"entity": "shared-memory-GitHub/infrastructure", "decision_ids": dead_ids,
         "judgement_ids": dead_ids, "judgement_types": dead_types,
         "projects": ["shared-memory-GitHub"], "domain": "infrastructure"},
    ])
    daemon._fold_insight = AsyncMock(return_value=True)

    finish = {}
    monkeypatch.setattr(cl, "_crun_start", lambda ct: 42)
    monkeypatch.setattr(cl, "_crun_finish",
                        lambda *a, **k: finish.update(args=a, kwargs=k))

    await daemon.run_insight_cycle()

    # Only the non-dead-lettered cluster ([245, 267]) actually folded.
    assert daemon._fold_insight.await_count == 1
    assert daemon._fold_insight.await_args.args[2] == [245, 267]
    assert finish["kwargs"]["eligible_clusters"] == 1
    assert finish["kwargs"]["extra"]["dead_lettered_clusters"] == 1


@pytest.mark.asyncio
async def test_run_insight_cycle_same_identity_appends_reference_not_a_new_fold(monkeypatch):
    """§2.5 / criterion G — a fresh cluster whose judgement set exactly
    matches an existing active insight's is NOT folded again; the
    triggering thematic summary id and domain are appended to the existing
    insight instead."""

    class _Conn(StubConn):
        def close(self):
            pass

    # The main insight-cycle conn (the SELECT+UPDATE inside
    # append_insight_references run against it, since run_insight_cycle
    # calls that helper with its own outer `conn`). `_crun_start`/
    # `_crun_finish`/`fetch_calibration_gate` each open their OWN throwaway
    # connection (real code: a fresh `psycopg2.connect` per call) — give
    # those an empty-scripted stub so they never consume THIS conn's script.
    conn = _Conn(script=[
        {"rowcount": 1, "rows": [({"summary_ids": [], "domains": ["architecture"]},)]},
        {"rowcount": 1, "rows": []},   # UPDATE metadata
    ])
    first_connect = {"done": False}
    def _connect(*a, **k):
        if not first_connect["done"]:
            first_connect["done"] = True
            return conn
        return _Conn()
    monkeypatch.setattr(cl.psycopg2, "connect", _connect)
    monkeypatch.setattr(cl, "fetch_unreconciled_insights", lambda c: [])
    monkeypatch.setattr(cl, "fetch_open_retro_decision_ids", lambda c: [])
    monkeypatch.setattr(cl, "fetch_refold_insights", lambda c, ids: [])
    monkeypatch.setattr(cl, "fetch_active_insight_rows",
                        lambda c: [(70, {245, 267}, {"summary_ids": [], "domains": []})])
    monkeypatch.setattr(cl, "fetch_active_thematic_summary_id", lambda c, p, d: 44)

    daemon, _ = daemon_with_fake_graph()
    daemon._find_fresh_insight_clusters = AsyncMock(return_value=[
        {"entity": "OutboxPattern", "decision_ids": [245, 267],
         "judgement_ids": [245, 267], "judgement_types": {245: "Decision", 267: "Decision"},
         "projects": ["shared-memory-GitHub"], "domain": "infrastructure"},
    ])
    daemon._fold_insight = AsyncMock(return_value=True)

    await daemon.run_insight_cycle()

    daemon._fold_insight.assert_not_awaited()
    update_sql, params = next(
        (s, p) for s, p in conn.executed
        if s.startswith("UPDATE community_summaries SET metadata"))
    written = json.loads(params[0])
    assert written["summary_ids"] == [44]
    assert written["domains"] == ["architecture", "infrastructure"]


@pytest.mark.asyncio
async def test_run_insight_cycle_covered_identity_skips_without_appending(monkeypatch):
    """A freshly-walked reach that is a STRICT SUBSET of an existing active
    insight's reach ('covered', not in §2.5's LOCKED table — the walk only
    ever grows in the documented scenario) is skipped with no write at
    all — nothing new to add, and folding it would create a redundant
    duplicate insight."""

    class _Conn(StubConn):
        def close(self):
            pass

    conn = _Conn()
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(cl, "fetch_unreconciled_insights", lambda c: [])
    monkeypatch.setattr(cl, "fetch_open_retro_decision_ids", lambda c: [])
    monkeypatch.setattr(cl, "fetch_refold_insights", lambda c, ids: [])
    monkeypatch.setattr(cl, "fetch_active_insight_rows",
                        lambda c: [(70, {245, 267, 999}, {})])

    daemon, _ = daemon_with_fake_graph()
    daemon._find_fresh_insight_clusters = AsyncMock(return_value=[
        {"entity": "OutboxPattern", "decision_ids": [245],
         "judgement_ids": [245], "judgement_types": {245: "Decision"},
         "projects": ["shared-memory-GitHub"], "domain": "architecture"},
    ])
    daemon._fold_insight = AsyncMock(return_value=True)

    await daemon.run_insight_cycle()

    daemon._fold_insight.assert_not_awaited()
    assert not any(s.startswith("UPDATE community_summaries") for s, _ in conn.executed)


# ── insight_cypher_query (§3.2, pure) ─────────────────────────────────────────

def test_insight_cypher_query_is_self_contained_and_deterministic():
    q1 = insight_cypher_query([267, 245])
    q2 = insight_cypher_query([245, 267])
    assert q1 == q2                       # sorted — order-independent
    assert "245" in q1 and "267" in q1
    assert "Decision" in q1 and "Retrospective" in q1
    assert "CONSIDERED" in q1 and "REJECTED" in q1 and "UNDER_CONDITIONS" in q1
