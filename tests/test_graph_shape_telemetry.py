"""compliance.label_distribution and compliance.top_paths: the monitor's graph-shape view (decision:2768).

Failure modes, named before the tests (decision:2671):
  F1 label_distribution publishes only the labels outside the ontology, as invalid_labels does
  F2 the same label set arriving in two orders splits one path into two rows (head(labels()) grouping)
  F3 the full relationship scan runs on a telemetry request instead of in the background
  F4 a failed refresh blanks the rows, or leaves them looking fresh
  F5 the refresh runs more often than GRAPH_TOP_PATHS_REFRESH_S, or at all when it is 0
  F6 a failure earlier in the refresher loop (a dead Postgres) skips the top_paths refresh
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_telemetry_contract import (  # noqa: E402
    _NEO4J_PLAN, _FakeSession, _async_ctx, _stub_telemetry_coordinator, gateway,  # noqa: F401
)

PATH_QUERY = "MATCH (a)-[r]->(b) RETURN labels(a) AS la"


class _CountingSession(_FakeSession):
    def __init__(self, plan, fail: bool = False):
        super().__init__(plan)
        self.fail = fail
        self.path_runs = 0

    async def run(self, cypher, **params):
        if PATH_QUERY in " ".join(cypher.split()):
            self.path_runs += 1
            if self.fail:
                raise RuntimeError("neo4j unavailable")
        return await super().run(cypher, **params)


def _coordinator(g, session):
    c = _stub_telemetry_coordinator(g)
    c._neo4j.session = MagicMock(return_value=_async_ctx(session))
    return c


def test_the_same_label_set_in_two_orders_is_one_path():
    import coordinator as co
    rows = [
        {"la": ["Entity", "Concept"], "rel": "RELATES_TO", "lb": ["Entity"], "c": 3},
        {"la": ["Concept", "Entity"], "rel": "RELATES_TO", "lb": ["Entity"], "c": 4},
        {"la": ["Fact"], "rel": "MENTIONS", "lb": ["Entity"], "c": 5},
    ]
    assert co.merge_top_paths(rows, 15) == [
        {"from": "Concept:Entity", "rel": "RELATES_TO", "to": "Entity", "count": 7},
        {"from": "Fact", "rel": "MENTIONS", "to": "Entity", "count": 5},
    ]


def test_top_paths_keeps_the_most_frequent_and_orders_ties_by_name():
    import coordinator as co
    rows = [{"la": [f"L{i}"], "rel": "R", "lb": ["X"], "c": 1} for i in range(20)]
    rows.append({"la": ["Top"], "rel": "R", "lb": ["X"], "c": 9})
    out = co.merge_top_paths(rows, 15)
    assert len(out) == 15
    assert out[0] == {"from": "Top", "rel": "R", "to": "X", "count": 9}
    assert [r["from"] for r in out[1:3]] == ["L0", "L1"]


def test_label_distribution_is_the_whole_census_not_the_invalid_subset(gateway):
    session = _CountingSession(_NEO4J_PLAN)
    c = _coordinator(gateway, session)
    comp = asyncio.run(c._graph_compliance())
    # Fact is a known ontology label, so invalid_labels never carries it.
    assert comp["label_distribution"] == {"Fact": 900, "Decision": 200}


def test_a_telemetry_build_never_runs_the_relationship_scan(gateway):
    session = _CountingSession(_NEO4J_PLAN)
    c = _coordinator(gateway, session)
    snap = asyncio.run(c._build_telemetry())
    assert session.path_runs == 0
    assert "error" not in snap["compliance"]
    assert snap["compliance"]["top_paths"] == []
    assert snap["compliance"]["top_paths_as_of"] is None


def test_a_failed_refresh_keeps_the_last_rows_and_says_so(gateway):
    session = _CountingSession(_NEO4J_PLAN)
    c = _coordinator(gateway, session)
    asyncio.run(c._refresh_top_paths(0.0))
    good_rows, good_as_of = list(c._top_paths["rows"]), c._top_paths["as_of"]
    assert good_rows and good_as_of

    session.fail = True
    asyncio.run(c._refresh_top_paths(10_000.0))
    comp = asyncio.run(c._graph_compliance())
    assert comp["top_paths"] == good_rows
    assert comp["top_paths_as_of"] == good_as_of
    assert comp["top_paths_error"] == "neo4j unavailable"

    session.fail = False
    asyncio.run(c._refresh_top_paths(20_000.0))
    assert "top_paths_error" not in asyncio.run(c._graph_compliance())


def test_the_refresh_waits_its_interval(gateway, monkeypatch):
    import coordinator as co
    monkeypatch.setattr(co, "GRAPH_TOP_PATHS_REFRESH_S", 300.0)
    session = _CountingSession(_NEO4J_PLAN)
    c = _coordinator(gateway, session)
    asyncio.run(c._refresh_top_paths(1000.0))
    asyncio.run(c._refresh_top_paths(1299.0))
    assert session.path_runs == 1
    asyncio.run(c._refresh_top_paths(1300.0))
    assert session.path_runs == 2


def test_zero_turns_the_refresh_off(gateway, monkeypatch):
    import coordinator as co
    monkeypatch.setattr(co, "GRAPH_TOP_PATHS_REFRESH_S", 0.0)
    session = _CountingSession(_NEO4J_PLAN)
    c = _coordinator(gateway, session)
    asyncio.run(c._refresh_top_paths(1000.0))
    assert session.path_runs == 0
    assert c._top_paths["rows"] == []


def test_a_dead_postgres_does_not_skip_the_top_paths_refresh(gateway, monkeypatch):
    """One pass of the real refresher loop with every earlier step failing."""
    import coordinator as co
    c = _stub_telemetry_coordinator(gateway)
    c._compute_consolidation_health = AsyncMock(side_effect=RuntimeError("pg down"))
    c._probe_postgres = AsyncMock(side_effect=RuntimeError("pg down"))
    c._refresh_top_paths = AsyncMock()

    async def _stop(_s):
        raise asyncio.CancelledError

    monkeypatch.setattr(co.asyncio, "sleep", _stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(c._consolidation_health_refresher())
    c._refresh_top_paths.assert_awaited_once()
