"""Project identity — the surrogate key, and the gate that counts it (027).

Two invariants ship here, and each one has a test that dies if it is removed:

  P21  A `:Project` node is keyed on its REGISTRY IDENTITY when one exists; the
       name rides along as a display label. The name may be renamed, so it is
       not what anything hangs off.
  P22  The insight gate counts distinct IDENTITIES, and a project node with no
       identity counts as NONE. It fails closed: the cost is a fold that does
       not happen, against the alternative cost of a cross-project insight
       synthesised out of one project's decisions.

⚠ Every assertion here is on a VALUE — the Cypher a function returns, the SQL a
call actually issued, the buckets a classifier produced — never on the source
text of a module. A guard disabled with `if False and …` leaves its own text
intact, and that has passed a test in this repo twice.
"""
import os
import sys

import pytest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

from project_axis import project_merge_cypher  # noqa: E402
from project_promotion import promote_record, METHOD_GROUNDING  # noqa: E402


# ── P21 — the node is keyed on the identity, the name is a label ─────────────

def test_an_identified_project_is_keyed_on_the_identity():
    cypher = project_merge_cypher(7)
    assert "MERGE (p:Project {project_id: $project_id})" in cypher
    # And the name is still written — it is what a human queries by, and what
    # the client-side templates filter on.
    assert "SET p.name = $project" in cypher


def test_an_identified_project_is_never_keyed_on_its_name():
    """THE regression this exists to catch. Keying the MERGE on the name while
    SETting the id looks equivalent and is not: it makes the mutable label the
    thing the node is found by, so a rename still splits one project into two
    nodes and the gate can still count that project twice."""
    cypher = project_merge_cypher(7)
    assert "MERGE (p:Project {name:" not in cypher


def test_an_unidentified_project_still_gets_its_node():
    """The WRITE never fails closed — only the read does. A record with no
    project edge violates the axis outright, so a name we cannot resolve to an
    identity is written the pre-027 way rather than dropped."""
    cypher = project_merge_cypher(None)
    assert cypher == "MERGE (p:Project {name: $project})"
    assert "project_id" not in cypher


def test_the_merge_variable_is_caller_chosen():
    """Three call sites embed this in different surrounding clauses; a fixed
    variable name would collide with one of them eventually."""
    assert project_merge_cypher(7, var="canon").startswith("MERGE (canon:Project")
    assert project_merge_cypher(None, var="canon") == "MERGE (canon:Project {name: $project})"


# ── P22 — RETIRED (Dreaming Cycle Plan to v2, §2.2; C2) ──────────────────────
#
# P22 protected `insight_cluster_cypher`'s project-IDENTITY discrimination for
# the pre-v2 insight gate's ≥2-distinct-projects rule (registry `project_id`,
# never the mutable `name`, per migration 027). The v2 insight gate drops the
# ≥2-projects requirement outright (plan §2.2: "Deliberately NOT in the gate:
# ... a count of distinct projects") — a gating group is exactly one
# `(project, domain)` pair by construction (`nrem_gate.eligible_domain_level_
# clusters`), so there is no cross-project count left to discriminate on,
# correctly or otherwise. `insight_cluster_cypher` itself is deleted
# (`insight_gate.py` now holds the v2 walk/component/gate functions — see
# `tests/test_insight_gate.py`), so the six tests that pinned its Cypher text
# are removed with it rather than rewritten against nothing. The ruling this
# reverses is `decision:245`; a `refined` retrospective against it is owed by
# the merger (not built here — see the brief's explicit scope boundary).
#
# P21 (above) is UNCHANGED: `project_merge_cypher`'s identity-over-name
# keying is orthogonal to the insight gate and still holds.


# ── The ledger keeps the name AND gains the pointer ──────────────────────────

class _FakeConn:
    def __init__(self):
        self.executed = []

    async def fetchrow(self, *args):
        return {"project": None, "id": 549}

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Tx()


@pytest.mark.asyncio
async def test_the_ledger_row_carries_the_identity_as_well_as_the_name():
    """The two columns answer different questions and BOTH are needed: the text
    is the evidence (what the record was moved onto, on the day it moved, which
    a later rename must never rewrite), the id is the durable pointer (so the
    row still resolves to the right project after that rename)."""
    conn = _FakeConn()
    await promote_record(conn, 549, "shared-memory-GitHub",
                         method=METHOD_GROUNDING, actor="claude", note="n")
    ledger = [s for s, _ in conn.executed
              if s.startswith("INSERT INTO project_promotions")]
    assert len(ledger) == 1
    sql = ledger[0]
    assert "to_project" in sql and "to_project_id" in sql
    # Resolved INSIDE the statement: a second round trip could read a registry
    # that changed between the two, and the NOT NULL column would then be the
    # only thing standing between the ledger and a row pointing at nothing.
    assert "SELECT id FROM projects WHERE name" in sql


# ── The reconcile tool ───────────────────────────────────────────────────────

def _classify(registry, nodes):
    import importlib
    return importlib.import_module("reconcile_project_identity").classify(registry, nodes)


def test_reconcile_splits_the_graph_against_the_registry():
    buckets = _classify(
        {"alpha-service": 1, "beta-tool": 2},
        [
            {"name": "alpha-service", "project_id": 1},    # already identified
            {"name": "beta-tool", "project_id": None},     # the population
            {"name": "gamma-app", "project_id": None},     # not in the registry
        ],
    )
    assert buckets["correct"] == ["alpha-service"]
    assert buckets["to_stamp"] == [("beta-tool", 2)]
    assert buckets["unregistered"] == ["gamma-app"]
    assert buckets["conflicting"] == []


def test_reconcile_reports_an_id_that_disagrees_with_the_registry():
    """Distinct from 'unidentified' on purpose: an id that is merely absent is
    an incomplete upgrade, while an id that CONTRADICTS the registry means the
    node was stamped against a row that has since moved — same repair, very
    different thing to read."""
    buckets = _classify({"alpha-service": 1},
                        [{"name": "alpha-service", "project_id": 99}])
    assert buckets["conflicting"] == [("alpha-service", 99, 1)]
    assert buckets["to_stamp"] == []


def test_reconcile_never_creates_a_project_node():
    """A registry name with no node is a project with no records yet. MERGE here
    would put an empty project into the graph — and into the gate's project set
    — for every registered name nobody has written to."""
    import importlib
    stamp = importlib.import_module("reconcile_project_identity").STAMP_CYPHER
    assert "MATCH (p:Project {name: row.name})" in stamp
    assert "MERGE" not in stamp


# ── The upgrade-completeness gauge ───────────────────────────────────────────

def _coordinator_with(nodes, registry):
    from coordinator import MemoryCoordinator
    coord = MemoryCoordinator()

    async def run(query, **params):
        result = MagicMock()
        result.data = AsyncMock(return_value=nodes)
        return result

    session = MagicMock()
    session.run = run
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    coord._neo4j = MagicMock()
    coord._neo4j.session = MagicMock(return_value=session)

    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{"name": n, "id": i}
                                         for n, i in registry.items()])
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    coord._acquire = MagicMock(return_value=acq)
    return coord


@pytest.mark.asyncio
async def test_health_reports_the_upgrade_as_incomplete_while_a_node_lacks_an_id():
    coord = _coordinator_with(
        [{"name": "alpha-service", "project_id": None},
         {"name": "beta-tool", "project_id": 2}],
        {"alpha-service": 1, "beta-tool": 2},
    )
    out = await coord._project_identity_health()
    assert out == {"nodes": 2, "unidentified": 1, "mismatched": 0,
                   "unregistered": 0, "complete": False}


@pytest.mark.asyncio
async def test_health_reports_complete_when_every_registered_node_is_identified():
    coord = _coordinator_with(
        [{"name": "alpha-service", "project_id": 1}],
        {"alpha-service": 1},
    )
    out = await coord._project_identity_health()
    assert out["complete"] is True
    assert out["unidentified"] == 0


@pytest.mark.asyncio
async def test_an_unregistered_node_is_reported_without_blocking_completeness():
    """It cannot be fixed by a tool — deciding what an unregistered project node
    means is an operator's judgement about their own corpus — so it must not
    make the upgrade look permanently unfinished. It still cannot take part in a
    fold, which is why it is counted rather than ignored."""
    coord = _coordinator_with(
        [{"name": "alpha-service", "project_id": 1},
         {"name": "gamma-app", "project_id": None}],
        {"alpha-service": 1},
    )
    out = await coord._project_identity_health()
    assert out["unregistered"] == 1
    assert out["unidentified"] == 0
    assert out["complete"] is True
