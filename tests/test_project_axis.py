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
