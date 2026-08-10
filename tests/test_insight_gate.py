"""The v2 INSIGHT GATE — walk, components, ordering, identity resolution
(Dreaming Cycle Plan to v2, §2.2-§2.5; C2).

Unit-tests insight_gate.py in isolation (pure functions + the async walk
driver against a FAKE neighbour function — no real Neo4j). The end-to-end
wiring through `consolidation_loop._find_fresh_insight_clusters` and
`coordinator._nrem_cycle_counts` is covered in tests/test_insight_consolidation.py
and tests/test_project_axis.py respectively — this file is the one place
every invariant (I1-I10, minus the ones that are structurally about a
different module) has a test that dies when the guard it names is removed.

Every mutation-check performed against the real source (guard deleted/
inverted, this exact test observed failing, guard restored) is recorded in
HANDOFF.md — not reproduced here as a code path, since there is nothing in
this file the mutation touches; the mutation happens in insight_gate.py
itself, temporarily, outside the committed diff.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

from insight_gate import (
    CLOSED_RELATION_TYPES,
    WALK_STEP_CYPHER,
    INSIGHT_AGE_CENSUS_K,
    _UnionFind,
    walk_reached_graph,
    walk_group_reached_set,
    passes_insight_gate,
    order_components,
    classify_identity,
)


# ── The closed relation set (§0, §2.3) — SUPERSEDES is never in it ───────────

def test_closed_relation_set_is_exactly_the_six_named_relations():
    assert set(CLOSED_RELATION_TYPES) == {
        "GROUNDED_IN", "INFORMED_BY", "CONSIDERED",
        "REJECTED", "UNDER_CONDITIONS", "HAD_OUTCOME",
    }


def test_closed_relation_set_never_contains_supersedes():
    """⛔ SETTLED (§2.3) — the walk must never follow SUPERSEDES. Structural
    guard: it cannot leak into the relation-type disjunction if it is never a
    member of the tuple the Cypher builder joins."""
    assert "SUPERSEDES" not in CLOSED_RELATION_TYPES
    assert "SUPERSEDES" not in WALK_STEP_CYPHER


# ── I1 — no gate predicate reads an entity name ───────────────────────────────

def test_i1_walk_cypher_never_touches_entity_or_mentions():
    """MUTATION-CHECKED (HANDOFF.md): adding `-[:MENTIONS]-(e:Entity)` back
    into `_walk_step_cypher` would make this fail."""
    assert "Entity" not in WALK_STEP_CYPHER
    assert "MENTIONS" not in WALK_STEP_CYPHER
    assert "REPORTS_ON" not in WALK_STEP_CYPHER
    assert "alias" not in WALK_STEP_CYPHER.lower()


# ── I2 — no gate predicate reads a count of projects ──────────────────────────

def test_i2_walk_cypher_never_touches_project_identity():
    """MUTATION-CHECKED (HANDOFF.md): reintroducing a `project_ids`/
    `size(projects)` clause (the pre-v2 ≥2-distinct-projects rule) would make
    this fail. A v2 gating group is exactly one (project, domain) pair by
    construction (nrem_gate.eligible_domain_level_clusters) — there is no
    project count left for the walk itself to read."""
    assert "Project" not in WALK_STEP_CYPHER
    assert "project_id" not in WALK_STEP_CYPHER
    assert "project_ids" not in WALK_STEP_CYPHER


def test_i2_passes_insight_gate_signature_carries_no_project_argument():
    import inspect
    params = list(inspect.signature(passes_insight_gate).parameters)
    assert params == ["labels", "consolidated"]


# ── I10 — a reversed Decision never enters the reached set ───────────────────

def test_i10_walk_cypher_excludes_a_reversed_decision_on_both_ends():
    """MUTATION-CHECKED (HANDOFF.md): deleting either NOT-clause from
    `_walk_step_cypher` makes this fail. The exclusion must be exact —
    `coalesce(*.superseded, false) = true` — and scoped to Decision only (a
    superseded FACT is still a valid pass-through node, plan §5.3/§8)."""
    assert WALK_STEP_CYPHER.count(
        "coalesce(n.superseded, false) = true") == 1
    assert WALK_STEP_CYPHER.count(
        "coalesce(m.superseded, false) = true") == 1
    assert "NOT (n:Decision AND coalesce(n.superseded, false) = true)" in WALK_STEP_CYPHER
    assert "NOT (m:Decision AND coalesce(m.superseded, false) = true)" in WALK_STEP_CYPHER
    # The exclusion must be Decision-scoped, never Fact-scoped or blanket.
    assert "coalesce(f.superseded" not in WALK_STEP_CYPHER
    assert "NOT (n:Fact AND" not in WALK_STEP_CYPHER
    assert "NOT (m:Fact AND" not in WALK_STEP_CYPHER


@pytest.mark.asyncio
async def test_i10_a_reversed_decision_the_cypher_never_yields_never_appears_anywhere():
    """The walk's Python side has nothing to filter — I10 is enforced entirely
    by the Cypher never yielding a reversed decision as a row (tested above).
    This proves the CONSEQUENCE holds given that contract: a component built
    from rows that never include the reversed decision contains no trace of
    it in labels, components, or (transitively) any gate count."""
    # D_live and D_reversed both ground on the same fact; the fake neighbour
    # function plays the role of the already-filtering Cypher and simply
    # never yields D_reversed — exactly what the live query does.
    adjacency = {1: [(201, "Decision", False)]}   # D_reversed=205 never appears
    labels, consolidated, components = await walk_reached_graph([1], _fake_neighbors(adjacency))
    assert 205 not in labels
    assert 205 not in consolidated
    assert all(205 not in c for c in components)


# ── _UnionFind — the components backbone ──────────────────────────────────────

def test_union_find_merges_transitively():
    uf = _UnionFind()
    uf.union(1, 2)
    uf.union(2, 3)
    assert uf.find(1) == uf.find(3)
    uf.union(4, 5)
    assert uf.find(1) != uf.find(4)


# ── walk_reached_graph — the fixpoint walk itself (I3) ────────────────────────

def _fake_neighbors(adjacency):
    """adjacency: {pg_id: [(dst, dst_label, dst_consolidated), ...]} — one
    fake BFS layer per call, batched across the whole frontier like the real
    Cypher does."""
    async def fetch(frontier):
        rows = []
        for src in frontier:
            for dst, label, consolidated in adjacency.get(src, []):
                rows.append({"src": src, "dst": dst, "dst_label": label,
                             "dst_consolidated": consolidated})
        return rows
    return fetch


@pytest.mark.asyncio
async def test_walk_reaches_direct_judgements_from_seed_facts():
    adjacency = {
        1: [(201, "Decision", False), (202, "Retrospective", False)],
    }
    labels, consolidated, components = await walk_reached_graph([1], _fake_neighbors(adjacency))
    assert labels == {201: "Decision", 202: "Retrospective"}
    assert consolidated == {201: False, 202: False}
    assert len(components) == 1
    assert set(components[0]) == {201, 202}


@pytest.mark.asyncio
async def test_walk_never_returns_a_fact_as_a_reached_judgement():
    """Facts are pass-through connectivity nodes, never themselves members of
    the reached set."""
    adjacency = {
        1: [(301, "Fact", False)],
        301: [(201, "Decision", False)],
    }
    labels, _consolidated, components = await walk_reached_graph([1], _fake_neighbors(adjacency))
    assert 301 not in labels
    assert set().union(*components) == {201}


@pytest.mark.asyncio
async def test_i3_walk_is_unbounded_no_hop_cap():
    """MUTATION-CHECKED (HANDOFF.md): a chain 12 hops deep from the seed
    fact — well past any hop bound the old design ever used (the pre-v2
    canonical_fixpoint_entity_cypher caps at 4) — is reached in full. Verified
    live by temporarily adding an artificial layer cap to
    insight_gate.walk_reached_graph, observing this exact test fail, and
    reverting (see HANDOFF.md for the exact numbers)."""
    CHAIN_LEN = 12
    adjacency = {}
    # seed fact 0 -> judgement 1 -> judgement 2 -> ... -> judgement CHAIN_LEN,
    # alternating Decision/Retrospective so both labels appear throughout.
    prev = 0
    for i in range(1, CHAIN_LEN + 1):
        label = "Decision" if i % 2 else "Retrospective"
        adjacency.setdefault(prev, []).append((i, label, False))
        prev = i
    labels, _consolidated, components = await walk_reached_graph([0], _fake_neighbors(adjacency))
    assert set(labels) == set(range(1, CHAIN_LEN + 1))
    assert len(components) == 1
    assert set(components[0]) == set(range(1, CHAIN_LEN + 1))


@pytest.mark.asyncio
async def test_walk_terminates_on_a_cycle_without_looping_forever():
    """Fixpoint termination (I3) relies on `seen` guarding re-entry — a cycle
    back to an already-visited node must not spin. Bounded by pytest-asyncio's
    own timeout if this regresses; the assertion is the real proof."""
    adjacency = {
        1: [(201, "Decision", False)],
        201: [(1, "Fact", False)],   # cycles back to the seed fact
    }
    labels, _consolidated, _components = await walk_reached_graph([1], _fake_neighbors(adjacency))
    assert labels == {201: "Decision"}


# ── I4 — freshness is tested on judgements, never on facts ────────────────────

@pytest.mark.asyncio
async def test_i4_a_facts_own_consolidated_flag_never_reaches_the_freshness_dict():
    """MUTATION-CHECKED (HANDOFF.md): dropping the judgement-label filter in
    `walk_reached_graph` (so a Fact's `dst_consolidated` populates the
    `consolidated`/`labels` dicts too) makes this fail — fact 301 carries
    `consolidated=True` here specifically so a defect that let it through
    would show up as a spurious dict key, not just a wrong gate answer."""
    adjacency = {
        1: [(301, "Fact", True)],           # fact — consolidated=True, irrelevant
        301: [(201, "Decision", False)],    # the only decision — fresh
        2: [(202, "Retrospective", False)],  # separate seed — gives G2 a retrospective
    }
    labels, consolidated, _components = await walk_reached_graph([1, 2], _fake_neighbors(adjacency))
    assert consolidated == {201: False, 202: False}   # never {..., 301: True}
    assert "Fact" not in labels.values()
    assert passes_insight_gate(labels, consolidated) is True


# ── I5 — two judgements sharing a grounding fact are in the SAME component ────

@pytest.mark.asyncio
async def test_i5_two_judgements_sharing_a_fact_are_one_component():
    """MUTATION-CHECKED (HANDOFF.md): moving `uf.union(src, dst)` inside the
    `if dst not in seen` guard (so a redundant same-batch rediscovery of an
    already-seen node is never unioned) makes this fail — D1 and D2 both
    reach the shared fact 301 in the SAME BFS layer's batch, so the union for
    the second row only matters if it still fires after the first row already
    marked 301 seen."""
    adjacency = {
        1: [(201, "Decision", False)],       # seed fact 1 -> D1
        2: [(202, "Decision", False)],       # seed fact 2 -> D2 (unrelated seed)
        201: [(301, "Fact", False)],         # D1 -> shared fact 301
        202: [(301, "Fact", False)],         # D2 -> shared fact 301 (same layer)
    }
    _labels, _consolidated, components = await walk_reached_graph([1, 2], _fake_neighbors(adjacency))
    assert len(components) == 1
    assert set(components[0]) == {201, 202}


@pytest.mark.asyncio
async def test_judgements_with_no_shared_fact_or_edge_are_separate_components():
    adjacency = {
        1: [(201, "Decision", False)],
        2: [(202, "Decision", False)],
    }
    _labels, _consolidated, components = await walk_reached_graph([1, 2], _fake_neighbors(adjacency))
    assert len(components) == 2
    assert {frozenset(c) for c in components} == {frozenset({201}), frozenset({202})}


# ── G2 + G3 — passes_insight_gate ─────────────────────────────────────────────

def test_i6_gate_requires_at_least_one_retrospective():
    """MUTATION-CHECKED (HANDOFF.md): inverting `has_retrospective` (or
    dropping it from the `and`) makes this fail — two fresh Decisions with no
    Retrospective must never pass, however many there are."""
    labels = {201: "Decision", 203: "Decision", 205: "Decision"}
    consolidated = {201: False, 203: False, 205: False}
    assert passes_insight_gate(labels, consolidated) is False
    labels[204] = "Retrospective"
    consolidated[204] = False
    assert passes_insight_gate(labels, consolidated) is True


def test_gate_requires_at_least_one_fresh_judgement_g3():
    labels = {201: "Decision", 204: "Retrospective"}
    consolidated = {201: True, 204: True}   # everything already consolidated
    assert passes_insight_gate(labels, consolidated) is False
    consolidated[201] = False
    assert passes_insight_gate(labels, consolidated) is True


def test_gate_on_empty_reach_is_false():
    assert passes_insight_gate({}, {}) is False


# ── §2.4 — order_components ────────────────────────────────────────────────────

def test_components_with_a_retrospective_sort_before_decision_only_ones():
    labels = {201: "Decision", 202: "Retrospective", 301: "Decision", 302: "Decision"}
    # component A: decision-only [301, 302]; component B: has a retrospective [201, 202]
    ordered = order_components([[302, 301], [202, 201]], labels)
    assert ordered == [[201, 202], [301, 302]]


def test_retrospective_containing_components_order_by_smallest_retro_pg_id():
    labels = {10: "Retrospective", 11: "Decision", 20: "Retrospective", 21: "Decision"}
    ordered = order_components([[21, 20], [11, 10]], labels)
    # component with retro pg_id 10 first, then the one with retro pg_id 20
    assert ordered == [[10, 11], [20, 21]]


def test_decision_only_components_order_by_smallest_decision_pg_id():
    labels = {50: "Decision", 51: "Decision", 30: "Decision"}
    ordered = order_components([[51, 50], [30]], labels)
    assert ordered == [[30], [50, 51]]


def test_a_lone_judgement_with_no_neighbours_is_still_a_component():
    labels = {999: "Decision"}
    ordered = order_components([[999]], labels)
    assert ordered == [[999]]


def test_within_a_component_members_are_ascending_pg_id():
    labels = {5: "Decision", 3: "Retrospective", 9: "Decision"}
    ordered = order_components([[9, 3, 5]], labels)
    assert ordered == [[3, 5, 9]]


# ── §2.5 — classify_identity ───────────────────────────────────────────────────

def test_identity_same_set_yields_same():
    assert classify_identity({201, 202}, {201, 202}) == "same"


def test_identity_new_strict_superset_supersedes_old():
    assert classify_identity({201, 202, 203}, {201, 202}) == "supersedes"


def test_identity_new_strict_subset_is_covered():
    assert classify_identity({201}, {201, 202}) == "covered"


def test_identity_partial_overlap_coexists():
    assert classify_identity({201, 999}, {201, 202}) == "overlap"


def test_identity_disjoint_sets_are_unrelated():
    assert classify_identity({111}, {201, 202}) == "disjoint"


def test_identity_two_empty_sets_are_same():
    assert classify_identity(set(), set()) == "same"


# ── Retained telemetry constant — no longer a gate parameter ─────────────────

# ── walk_group_reached_set — the real-I/O wrapper, against a fake driver ─────

class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    async def data(self):
        return self._rows


class _FakeSession:
    def __init__(self, shared_layers):
        self._layers = shared_layers  # SAME list object across sessions — one
                                       # real Neo4j session is opened per BFS
                                       # layer, so the queue must be shared,
                                       # not copied per `.session()` call.

    async def run(self, query, **params):
        assert "ids" in params
        return self._layers.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeDriver:
    """Duck-types neo4j.AsyncDriver's `.session()` contract — proves
    walk_group_reached_set needs nothing more than that, per its own
    docstring (no `import neo4j` anywhere in insight_gate.py)."""
    def __init__(self, layers):
        self._layers = list(layers)  # one FakeResult per expected .run() call

    def session(self):
        return _FakeSession(self._layers)


@pytest.mark.asyncio
async def test_walk_group_reached_set_drives_the_real_cypher_via_a_duck_typed_driver():
    driver = _FakeDriver([
        _FakeResult([{"src": 1, "dst": 201, "dst_label": "Decision", "dst_consolidated": False}]),
        _FakeResult([]),
    ])
    labels, consolidated, components = await walk_group_reached_set(driver, [1])
    assert labels == {201: "Decision"}
    assert consolidated == {201: False}
    assert components == [[201]]


def test_insight_age_census_k_is_a_plain_int_not_a_gate_parameter():
    """Renamed from INSIGHT_THRESHOLD (C2) — the old name's MEANING would
    otherwise invert under an unchanged name (a gate count -> a telemetry K),
    which CLAUDE.md names as exactly the trap to avoid."""
    assert isinstance(INSIGHT_AGE_CENSUS_K, int)
    assert INSIGHT_AGE_CENSUS_K >= 1
    import inspect
    assert "INSIGHT_THRESHOLD" not in dir(sys.modules["insight_gate"])
