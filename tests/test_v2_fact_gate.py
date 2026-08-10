"""v2 FACT GATE invariants (Dreaming Cycle Plan to v2, §2.1, §2.6; task C1).

Mutation-checked coverage for I1, I2, I8 — each test is written against the
REAL code path (the executed Cypher text captured from
`_find_grounded_fact_groups`, or the real `eligible_domain_level_clusters`
partitioner), never a paraphrase. Every mutation performed to verify these
tests actually die is recorded in HANDOFF.md at the worktree root, alongside
which test died and how it was restored.

No DB, no Neo4j, no LLM — the Cypher text is captured via a fake driver
session, exactly as test_nrem_confidence.py already does.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import consolidation_loop as cl  # noqa: E402
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
