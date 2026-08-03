"""Invariant P1 — one project resolution, used by every reader.

Before this, the same COALESCE existed in eight places across five files: three
already canonical, two falling back to `domain`, one also to `scope`. The same
record answered "which project?" differently depending on who asked, and 219 of
261 decisions read as untagged while carrying a project all along.

Also covers the replacement of the `decision_cycles` gauge: it partitioned every
eligible Decision node by Postgres project — no shared grounded entity, no
≥2-distinct-projects rule, no HAD_OUTCOME — and so reported a backlog the daemon
could not fold. It now runs the daemon's own predicate, count-only.

No DB or Neo4j required.
"""
import os
import re
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

from project_axis import PROJECT_SQL, PROJECT_MATCH_SQL, SENTINEL, resolve_project
from insight_gate import insight_cluster_cypher


# ── The resolution itself ────────────────────────────────────────────────────

def test_resolution_prefers_the_decision_blob():
    """A judgement carries its project inside metadata.decision; a fact carries
    it at the top level. The decision branch must come FIRST and must exist —
    dropping it is what made 219 decisions read as untagged."""
    assert "metadata->'decision'->>'project'" in PROJECT_SQL
    assert PROJECT_SQL.index("decision") < PROJECT_SQL.index("metadata->>'project'")
    assert resolve_project({"decision": {"project": "smg"}, "project": "other"}) == "smg"
    assert resolve_project({"project": "smg"}) == "smg"


def test_neither_scope_nor_domain_is_in_the_chain():
    """A domain is a SECTION of a project and cannot stand in for the whole;
    `scope` is access control and is never topical."""
    assert "domain" not in PROJECT_SQL
    assert "scope" not in PROJECT_SQL
    assert resolve_project({"domain": "crypto"}) is None
    assert resolve_project({"scope": "team-a"}) is None
    assert resolve_project({"domain": "crypto", "scope": "team-a"}) is None


def test_unresolvable_is_none_not_a_bucket():
    """None, so a caller can tell "no project" from any particular name. PR 2
    turns that into fold-ineligibility rather than a shared bucket."""
    assert resolve_project({}) is None
    assert resolve_project(None) is None
    assert resolve_project("not-a-dict") is None
    assert resolve_project({"project": ""}) is None
    assert resolve_project({"project": "   "}) is None
    assert resolve_project({"project": 42}) is None
    assert resolve_project({"decision": "not-a-dict", "project": "smg"}) == "smg"


def test_sentinel_is_reserved_and_distinct():
    assert SENTINEL == "general_discussion"
    assert resolve_project({"project": SENTINEL}) == SENTINEL


# ── P1 has teeth only if no reader keeps a private copy ──────────────────────

_OWN_COPY = re.compile(r"COALESCE\(\s*metadata->[^)]*'project'", re.IGNORECASE)

# Python implicit string concatenation splits a query across source lines, so
# collapse `" ... "  f" ... "` before looking for the pattern.
_JOIN = re.compile(r'"\s*f?"')


def _scripts():
    for name in sorted(os.listdir(_SCRIPTS)):
        if name.endswith(".py") and name != "project_axis.py":
            yield name, open(os.path.join(_SCRIPTS, name), encoding="utf-8").read()


def test_no_reader_carries_its_own_project_resolution():
    """The regression guard for P1: a ninth copy appearing anywhere fails here.

    project_axis.py is the sole exemption — it is where the resolution lives.
    """
    offenders = [
        name for name, src in _scripts()
        if _OWN_COPY.search(_JOIN.sub("", src))
    ]
    assert offenders == [], (
        "these modules hand-roll a project resolution instead of importing "
        f"project_axis.PROJECT_SQL: {offenders}"
    )


def test_every_reader_actually_imports_the_module():
    """The mirror of the above: absence of a copy could also mean the reader
    stopped resolving a project at all."""
    expected = {
        "coordinator.py", "consolidation_loop.py", "rem_loop.py",
        "entity_resolution_eval.py", "migrate_retro_edges.py",
        "normalize_projects.py",
    }
    importers = {name for name, src in _scripts() if "from project_axis import" in src}
    assert expected <= importers, f"no longer importing the axis: {expected - importers}"


def test_the_migration_tool_matches_both_fields_not_the_resolution():
    """normalize_projects rewrites legacy spellings, so it must find a row whose
    OLD name is shadowed by a newer top-level one. COALESCE would skip it."""
    predicate = PROJECT_MATCH_SQL.format(p="%s")
    assert "metadata->>'project' = %s" in predicate
    assert "metadata->'decision'->>'project' = %s" in predicate
    assert "COALESCE" not in predicate
    assert " OR " in predicate


# ── decision_cycles now runs the daemon's own gate ───────────────────────────

def test_count_only_differs_from_the_fold_query_by_projection_alone():
    """One predicate, two projections. If they can drift, telemetry can once
    again report a backlog the daemon cannot fold."""
    full = insight_cluster_cypher()
    counting = insight_cluster_cypher(count_only=True)
    tail = " RETURN count(*) AS cycles"
    assert counting.endswith(tail)
    shared = counting[: -len(tail)]
    assert full.startswith(shared)


def test_the_gate_carries_all_three_conditions():
    """The conditions a Postgres partition could not express — and the reason
    the old gauge said 2 where the daemon folds 0."""
    q = insight_cluster_cypher(count_only=True)
    assert "size(projects) >= 2" in q
    assert "HAD_OUTCOME" in q
    assert "$hub_cap" in q
    assert "$threshold" in q


@pytest.mark.asyncio
async def test_decision_cycles_reflects_the_insight_gate_not_a_partition():
    """Eligible decisions that share no entity yield 0 cycles.

    The old chain collected them flat and bucketed by project, so two decisions
    in one project counted as a cycle even with nothing in common. The stub
    graph here returns no insight cluster — which is what the real gate returns
    for decisions sharing no grounded entity — so the gauge must read 0.
    """
    from coordinator import MemoryCoordinator

    coord = MemoryCoordinator()

    captured = []

    async def fake_run(query, **params):
        captured.append(query)
        result = MagicMock()
        if "count(*) AS cycles" in query:
            result.data = AsyncMock(return_value=[{"cycles": 0}])
        else:  # the fact-cluster query
            result.data = AsyncMock(return_value=[])
        return result

    session = MagicMock()
    session.run = fake_run
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    coord._neo4j = MagicMock()
    coord._neo4j.session = MagicMock(return_value=session)

    counts = await coord._nrem_cycle_counts()

    assert counts["decision_cycles"] == 0
    assert counts["total_cycles"] == 0
    # It must have ASKED the graph the insight question — a partition of
    # Postgres project values would never issue this query.
    assert any("count(*) AS cycles" in q for q in captured)
    assert any("size(projects) >= 2" in q for q in captured)


@pytest.mark.asyncio
async def test_decision_cycles_reports_what_the_gate_returns():
    """The counterpart: when the gate does find clusters, the gauge is theirs."""
    from coordinator import MemoryCoordinator

    coord = MemoryCoordinator()

    async def fake_run(query, **params):
        result = MagicMock()
        if "count(*) AS cycles" in query:
            result.data = AsyncMock(return_value=[{"cycles": 3}])
        else:
            result.data = AsyncMock(return_value=[])
        return result

    session = MagicMock()
    session.run = fake_run
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    coord._neo4j = MagicMock()
    coord._neo4j.session = MagicMock(return_value=session)

    counts = await coord._nrem_cycle_counts()
    assert counts["decision_cycles"] == 3


def test_reported_decision_threshold_tracks_the_real_gate():
    """A deployment tuning insight_threshold must see the tuned value, not a
    hardcoded twin that used to sit beside it in coordinator.py."""
    import coordinator
    from insight_gate import INSIGHT_THRESHOLD

    assert coordinator.INSIGHT_THRESHOLD is INSIGHT_THRESHOLD
    assert not hasattr(coordinator, "NREM_DECISION_THRESHOLD")


# ── P2/P3 — an unresolvable project folds nothing (v0.8.32) ──────────────────

def test_fold_eligible_rejects_every_shape_of_absence():
    from project_axis import fold_eligible

    assert fold_eligible("shared-memory-GitHub") is True
    assert fold_eligible(None) is False
    assert fold_eligible("") is False
    assert fold_eligible("   ") is False
    assert fold_eligible(0) is False
    assert fold_eligible(["smg"]) is False


def test_both_partitioners_use_the_same_eligibility_predicate():
    """The gauge and the fold agreeing is not a coincidence to be maintained by
    hand — they call one function."""
    import consolidation_loop
    import coordinator

    assert consolidation_loop.fold_eligible is coordinator.fold_eligible


def test_the_fold_key_query_no_longer_invents_a_bucket():
    """The SQL must let NULL through so the partitioner can exclude it. A
    COALESCE here would rebuild the `general` bucket underneath the guard,
    and every unit test above would still pass."""
    src = open(os.path.join(_SCRIPTS, "consolidation_loop.py"), encoding="utf-8").read()
    assert "f\"SELECT id, {PROJECT_SQL},\"" in src
    assert "COALESCE({PROJECT_SQL}, 'general')" not in src

    coord = open(os.path.join(_SCRIPTS, "coordinator.py"), encoding="utf-8").read()
    assert "f\"SELECT id, {PROJECT_SQL} AS domain\"" in coord
    assert "DEFAULT_DOMAIN" not in coord.split("# NREM dream-cycle backlog gauge")[0]


def test_project_node_is_minted_from_the_resolved_project():
    """P3 — never from a section, never from a chain."""
    coord = open(os.path.join(_SCRIPTS, "coordinator.py"), encoding="utf-8").read()
    assert '"project": project_for_graph(metadata),' in coord
    assert '"project": metadata.get("project"),' not in coord
    assert '"project": resolve_project(metadata),' not in coord


# ── The PROJECT_OF backfill row (v0.8.32) ────────────────────────────────────

class _Recorder:
    """Captures the Cypher a handler runs, plus the SQL it issues."""

    def __init__(self):
        self.cypher = []
        self.sql = []

    def coordinator(self):
        from coordinator import MemoryCoordinator
        c = MemoryCoordinator()

        async def run(query, **params):
            self.cypher.append((query, params))
            r = MagicMock()
            r.data = AsyncMock(return_value=[])
            return r

        session = MagicMock()
        session.run = run
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        c._neo4j = MagicMock()
        c._neo4j.session = MagicMock(return_value=session)

        conn = MagicMock()
        conn.execute = AsyncMock(side_effect=lambda *a: self.sql.append(a))
        acq = MagicMock()
        acq.__aenter__ = AsyncMock(return_value=conn)
        acq.__aexit__ = AsyncMock(return_value=False)
        c._acquire = MagicMock(return_value=acq)
        return c


@pytest.mark.asyncio
async def test_backfill_row_writes_only_the_project_edge():
    """THE safety property. Re-enqueuing an ordinary fact row would re-run its
    `UNWIND $entities MERGE MENTIONS`, resurrecting every enrichment edge a
    later sweep deliberately deleted. The repair row must touch nothing else."""
    rec = _Recorder()
    coord = rec.coordinator()
    await coord._apply_project_of_outbox_row(7, 42, {"type": "project_of",
                                                     "project": "smg"})
    assert len(rec.cypher) == 1
    query, params = rec.cypher[0]
    assert "PROJECT_OF" in query
    assert "MENTIONS" not in query
    assert "$entities" not in query
    assert "f.content" not in query
    assert params == {"pg_id": 42, "project": "smg"}


@pytest.mark.asyncio
async def test_backfill_matches_the_record_and_never_mints_one():
    """A backfill mints no records. MERGE on the target would conjure a phantom
    node whose only property is a pg_id — the exact defect the supersede
    handler documents.

    Asserted as MATCH-not-MERGE on the target rather than as an exact opening
    string: the row now matches the whole SPINE (a promotion can target a
    judgement, and matching :Fact alone would silently drop those rows) and
    deletes the stale edge first (P19). Pinning the literal prefix made this
    test fail for a change that strengthened the very property it protects.
    """
    rec = _Recorder()
    coord = rec.coordinator()
    await coord._apply_project_of_outbox_row(7, 42, {"type": "project_of",
                                                     "project": "smg"})
    query = rec.cypher[0][0]
    assert query.startswith("MATCH ("), "the target is matched, never merged"
    # The ONLY node this row may create is the :Project it points at.
    merged_labels = re.findall(r"MERGE \(\w+:(\w+)", query)
    assert merged_labels == ["Project"], merged_labels


@pytest.mark.asyncio
async def test_backfill_row_is_deleted_not_left_as_backlog():
    """One-shot, like the supersede row: it carries no dream lifecycle, so
    leaving it applied would inflate the outbox working set forever."""
    rec = _Recorder()
    coord = rec.coordinator()
    await coord._apply_project_of_outbox_row(7, 42, {"type": "project_of",
                                                     "project": "smg"})
    assert rec.sql == [("DELETE FROM neo4j_outbox WHERE id=$1", 7)]


@pytest.mark.asyncio
async def test_backfill_row_with_no_project_writes_nothing():
    """P3 — never mint a :Project from an absent value."""
    rec = _Recorder()
    coord = rec.coordinator()
    await coord._apply_project_of_outbox_row(7, 42, {"type": "project_of",
                                                     "project": "  "})
    assert rec.cypher == []
    assert rec.sql == [("DELETE FROM neo4j_outbox WHERE id=$1", 7)]


def test_backfill_never_writes_neo4j_directly():
    """The script's whole contract: it enqueues, the worker applies."""
    src = open(os.path.join(_SCRIPTS, "backfill_project_of.py"), encoding="utf-8").read()
    assert "INSERT INTO neo4j_outbox" in src
    assert "'pending'" in src          # the status the worker actually polls
    assert "MERGE" not in src          # no Cypher writes of its own


def test_backfill_refuses_against_a_gateway_that_cannot_handle_the_row():
    """The ordering hazard, made unarmable — and asserted on BEHAVIOUR, not on
    source text: a guard whose condition is disabled leaves its own message in
    the file, so a text assertion passes while the guard is dead.

    A worker predating this row type treats it as an ordinary fact row and runs
    `SET f.content = $content` with content the row does not carry, blanking the
    content of every fact it touches. Silent graph-side data loss.
    """
    import importlib
    bf = importlib.import_module("backfill_project_of")

    assert bf.gateway_handles_project_of((0, 8, 32)) is True
    assert bf.gateway_handles_project_of((0, 9, 0)) is True
    assert bf.gateway_handles_project_of((1, 0, 0)) is True
    assert bf.gateway_handles_project_of((0, 8, 31)) is False
    assert bf.gateway_handles_project_of((0, 7, 9)) is False


def test_backfill_fails_closed_when_the_version_is_unknowable():
    """'Cannot tell' is not 'safe to write'."""
    import importlib
    bf = importlib.import_module("backfill_project_of")

    assert bf.gateway_handles_project_of(None) is False

    old = bf.GATEWAY_URL
    try:
        bf.GATEWAY_URL = "http://127.0.0.1:1"   # nothing listens here
        raw, parsed = bf.gateway_version()
        assert parsed is None
        assert bf.gateway_handles_project_of(parsed) is False
    finally:
        bf.GATEWAY_URL = old


def test_backfill_consults_the_guard_before_inserting():
    """Placement matters as much as the predicate: the check must gate the
    INSERT, not merely exist somewhere in the file."""
    src = open(os.path.join(_SCRIPTS, "backfill_project_of.py"), encoding="utf-8").read()
    apply_path = src.split("Dry run — nothing enqueued")[1]
    assert apply_path.index("gateway_handles_project_of") < apply_path.index(
        "INSERT INTO neo4j_outbox"
    )
