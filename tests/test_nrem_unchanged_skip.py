"""The thematic fold's OUTPUT-IDENTITY skip (operator ruling 2026-08-11).

The defect these cover: `_consolidate_clusters` embedded and upserted every
eligible (project, domain) group on every sweep, unconditionally. The plan's
own rationale for deterministic ordering — "the summary is upserted and its
content compared across re-folds" — promised a comparison that was never
implemented, and the insight path's G3 freshness gate (§2.2: without it "a
gating group re-folds an identical insight every cycle") never got a thematic
twin. Measured live before the fix: the same two summaries rewritten every
~15 minutes for a full afternoon after the last save, their 20-entry
summary_history rings churned to identical snapshots, while the permanently
non-drainable outbox backlog kept `consolidation_due` true.

The contract under test:

  * a re-fold whose output is byte-identical to the ACTIVE row is skipped —
    no embedding, no write, no history append — and counted under the NEW
    `unchanged_clusters` key (never an alias for `eligible_clusters`);
  * a skipped cluster is EXCLUDED from the `eligible_clusters` census, so the
    ADR-018 stall verdict cannot read a fully-current corpus as "eligible
    backlog present, no fold succeeded" forever;
  * every divergence fails OPEN to folding — content, member set, entities —
    so no subset-triggered refold (P12 subset supersession, a superseded
    constituent shrinking membership, REM re-condensation) is ever
    suppressed;
  * a SUPERSEDED row on the same axis key is invisible to the check
    (Mechanism B retirement must always re-fold).

No DB, no Neo4j, no LLM — conventions of test_nrem_confidence.
"""
import datetime
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
import consolidation_loop as cl
from consolidation_loop import (
    ConsolidationDaemon,
    fetch_active_thematic_rows,
    thematic_fold_is_current,
)


# ── Stubs (test_nrem_confidence conventions) ─────────────────────────────────

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
        self.closed = False

    def cursor(self):
        return StubCursor(self._script, self.executed)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


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


# ── Fixture: the same two-fact 'general/ops' cluster test_nrem_confidence
#    folds, plus the byte-exact output that fold produces ───────────────────

_ROWS = [
    {"pg_id": 1, "content": "The consolidation daemon writes summaries",
     "project": "general", "domain": "ops"},
    {"pg_id": 2, "content": "The outbox worker applies rows",
     "project": "general", "domain": "ops"},
]

_CURRENT_CONTENT = (
    "[FACT kind=tested from=\"tests/test_x.py\" recorded=2026-07-11 pg_id=1] "
    "The consolidation daemon writes summaries\n"
    "[FACT kind=discussion recorded=2026-07-11 pg_id=2] "
    "The outbox worker applies rows"
)


def _script(active_rows):
    """Cursor script for one _consolidate_clusters pass: record_map fetch,
    dead-letter counts, then fetch_active_thematic_rows returning
    `active_rows`; everything after that (census short-circuits when all
    clusters skip; drop_out_of_scan and the fold writes take stub defaults
    or explicit entries appended by the caller)."""
    d = datetime.date(2026, 7, 11)
    return [
        {"rowcount": 2, "rows": [
            (1, "general", "fact", "tests/test_x.py", d, {"entities": ["Widget"]}),
            (2, "general", "fact", None, d, {}),
        ]},
        {"rowcount": 0, "rows": []},                       # dead-letter counts
        {"rowcount": len(active_rows), "rows": active_rows},  # active thematic rows
    ]


def _wire(monkeypatch, daemon, conn, finish):
    monkeypatch.setattr(cl, "DENSITY_THRESHOLD", 2)
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(cl, "_crun_start", lambda ct: 42)
    monkeypatch.setattr(cl, "_crun_finish",
                        lambda *a, **k: finish.update(args=a, kwargs=k))
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)


# ── The pure comparison ──────────────────────────────────────────────────────

def test_identical_output_is_current():
    row = (_CURRENT_CONTENT, [1, 2], ["Widget"])
    assert thematic_fold_is_current(row, _CURRENT_CONTENT, [1, 2], ["Widget"])


def test_no_active_row_is_never_current():
    """Mechanism B retirement: the retired row is invisible (NOT superseded
    filter), so the group MUST re-fold — `None` can never read as current."""
    assert not thematic_fold_is_current(None, _CURRENT_CONTENT, [1, 2], ["Widget"])


def test_content_divergence_folds():
    """Any text change — membership, rem_summary re-condensation, kind,
    origin, even pure line re-ordering — fails the check and re-folds."""
    row = (_CURRENT_CONTENT + " (stale)", [1, 2], ["Widget"])
    assert not thematic_fold_is_current(row, _CURRENT_CONTENT, [1, 2], ["Widget"])


def test_source_set_divergence_folds():
    """A superseded constituent shrinks the member set; a new fact grows it.
    Either way the stored set no longer matches and the group re-folds."""
    row = (_CURRENT_CONTENT, [1, 2, 3], ["Widget"])
    assert not thematic_fold_is_current(row, _CURRENT_CONTENT, [1, 2], ["Widget"])


def test_entity_divergence_folds():
    """`entities` is the one §3.1 payload field that can move without the
    text moving — it is compared in its own right."""
    row = (_CURRENT_CONTENT, [1, 2], ["Widget", "Gadget"])
    assert not thematic_fold_is_current(row, _CURRENT_CONTENT, [1, 2], ["Widget"])


def test_comparison_is_order_insensitive_on_sets_only():
    """source_pg_ids and entities compare as SETS (storage order is not
    semantic); content compares as EXACT BYTES (its order is the artifact)."""
    row = (_CURRENT_CONTENT, [2, 1], ["Widget"])
    assert thematic_fold_is_current(row, _CURRENT_CONTENT, [1, 2], ["Widget"])
    row = (_CURRENT_CONTENT, [1, 2], None)
    assert not thematic_fold_is_current(row, _CURRENT_CONTENT, [1, 2], ["Widget"])
    assert thematic_fold_is_current(row, _CURRENT_CONTENT, [1, 2], [])


# ── The fetch ────────────────────────────────────────────────────────────────

def test_fetch_skips_superseded_and_insight_rows_by_predicate():
    """The SQL itself must exclude superseded rows (retirement re-folds),
    insight-kind rows, and non-domain levels — the same scoping as the
    upsert's migration-032 arbiter. Asserted on the executed SQL because the
    stub cannot parse it and a live run is the release gate for that."""
    conn = StubConn(script=[{"rowcount": 0, "rows": []}])
    fetch_active_thematic_rows(conn, [("general", "ops")])
    sql = conn.executed[0][0]
    assert "NOT superseded" in sql
    assert "<> 'insight'" in sql
    assert "'level'" in sql and "'entity'" in sql


def test_fetch_with_no_keys_runs_no_query():
    conn = StubConn()
    assert fetch_active_thematic_rows(conn, []) == {}
    assert conn.executed == []


# ── The daemon pass ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_current_cluster_is_skipped_without_embedding_or_write(monkeypatch):
    """THE fix. An active row byte-identical to the computed output: no
    INSERT, no embedding call, folds 0/0, the census reports ZERO eligible
    clusters (stall-verdict guard), and extra carries unchanged_clusters=1."""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, session = daemon_with_fake_graph()
    conn = StubConn(script=_script(
        [("general", "ops", _CURRENT_CONTENT, [1, 2], ["Widget"])]))
    finish = {}
    _wire(monkeypatch, daemon, conn, finish)

    await daemon._consolidate_clusters(_ROWS)

    assert not any(s.startswith("INSERT INTO community_summaries")
                   for s, _ in conn.executed)
    assert daemon.get_embedding.await_count == 0
    assert finish["args"][1:5] == ("completed", 0, 0, 0)
    assert finish["kwargs"]["eligible_clusters"] == 0
    assert finish["kwargs"]["extra"]["unchanged_clusters"] == 1
    # No graph marking ran — nothing was folded.
    assert not any("consolidated = true" in q for q, _ in session.calls)


@pytest.mark.asyncio
async def test_divergent_active_row_still_folds(monkeypatch):
    """The fail-open direction: a stale active row (any divergence) folds
    exactly as before — the skip can only ever suppress an exact rewrite."""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, session = daemon_with_fake_graph()
    script = _script(
        [("general", "ops", "an older fold of this group", [1, 2], ["Widget"])])
    script += [
        {"rowcount": 2, "rows": []},          # census outbox timestamps
        {"rowcount": 0, "rows": []},          # drop_out_of_scan close
        {"rowcount": 1, "rows": [(90,)]},     # summary INSERT
        {"rowcount": 2, "rows": []},          # outbox flip
        {"rowcount": 0, "rows": []},          # supersession SELECT
    ]
    conn = StubConn(script=script)
    finish = {}
    _wire(monkeypatch, daemon, conn, finish)

    await daemon._consolidate_clusters(_ROWS)

    insert = next(p for s, p in conn.executed
                  if s.startswith("INSERT INTO community_summaries"))
    assert insert[0] == _CURRENT_CONTENT
    assert daemon.get_embedding.await_count == 1
    assert finish["args"][1:5] == ("completed", 1, 1, 0)
    assert finish["kwargs"]["eligible_clusters"] == 1
    # Nothing was skipped, so the key reports 0 via extra=None (the
    # pre-stage-5 byte-identical ledger shape is preserved when nothing
    # counted) — asserted so the key can never silently inflate.
    assert finish["kwargs"]["extra"] is None


@pytest.mark.asyncio
async def test_entity_only_divergence_still_folds(monkeypatch):
    """Same content, same members, different stored entities — the §3.1
    payload comparison alone must force the re-fold."""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, _session = daemon_with_fake_graph()
    script = _script(
        [("general", "ops", _CURRENT_CONTENT, [1, 2], ["Widget", "Gadget"])])
    script += [
        {"rowcount": 2, "rows": []},
        {"rowcount": 0, "rows": []},
        {"rowcount": 1, "rows": [(90,)]},
        {"rowcount": 2, "rows": []},
        {"rowcount": 0, "rows": []},
    ]
    conn = StubConn(script=script)
    finish = {}
    _wire(monkeypatch, daemon, conn, finish)

    await daemon._consolidate_clusters(_ROWS)

    insert = next(p for s, p in conn.executed
                  if s.startswith("INSERT INTO community_summaries"))
    assert json.loads(insert[1])["entities"] == ["Widget"]
    assert finish["args"][1:5] == ("completed", 1, 1, 0)
