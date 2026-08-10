"""v2 FACT GATE invariants (Dreaming Cycle Plan to v2, §2.1, §2.6; task C1/C1b).

Mutation-checked coverage for I1, I2, I8, and (C1b) the gauge/fold coupling
CLAUDE.md's Group 3 rule names — each test is written against the REAL code
path (the executed Cypher text captured from `_find_grounded_fact_groups`,
or the real `eligible_domain_level_clusters` partitioner), never a
paraphrase. Every mutation performed to verify these tests actually die is
recorded in HANDOFF.md at the worktree root, alongside which test died and
how it was restored.

No DB, no Neo4j, no LLM — the Cypher text is captured via a fake driver
session, exactly as test_nrem_confidence.py already does.
"""
import inspect
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import consolidation_loop as cl  # noqa: E402
import coordinator as co  # noqa: E402
from consolidation_loop import (  # noqa: E402
    ConsolidationDaemon,
    eligible_domain_level_clusters,
)


# ── Fake Neo4j driver — captures the Cypher text, no I/O ──────────────────────

class _AsyncCtx:
    def __init__(self, val):
        self._val = val

    async def __aenter__(self):
        return self._val

    async def __aexit__(self, *_):
        pass


class _FakeResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    async def data(self):
        return self._rows


class _FakeSession:
    """Captures every (query, params) run against the fake Neo4j driver."""
    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])

    async def run(self, query, **params):
        self.calls.append((" ".join(query.split()), params))
        return self._results.pop(0) if self._results else _FakeResult()


def _daemon_with_fake_graph(results=None):
    from unittest.mock import MagicMock
    daemon = ConsolidationDaemon()
    session = _FakeSession(results)
    daemon.driver = MagicMock()
    daemon.driver.session = MagicMock(return_value=_AsyncCtx(session))
    return daemon, session


async def _captured_discovery_query():
    """Runs `_find_grounded_fact_groups` against a fake driver and returns the
    single Cypher string it executed."""
    daemon, session = _daemon_with_fake_graph([_FakeResult([])])
    await daemon._find_grounded_fact_groups()
    assert len(session.calls) == 1, (
        "the v2 fact gate discovery step is expected to run exactly ONE "
        "Cypher query — a second call means the gate started reading a "
        "second source of truth"
    )
    query, _params = session.calls[0]
    return query


# ── I1 — No gate predicate reads an entity name. ───────────────────────────────

@pytest.mark.asyncio
async def test_i1_discovery_query_never_touches_entity_or_mentions():
    """MUTATION-CHECKED (see HANDOFF.md): a temporary
    `MATCH (f)-[:MENTIONS]->(e:Entity)` clause added to
    `_find_grounded_fact_groups`'s Cypher made this test fail (both
    substrings appeared), confirming it actually bites. Reverted after."""
    query = await _captured_discovery_query()
    assert f":{cl.ONT.entity}" not in query
    assert f":{cl.ONT.entity_link}" not in query
    assert f":{cl.ONT.entity_link_alias}" not in query
    assert "alias_component" not in query          # ADR-017 entity-clustering artefact


def test_i1_partitioner_signature_carries_no_entity_parameter():
    """I1 restated at the partitioner: `eligible_domain_level_clusters` — the
    SOLE partitioner the v2 fold calls — has no entity-shaped parameter at
    all, so there is no argument an entity name could even be threaded
    through. Signature-level, not behavioural — this is the twin of the
    Cypher check above, covering the OTHER half of the gate."""
    import inspect
    params = list(inspect.signature(eligible_domain_level_clusters).parameters)
    assert params == [
        "contents", "pg_ids", "project_map", "domains_map",
        "threshold", "registered_sections",
    ]
    assert not any("entity" in p for p in params)


# ── I2 — No gate predicate reads a count of projects. ──────────────────────────

@pytest.mark.asyncio
async def test_i2_discovery_query_never_counts_projects():
    """MUTATION-CHECKED (see HANDOFF.md): temporarily appending
    `, count(DISTINCT proj) AS project_count` to the RETURN clause made this
    test fail. Reverted after. This is I2 restated precisely: the v2 fact
    gate anchors on (project, domain) IDENTITY, never on a project COUNT —
    that rule belongs to the (separate, C2-owned) insight gate's ≥2-distinct-
    projects rule, and this test is what stops it leaking into the fact gate."""
    query = await _captured_discovery_query()
    assert "project_ids" not in query
    # No aggregate function is ever applied to a project-typed variable.
    assert not re.search(r"(count|collect)\(\s*DISTINCT\s+proj", query, re.IGNORECASE)


def test_i2_partitioner_never_counts_distinct_projects():
    """Same invariant, the partitioner half: `eligible_domain_level_clusters`
    groups records by the (project, section) key itself — it has no branch
    that counts how many distinct projects a candidate set spans."""
    import inspect
    source = inspect.getsource(eligible_domain_level_clusters)
    assert not re.search(r"(count|len)\(.*project", source, re.IGNORECASE)
    assert "project_ids" not in source


# ── I8 — keyed on (project, domain), both present and registered; never a
#        project alone, never an entity. ───────────────────────────────────────

@pytest.mark.asyncio
async def test_i8_discovery_query_requires_the_domain_of_project_of_chain():
    """The discovery Cypher's own MATCH clauses are the registration proof:
    a fact reaches `project`/`domain` in the RETURN only by walking
    DOMAIN_OF then PROJECT_OF — a fact with a project but no registered
    domain simply never produces a row (no DOMAIN_OF edge to walk), so
    "project alone" cannot appear in the output at all."""
    query = await _captured_discovery_query()
    assert f"-[:{cl.ONT.domain_of}]->" in query
    assert f"-[:{cl.ONT.project_of}]->" in query
    assert f"(dom:{cl.ONT.domain})" in query
    assert f"(proj:{cl.ONT.project})" in query
    assert f"-[:{cl.ONT.grounded_in}]->" in query   # membership = GROUNDED_IN, per §0/§2.1


def test_i8_project_alone_never_forms_a_group():
    """MUTATION-CHECKED (see HANDOFF.md): inverting the
    `if (project, section) not in registered: continue` guard in
    `eligible_domain_level_clusters` (so it read `if ... in registered:
    continue`, admitting the OPPOSITE set) made this test fail — a project
    with no registered domain then formed a group. Reverted after.

    Three facts share a project and NO section at all — under the pre-v2
    entity-level rule (P15) this would fold as one project-only bucket; the
    v2 gate must produce nothing."""
    contents = ["a", "b", "c"]
    pg_ids = [1, 2, 3]
    project_map = {1: "smg", 2: "smg", 3: "smg"}
    domains_map = {1: [], 2: [], 3: []}
    result = eligible_domain_level_clusters(
        contents, pg_ids, project_map, domains_map,
        threshold=2, registered_sections={("smg", "architecture")})
    assert result == []


def test_i8_project_present_but_domain_unregistered_never_forms_a_group():
    """Both axes present is not enough — the domain must be REGISTERED."""
    contents = ["a", "b", "c"]
    pg_ids = [1, 2, 3]
    project_map = {i: "smg" for i in pg_ids}
    domains_map = {i: ["not-in-the-registry"] for i in pg_ids}
    result = eligible_domain_level_clusters(
        contents, pg_ids, project_map, domains_map,
        threshold=2, registered_sections={("smg", "architecture")})
    assert result == []


def test_i8_key_is_the_project_domain_tuple_not_project_alone():
    """Two projects share a section NAME ("architecture") but are distinct
    (project, domain) pairs — I8 requires the key to be the TUPLE, so they
    must never merge into one bucket keyed on the section name alone."""
    contents = ["a", "b", "c", "d"]
    pg_ids = [1, 2, 3, 4]
    project_map = {1: "smg", 2: "smg", 3: "other", 4: "other"}
    domains_map = {i: ["architecture"] for i in pg_ids}
    registered = {("smg", "architecture"), ("other", "architecture")}
    result = eligible_domain_level_clusters(
        contents, pg_ids, project_map, domains_map,
        threshold=2, registered_sections=registered)
    keys = {k for k, _c, _p in result}
    assert keys == {("smg", "architecture"), ("other", "architecture")}
    by_key = {k: p for k, _c, p in result}
    assert by_key[("smg", "architecture")] == [1, 2]
    assert by_key[("other", "architecture")] == [3, 4]


# ── C1b — the telemetry gauge and the fold must share ONE partitioner ─────────
# CLAUDE.md's Group 3 rule, stated exactly for this case: "when a gate
# changes, a metric's meaning can invert while its name stays — rename it."
# The escalation this closes: coordinator._nrem_cycle_counts used to run a
# SEPARATE entity-hub Cypher + count_entity_level_cycles/count_domain_level_cycles
# split that the fold could no longer produce after C1 — a counter that
# survived its partitioner. The fix is not just "make the numbers agree
# today" (that could still drift tomorrow) — it is "make disagreement
# structurally impossible" by sharing one function.

def test_nrem_cycle_counts_reuses_the_folds_own_partitioner():
    """MUTATION-CHECKED (see HANDOFF.md): temporarily replacing the
    `from consolidation_loop import count_domain_level_cycles` import (and its
    call) with an inline reimplementation made this test fail. Reverted after.

    Source-level proof that `_nrem_cycle_counts` cannot silently diverge from
    the fold: it must import and call `count_domain_level_cycles` — the exact
    count-only twin of `eligible_domain_level_clusters`, the SAME partitioner
    `_consolidate_clusters` calls — never a second, hand-rolled threshold
    check that could quietly stop matching it."""
    source = inspect.getsource(co.MemoryCoordinator._nrem_cycle_counts)
    assert "from consolidation_loop import count_domain_level_cycles" in source
    assert "count_domain_level_cycles(" in source
    # No entity-hub language left in the fact-cycle half of this method at all
    # — that gate is gone, not just unused (the insight half below is a
    # DIFFERENT, still-live mechanism and legitimately keeps its own Cypher).
    assert f":{cl.ONT.entity}" not in source
    assert f":{cl.ONT.entity_link}" not in source
    assert "alias_component" not in source


async def _run_nrem_cycle_counts_capturing_queries(fact_rows=None, insight_cycles=0):
    """Runs the REAL `_nrem_cycle_counts` against a fake Neo4j session (no DB)
    and returns (counts, [captured query strings]) — mirrors the fake_run
    dispatch pattern already established in test_project_axis.py."""
    from unittest.mock import AsyncMock, MagicMock

    captured = []

    async def fake_run(query, **params):
        captured.append(" ".join(query.split()))
        result = MagicMock()
        if "count(*) AS cycles" in query:
            result.data = AsyncMock(return_value=[{"cycles": insight_cycles}])
        else:  # the fact discovery query
            result.data = AsyncMock(return_value=fact_rows or [])
        return result

    session = MagicMock()
    session.run = fake_run
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    coord = co.MemoryCoordinator()
    coord._neo4j = MagicMock()
    coord._neo4j.session = MagicMock(return_value=session)

    counts = await coord._nrem_cycle_counts()
    return counts, captured


@pytest.mark.asyncio
async def test_nrem_cycle_counts_walks_the_same_grounded_in_domain_of_project_of_chain():
    """The ACTUAL executed Cypher `_nrem_cycle_counts` runs to source
    project/domain is the SAME chain `_find_grounded_fact_groups` folds on —
    not a Postgres PROJECT_SQL resolution that could disagree with the graph."""
    _counts, queries = await _run_nrem_cycle_counts_capturing_queries()
    fact_query = next(q for q in queries if "count(*) AS cycles" not in q)
    assert f"-[:{cl.ONT.grounded_in}]->" in fact_query
    assert f"-[:{cl.ONT.domain_of}]->" in fact_query
    assert f"-[:{cl.ONT.project_of}]->" in fact_query
    assert "proj.name AS project" in fact_query
    assert "dom.name AS domain" in fact_query
    assert f":{cl.ONT.entity}" not in fact_query
    assert f":{cl.ONT.entity_link}" not in fact_query


@pytest.mark.asyncio
async def test_nrem_cycle_counts_reports_fact_cycles_matching_the_real_partitioner():
    """MUTATION-CHECKED (see HANDOFF.md): the composition proof — feed the
    fake graph session two grounded facts in one registered (project, domain)
    group (threshold 3, per ONT.density_threshold in this test env) and one
    in another, and check the reported `fact_cycles` against what
    `eligible_domain_level_clusters` independently computes on the SAME row
    shape. If `_nrem_cycle_counts` ever stops calling that shared partitioner,
    this is the test that would catch the two counts silently diverging."""
    rows = [
        {"pg_id": 1, "project": "smg", "domain": "architecture"},
        {"pg_id": 2, "project": "smg", "domain": "architecture"},
        {"pg_id": 3, "project": "smg", "domain": "architecture"},
        {"pg_id": 4, "project": "smg", "domain": "operations"},
    ]
    counts, _queries = await _run_nrem_cycle_counts_capturing_queries(fact_rows=rows)

    project_map = {r["pg_id"]: r["project"] for r in rows}
    domains_map = {r["pg_id"]: [r["domain"]] for r in rows}
    registered = {(r["project"], r["domain"]) for r in rows}
    expected = len(eligible_domain_level_clusters(
        [""] * len(rows), [r["pg_id"] for r in rows],
        project_map, domains_map, cl.DENSITY_THRESHOLD, registered))

    assert counts["fact_cycles"] == expected == 1   # architecture (3) gates; operations (1) does not
    assert counts["fact_threshold"] == cl.DENSITY_THRESHOLD


def test_the_four_legacy_names_and_the_dead_wrapper_are_gone():
    """C1b closes the escalation C1 raised: once `_nrem_cycle_counts` no
    longer needs the pre-v2 two-level split, the names that existed only to
    feed it come out too — `NREM_DOMAIN_THRESHOLD` (a second, unread density
    knob), `eligible_entity_level_clusters` / `count_entity_level_cycles`
    (the entity-hub level), `eligible_domain_clusters` (its project-only
    wrapper), and `coordinator._count_domain_cycles` (already dead — no
    caller besides its own test, which is deleted with it)."""
    assert not hasattr(cl, "NREM_DOMAIN_THRESHOLD")
    assert not hasattr(cl, "eligible_entity_level_clusters")
    assert not hasattr(cl, "count_entity_level_cycles")
    assert not hasattr(cl, "eligible_domain_clusters")
    assert not hasattr(co, "_count_domain_cycles")


@pytest.mark.asyncio
async def test_nrem_cycle_counts_returned_dict_has_exactly_the_new_shape():
    """The wire contract of `GET /memory/telemetry`'s `nrem` key, pinned by
    return value rather than source text."""
    counts, _queries = await _run_nrem_cycle_counts_capturing_queries()
    assert set(counts) == {
        "fact_cycles", "decision_cycles", "total_cycles",
        "fact_threshold", "decision_threshold",
    }
