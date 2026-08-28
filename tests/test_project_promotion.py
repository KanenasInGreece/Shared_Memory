"""Invariant P6 — one writer, one transition, parked → real, both stores.

A record whose project could not be established at first write is PARKED. Making
it real later is a state transition, and the whole point of routing it through
one writer is that a gating property must not have a second writer — the shape of
a defect this framework has already shipped once.

The transition is ONE-WAY on purpose, which is exactly why these tests matter: a
`real → real` write that slips through cannot be undone through the supported
path, so the refusal is the safety property, not a nicety.

Also covers P19 — a record carries at most ONE project edge. The graph writer used
to be a bare MERGE, which is correct only while every target has no edge. Measured
before it changed: 35 parked facts already carried an edge Postgres could not
justify, and 4 spine nodes carried two.

⚠ The predicates are asserted DIRECTLY rather than through source text. A guard
disabled with `if False and …` leaves its own text in the file, so a test that
greps the source passes against a dead guard — that has happened here twice.

No DB or Neo4j required.
"""
import os
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

from project_axis import SENTINEL
from project_promotion import (
    is_parked, promotion_refusal, sole_project, promote_record,
    METHOD_GROUNDING,
)


# ── The source condition: what counts as parked ──────────────────────────────

def test_parked_means_absent_or_sentinel_not_sentinel_alone():
    """Both absences mean "no project has been established".

    A strictly sentinel-only test would have been correct on paper and reached
    NOTHING: when this writer was built, 0 records carried the sentinel and all
    126 parked facts were parked by absence.
    """
    assert is_parked(None) is True
    assert is_parked("") is True
    assert is_parked("   ") is True
    assert is_parked(SENTINEL) is True
    assert is_parked(f"  {SENTINEL}  ") is True
    assert is_parked("shared-memory-GitHub") is False


def test_a_non_string_project_is_parked_not_an_exception():
    """Metadata is client-supplied; a wrong type is a parked record, not a 500."""
    assert is_parked(123) is True
    assert is_parked({"name": "x"}) is True
    assert is_parked(["x"]) is True


# ── The transition: what may and may not happen ──────────────────────────────

def test_parked_to_real_is_allowed():
    assert promotion_refusal(None, "shared-memory-GitHub") is None
    assert promotion_refusal(SENTINEL, "shared-memory-GitHub") is None
    assert promotion_refusal("", "project-b") is None


def test_real_to_real_is_refused():
    """The safety property. Promotion is one-way, so overwriting an established
    project here would be unrepairable through this path."""
    refusal = promotion_refusal("project-b", "shared-memory-GitHub")
    assert refusal is not None
    assert "project-b" in refusal


def test_real_to_the_same_value_is_still_refused():
    """Idempotence must not be bought by weakening the guard: 'it was already
    that value' is still a real current value, and accepting it would make the
    ledger record a transition that never happened."""
    assert promotion_refusal("smg", "smg") is not None


def test_promoting_to_the_sentinel_is_refused():
    """That is parking, not promotion — and the record is already parked."""
    assert promotion_refusal(None, SENTINEL) is not None
    assert promotion_refusal(SENTINEL, SENTINEL) is not None


def test_promoting_to_an_empty_target_is_refused():
    assert promotion_refusal(None, "") is not None
    assert promotion_refusal(None, "   ") is not None
    assert promotion_refusal(None, None) is not None


# ── Caller 1's ambiguity guard ───────────────────────────────────────────────

def test_sole_project_requires_unanimity_among_real_values():
    assert sole_project(["smg", "smg"]) == "smg"
    assert sole_project(["smg"]) == "smg"
    assert sole_project(["smg", "project-b"]) is None
    assert sole_project([]) is None
    assert sole_project(None) is None


def test_abstentions_are_not_dissent():
    """A judgement naming no project has not disagreed — it has said nothing.
    Counting an absence as a second opinion would park every fact whose citing
    judgements include one untagged record."""
    assert sole_project(["smg", None, "", SENTINEL]) == "smg"


def test_two_real_projects_leave_the_record_parked():
    """Parked is visible and repairable; wrong is neither."""
    assert sole_project(["smg", "project-b", "smg"]) is None


# ── The writer: all three writes, or none ────────────────────────────────────

class FakeConn:
    """Minimal asyncpg-shaped connection. Records what was written."""

    def __init__(self, current, project_id=11):
        self.current = current
        self.executed = []
        self.locked = False
        # The registry identity behind a project name (migration 027). Added at
        # v0.9.69 (item 6, ruled R3): this fixture never modelled the lookup at
        # all, and `_project_identity` now RAISES rather than silently falling
        # back to a name-keyed node when it cannot answer — so "no fetchval on
        # the fake connection" became a failure instead of a shrug.
        self.project_id = project_id

    async def fetchval(self, sql, *args):
        return self.project_id

    async def fetchrow(self, sql, *args):
        if "FOR UPDATE" in sql:
            self.locked = True
            return {"project": self.current}
        return None

    async def fetch(self, sql, *args):
        return []

    async def execute(self, sql, *args):
        self.executed.append((" ".join(sql.split()), args))
        if sql.lstrip().startswith("UPDATE technical_docs"):
            self.current = args[1]

    def statements(self):
        return [s for s, _ in self.executed]


@pytest.mark.asyncio
async def test_promotion_writes_metadata_outbox_and_ledger():
    """Both stores plus the durable record, in the caller's transaction.

    ⚠ Matched with startswith, never with `in`. A substring check is satisfied
    by the statement appearing in a COMMENT — deleting the ledger write and
    leaving `-- INSERT INTO project_promotions` behind survived this test until
    it was written this way. That is the same failure mode as asserting on
    source text, reached from a different direction.
    """
    conn = FakeConn(current=None)
    result = await promote_record(
        conn, 549, "shared-memory-GitHub",
        method=METHOD_GROUNDING, actor="claude", note="grounded by pg_id=1000",
    )
    assert result["promoted"] is True
    assert result["from"] is None
    stmts = conn.statements()
    assert sum(s.startswith("UPDATE technical_docs") for s in stmts) == 1
    assert sum(s.startswith("INSERT INTO neo4j_outbox") for s in stmts) == 1
    assert sum(s.startswith("INSERT INTO project_promotions") for s in stmts) == 1


@pytest.mark.asyncio
async def test_the_ledger_row_carries_the_whole_transition():
    """The ledger is the ONLY durable evidence for a one-way write. A row that
    omits where the record came from, or on what basis, cannot answer the
    question it exists to answer — so the values are asserted, not just the
    statement's presence."""
    conn = FakeConn(current=SENTINEL)
    await promote_record(
        conn, 549, "shared-memory-GitHub",
        method=METHOD_GROUNDING, actor="claude", note="grounded by pg_id=1000",
    )
    ledger = [a for s, a in conn.executed
              if s.startswith("INSERT INTO project_promotions")]
    assert len(ledger) == 1
    pg_id, from_project, to_project, method, actor, note = ledger[0]
    assert pg_id == 549
    assert from_project == SENTINEL          # where it came FROM, not just where it went
    assert to_project == "shared-memory-GitHub"
    assert method == METHOD_GROUNDING        # on what basis
    assert actor == "claude"                 # who asked
    assert "1000" in note                    # and the evidence for it


@pytest.mark.asyncio
async def test_promotion_takes_the_row_lock_before_deciding():
    """The read that establishes 'currently parked' must be the one that locks
    the row, or two concurrent promotions both see parked and both write."""
    conn = FakeConn(current=None)
    await promote_record(conn, 1, "smg", method="test", actor="t")
    assert conn.locked is True


@pytest.mark.asyncio
async def test_the_graph_half_goes_through_the_outbox_never_direct():
    """Outbox atomicity — a partial run must leave durable work, not half a
    graph. The writer has no Neo4j handle at all, which is the strongest form
    of this guarantee."""
    conn = FakeConn(current=None)
    await promote_record(conn, 1, "smg", method="test", actor="t")
    outbox = [a for s, a in conn.executed if "neo4j_outbox" in s]
    assert len(outbox) == 1
    assert '"type": "project_of"' in outbox[0][1] or "project_of" in str(outbox[0][1])


@pytest.mark.asyncio
async def test_a_refused_promotion_writes_nothing_at_all():
    """A refusal is a result, not a partial write."""
    conn = FakeConn(current="project-b")
    result = await promote_record(conn, 1, "smg", method="test", actor="t")
    assert result["promoted"] is False
    assert conn.executed == []


@pytest.mark.asyncio
async def test_a_second_promotion_of_the_same_record_is_refused():
    """The one-way property, end to end: the first write makes the value real,
    and a real value is exactly what the guard refuses."""
    conn = FakeConn(current=None)
    first = await promote_record(conn, 1, "smg", method="test", actor="t")
    assert first["promoted"] is True
    second = await promote_record(conn, 1, "other", method="test", actor="t")
    assert second["promoted"] is False
    assert "smg" in second["reason"]


@pytest.mark.asyncio
async def test_a_missing_record_is_refused_not_created():
    """A promotion repairs; it never mints."""
    class Missing(FakeConn):
        async def fetchrow(self, sql, *args):
            return None

    conn = Missing(current=None)
    result = await promote_record(conn, 99999, "smg", method="test", actor="t")
    assert result["promoted"] is False
    assert conn.executed == []


# ── P19 — the graph half replaces, it does not accumulate ────────────────────
#
# Asserted against the Cypher the worker ACTUALLY runs, not against the source
# file: deleting the clause changes the executed string, so the test dies.

class _FakeSession:
    def __init__(self):
        self.queries = []

    async def run(self, query, **params):
        self.queries.append((" ".join(query.split()), params))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeNeo4j:
    def __init__(self, session):
        self._session = session

    def session(self):
        return self._session


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


def _worker(session, conn):
    from coordinator import MemoryCoordinator

    c = object.__new__(MemoryCoordinator)
    c._neo4j = _FakeNeo4j(session)
    c._acquire = lambda: _FakeAcquire(conn)
    return c


@pytest.mark.asyncio
async def test_project_of_row_deletes_the_stale_edge_before_merging():
    """A bare MERGE is correct only while every target has no edge — true of the
    backfill's population by construction, false of the promotion writer's."""
    session, conn = _FakeSession(), FakeConn(current=None)
    await _worker(session, conn)._apply_project_of_outbox_row(
        7, 549, {"type": "project_of", "project": "shared-memory-GitHub"}
    )
    cypher = session.queries[0][0]
    assert "DELETE" in cypher, "a MERGE-only writer accumulates project edges"
    assert cypher.index("DELETE") < cypher.index("MERGE"), \
        "the stale edge must go BEFORE the new one is merged"
    assert "PROJECT_OF" in cypher


@pytest.mark.asyncio
async def test_project_of_row_matches_the_whole_spine_not_just_facts():
    """Matching :Fact alone would silently drop every row targeting a judgement,
    and a dropped repair row looks exactly like a successful one."""
    session, conn = _FakeSession(), FakeConn(current=None)
    await _worker(session, conn)._apply_project_of_outbox_row(
        7, 549, {"type": "project_of", "project": "smg"}
    )
    cypher = session.queries[0][0]
    assert "Decision" in cypher and "Retrospective" in cypher


@pytest.mark.asyncio
async def test_project_of_row_never_mints_the_record():
    """MATCH the record, MERGE only the :Project. A repair that MERGEs its own
    target conjures a contentless node whose only property is a pg_id."""
    session, conn = _FakeSession(), FakeConn(current=None)
    await _worker(session, conn)._apply_project_of_outbox_row(
        7, 549, {"type": "project_of", "project": "smg"}
    )
    cypher = session.queries[0][0]
    assert cypher.startswith("MATCH (n:")


@pytest.mark.asyncio
async def test_a_project_of_row_with_no_project_touches_neo4j_not_at_all():
    """An empty project is a no-op row, not a licence to delete the edge the
    record already has."""
    session, conn = _FakeSession(), FakeConn(current=None)
    await _worker(session, conn)._apply_project_of_outbox_row(
        7, 549, {"type": "project_of", "project": ""}
    )
    assert session.queries == []


# ── The reconciler's predicate — repair a disagreement, never fill an absence ─

def _find_drift(*a, **kw):
    from reconcile_project_edges import find_drift
    return find_drift(*a, **kw)


def test_reconciler_repairs_a_wrong_edge():
    drift = _find_drift({7: "project-b"}, {7: ["shared-memory-GitHub"]}, set())
    assert drift == {7: (["shared-memory-GitHub"], "project-b")}


def test_reconciler_collapses_an_extra_edge():
    """A bare MERGE only ever adds, so a record can name two projects at once."""
    drift = _find_drift({7: "project-b"}, {7: ["project-b", "smg"]}, set())
    assert 7 in drift


def test_reconciler_leaves_an_agreeing_record_alone():
    assert _find_drift({7: "smg"}, {7: ["smg"]}, set()) == {}


def test_reconciler_never_fills_an_absence():
    """Repairing a disagreement is not the same act as filling a gap. On the
    live corpus 161 of 167 candidates were edgeless retrospectives, and whether
    those SHOULD carry the edge is still an open question — so a tool that
    treated absence as drift would have made a 161-edge graph change wearing the
    label of a repair."""
    assert _find_drift({7: "smg"}, {7: []}, set()) == {}


def test_reconciler_leaves_parked_records_to_the_promotion_path():
    """A parked record carrying an edge is the promotion writer's population;
    clearing it first would destroy the only surviving hint about where the
    record belongs before anything has been decided about it."""
    assert _find_drift({7: None}, {7: ["smg"]}, set()) == {}
    assert _find_drift({7: "general_discussion"}, {7: ["smg"]}, set()) == {}


def test_reconciler_skips_rows_already_queued():
    """Re-running before the worker drains must not enqueue the same repair."""
    assert _find_drift({7: "a"}, {7: ["b"]}, {7}) == {}


def test_reconciler_ignores_a_node_with_no_postgres_row():
    """A graph node whose record is gone has nothing to be reconciled against."""
    assert _find_drift({}, {7: ["smg"]}, set()) == {}
