"""THE v2 INSIGHT GATE — walk, components, ordering, identity (Dreaming Cycle
Plan to v2, §2.2-§2.5). Replaces the pre-v2 1-hop shared-Entity gate wholesale:
no entity anchor, no ≥2-distinct-projects rule, no hub-degree cap. See
``Local_Documentation/Dreaming_Cycle_Plan_to_v2.md`` for the design this file
implements — it is the authority; this module is the mechanism.

G1 (the group must pass the FACT GATE) is deliberately NOT re-implemented
here — it is ``nrem_gate.eligible_domain_level_clusters`` fed by
``consolidation_loop._find_grounded_fact_groups``'s graph-native discovery.
Callers pass this module the resulting group's grounded, non-superseded fact
pg_ids as the walk's seed; this module answers G2 (>=1 Retrospective reached)
and G3 (>=1 fresh/unconsolidated judgement reached), computes the reached
judgement set's connected components (§2.4), orders them deterministically,
and classifies a new reach against an existing insight's coverage (§2.5).

⛔ DRIVER-FREE, LIKE nrem_gate.py — imports only ``ontology`` and stdlib. The
shipped gateway service (coordinator.py's process) carries no ``psycopg2``
and reaches this module via a TOP-LEVEL import (unlike ``nrem_gate``, which
coordinator.py imports lazily) — see the nrem-telemetry-gauge fix
(v0.8.65) for the failure class this avoids. The async walk driver below
takes an ALREADY-CONSTRUCTED Neo4j driver object as a plain parameter
(duck-typed: anything exposing ``.session()`` the way ``neo4j.AsyncDriver``
does) rather than importing the ``neo4j`` package itself, so this module
never needs to import a DB/network driver to do real graph I/O.
``tests/test_insight_gate_import_purity.py`` enforces the source-level
guarantee.

SERVER-SIDE ONLY — never shipped in a skill.
"""
from ontology import ONT

# THE CLOSED RELATION SET (plan §0): grounding edges + HAD_OUTCOME. Nothing
# else — in particular NEVER SUPERSEDES (§2.3 is explicit and settled: the
# walk must never follow it). Order here is cosmetic only; the Cypher below
# matches all six as one undirected relationship-type disjunction.
CLOSED_RELATION_TYPES: tuple[str, ...] = (
    ONT.grounded_in, ONT.informed_by, ONT.considered,
    ONT.rejected, ONT.under_conditions, ONT.had_outcome,
)

_JUDGEMENT_LABELS = (ONT.decision, ONT.retrospective)

# Retained ONLY as the telemetry age-percentile K for
# ``_kth_oldest_age_seconds`` (consolidation_loop.run_insight_cycle) — how
# many of a component's oldest members set its "how long has this been
# eligible" reading. It is NOT a gate parameter any more: v2 has no
# per-decision-count threshold (G1's density lives in ONT.density_threshold;
# G2/G3 are each "at least one", not a tunable count). Kept under its old
# name deliberately narrowed rather than silently repurposed — see the
# CLAUDE.md rule that a metric's meaning must never invert under an unchanged
# name; the docstring here is that rename-in-place.
INSIGHT_AGE_CENSUS_K: int = ONT.insight_threshold


def _walk_step_cypher() -> str:
    """One BFS layer (I3) over the closed relation set, undirected, from the
    current frontier (``$ids``) to its unvisited neighbours.

    Matches only {Fact, Decision, Retrospective} on both ends — a bare
    ``{pg_id: $pid}`` property match would also hit CommunitySummary nodes,
    whose pg_id sequence (``community_summaries.id``) is independent of
    ``technical_docs.id`` and can coincide by coincidence (the same collision
    risk §3.2 names for source_pg_ids vs summary_ids).

    I10: a reversed Decision (``superseded = true``) is excluded on BOTH
    ends — as ``m`` so it is never discovered/added to the frontier, and as
    ``n`` (belt-and-braces: by construction a reversed decision can never
    already be in the frontier, since it would have failed the ``m`` check
    the layer that would have discovered it, but the walk must not depend on
    that invariant holding for a reason it cannot see).

    Facts carry no such exclusion here — only DECISION supersession removes a
    node from the walk (I10 is decision-specific); a superseded Fact is still
    a valid pass-through connectivity node (measured live, plan §5.3/§8: two
    judgements sharing a since-superseded fact remain one component).

    Returns ``{src, dst, dst_label, dst_consolidated}`` rows — ``dst_label``
    is one of 'Fact' / 'Decision' / 'Retrospective'; ``dst_consolidated`` is
    only meaningful for judgement rows (I4: freshness is tested on
    judgements, never on facts) and is read regardless so the caller need not
    special-case it.
    """
    rels = "|".join(CLOSED_RELATION_TYPES)
    return (
        f"UNWIND $ids AS pid"
        f" MATCH (n {{pg_id: pid}})"
        f" WHERE (n:{ONT.fact} OR n:{ONT.decision} OR n:{ONT.retrospective})"
        f"   AND NOT (n:{ONT.decision} AND coalesce(n.superseded, false) = true)"
        f" MATCH (n)-[:{rels}]-(m)"
        f" WHERE (m:{ONT.fact} OR m:{ONT.decision} OR m:{ONT.retrospective})"
        f"   AND NOT (m:{ONT.decision} AND coalesce(m.superseded, false) = true)"
        f" RETURN DISTINCT pid AS src, m.pg_id AS dst,"
        f"   CASE WHEN m:{ONT.decision} THEN '{ONT.decision}'"
        f"        WHEN m:{ONT.retrospective} THEN '{ONT.retrospective}'"
        f"        ELSE '{ONT.fact}' END AS dst_label,"
        f"   coalesce(m.consolidated, false) AS dst_consolidated"
    )


WALK_STEP_CYPHER: str = _walk_step_cypher()


class _UnionFind:
    """Disjoint-set over pg_ids — connectivity backbone for §2.4 components.
    Path-halving find, union by attach-to-other-root (no rank tracking
    needed at this corpus size). Pure, no I/O."""

    def __init__(self):
        self._parent: dict = {}

    def find(self, x):
        self._parent.setdefault(x, x)
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb


async def walk_reached_graph(seed_fact_ids, fetch_neighbors):
    """THE WALK (plan §2.3) — undirected, unbounded fixpoint (I3) over the
    closed relation set, starting from a gating group's grounded,
    non-superseded fact pg_ids.

    ``fetch_neighbors`` is an async callable: ``list[int] -> list[dict]``
    (one BFS layer for the given frontier, shaped like ``_walk_step_cypher``'s
    RETURN row). Injected rather than hard-wired to a Neo4j session so this
    function is testable with a plain fake — see
    ``tests/test_insight_gate.py``.

    Termination is BY FIXPOINT, not a hop count: each layer only re-queries
    nodes newly discovered this layer (``frontier``), and a node is queried
    at most once (``seen`` guards re-entry) — the loop ends when a layer
    discovers nothing new, which is guaranteed to happen because the graph is
    finite. No hop cap, no edge cap, no hub cap (I3) — nothing here bounds
    how many layers run.

    Facts are traversed (so two judgements sharing a fact connect, per §0 and
    I5) but never themselves returned as members of the reached set — the
    ``labels``/``consolidated`` dicts below only ever gain a Decision or
    Retrospective key. I10 is enforced by ``_walk_step_cypher`` itself: a
    reversed Decision is never yielded as ``dst``, so it can never enter
    ``seen``/``labels``/a component.

    Returns ``(labels, consolidated, components)``:
      ``labels``       -- {judgement_pg_id: 'Decision' | 'Retrospective'}
      ``consolidated``  -- {judgement_pg_id: bool}
      ``components``   -- list[list[int]], each an UNORDERED judgement-id
                           component (§2.4) — pass to ``order_components``
                           for the deterministic fold order.
    """
    uf = _UnionFind()
    labels: dict = {}
    consolidated: dict = {}
    seed_fact_ids = list(seed_fact_ids)
    seen = set(seed_fact_ids)
    for s in seed_fact_ids:
        uf.find(s)
    frontier = list(seed_fact_ids)
    while frontier:
        rows = await fetch_neighbors(frontier)
        frontier = []
        for row in rows:
            src, dst, dst_label = row["src"], row["dst"], row["dst_label"]
            uf.union(src, dst)
            if dst in seen:
                continue
            seen.add(dst)
            if dst_label in _JUDGEMENT_LABELS:
                labels[dst] = dst_label
                consolidated[dst] = bool(row.get("dst_consolidated"))
            frontier.append(dst)

    groups: dict = {}
    for pid in labels:
        groups.setdefault(uf.find(pid), []).append(pid)
    return labels, consolidated, list(groups.values())


async def walk_group_reached_set(driver, seed_fact_ids):
    """Real-I/O convenience wrapper over ``walk_reached_graph`` — ``driver``
    is any object exposing ``.session()`` the way ``neo4j.AsyncDriver`` does
    (``consolidation_loop.ConsolidationDaemon.driver`` and
    ``coordinator.MemoryCoordinator._neo4j`` both qualify; both callers pass
    their own already-constructed driver, so this module never imports
    ``neo4j`` itself)."""
    async def fetch_neighbors(frontier):
        async with driver.session() as session:
            result = await session.run(WALK_STEP_CYPHER, ids=frontier)
            return await result.data()
    return await walk_reached_graph(seed_fact_ids, fetch_neighbors)


def passes_insight_gate(labels, consolidated) -> bool:
    """G2 + G3 (plan §2.2) — evaluated once per gating group's FULL reached
    set, never per component (§2.4: components structure the payload, they
    do not gate; every component in a passing group folds).

    G1 (fact gate) is the caller's job — this is only ever invoked on a group
    that has already cleared it (nrem_gate.eligible_domain_level_clusters).

    G2 — at least one Retrospective anywhere in the reached set.
    G3 — at least one judgement (Decision OR Retrospective) with
         ``consolidated = false`` anywhere in the reached set. I4: this reads
         ONLY ``consolidated`` (judgements) — never a fact's own flag, which
         plays no part here or anywhere in this module.
    """
    has_retrospective = any(l == ONT.retrospective for l in labels.values())
    has_fresh = any(not c for c in consolidated.values())
    return has_retrospective and has_fresh


def order_components(components, labels):
    """§2.4 deterministic component ordering — stable across re-folds so the
    upserted insight's content-comparison is meaningful cycle over cycle.

    1. Components containing >=1 Retrospective come first, ordered by their
       SMALLEST retrospective pg_id.
    2. Components with no Retrospective follow, ordered by their smallest
       (any-judgement) pg_id — this also covers a lone judgement with no
       neighbours, which is still a one-member component.
    3. Within a component: ascending pg_id (also causal order — a
       retrospective's pg_id always postdates the decision it evaluates).

    Pure. Returns a NEW list of ascending-pg_id-sorted component lists, in
    fold order.
    """
    def sort_key(component):
        ids = sorted(component)
        retro_ids = [i for i in ids if labels.get(i) == ONT.retrospective]
        if retro_ids:
            return (0, min(retro_ids))
        return (1, min(ids) if ids else 0)

    return [sorted(c) for c in sorted(components, key=sort_key)]


def classify_identity(new_ids, existing_ids) -> str:
    """§2.5 — LOCKED. An insight's identity is the SET of judgement pg_ids it
    covers. Pure set comparison between a freshly-walked component's judgement
    ids and one existing active insight's judgement-id set.

      'same'       -- identical sets. No new insight — the caller (C3) adds
                       the triggering thematic summary/domain as a reference
                       on the existing insight rather than folding again.
      'supersedes' -- ``new_ids`` is a STRICT superset of ``existing_ids``.
                       New insight; it supersedes the old by subset coverage.
      'covered'    -- ``existing_ids`` is a strict superset of ``new_ids``
                       (the reverse of 'supersedes') — the existing insight
                       already covers this reach in full; nothing new to add.
                       Not named in the plan's LOCKED table because the walk
                       only grows over time in the documented scenario, but
                       the same set logic settles it the same way: no new
                       insight is warranted.
      'overlap'    -- partial intersection, neither a subset of the other.
                       Both coexist (accepted early duplication, §2.5).
      'disjoint'   -- no shared members — unrelated insights.
    """
    new_ids, existing_ids = set(new_ids), set(existing_ids)
    if new_ids == existing_ids:
        return "same"
    if existing_ids < new_ids:
        return "supersedes"
    if new_ids < existing_ids:
        return "covered"
    if new_ids & existing_ids:
        return "overlap"
    return "disjoint"
