"""Insight gate: from a fact-group seed, require a retrospective (G2) and a fresh
judgement (G3), then componentise the reached set, as `fact:1157` specifies (the v0.8.66
record of G2/G3, the unbounded walk and the reversed-decision exclusion).

Imports only ontology and stdlib; the walk takes an already-built Neo4j driver as a parameter. Server-side only.
"""
from ontology import ONT

# Grounding edges plus HAD_OUTCOME only. The walk must not follow SUPERSEDES, or a reversed decision pulls the next record in.
CLOSED_RELATION_TYPES: tuple[str, ...] = (
    ONT.grounded_in, ONT.informed_by, ONT.considered,
    ONT.rejected, ONT.under_conditions, ONT.had_outcome,
)

_JUDGEMENT_LABELS = (ONT.decision, ONT.retrospective)


def _census_rank(k: int) -> int:
    """Return ``k`` if it is a valid 1-based census rank (>= 1), else raise.

    The gauge indexes ``ts[k - 1]``: 0 would read the newest member and a negative
    value an end-relative one, so a non-positive rank is refused at import."""
    if k < 1:
        raise ValueError(
            f"consolidation.insight_threshold in ontology.yaml must be a whole "
            f"number >= 1 (the census rank K), got {k!r}")
    return k


# K is the rank of the coverage-debt gauge only, not a gate (G2/G3 are at-least-one
# checks). Its value comes from `consolidation.insight_threshold` in ontology.yaml.
INSIGHT_AGE_CENSUS_K: int = _census_rank(ONT.insight_threshold)


def _walk_step_cypher() -> str:
    """One BFS layer over the closed relation set, undirected, from the
    current frontier (``$ids``) to its unvisited neighbours.

    Matches only {Fact, Decision, Retrospective} on both ends — a bare
    ``{pg_id: $pid}`` property match would also hit CommunitySummary nodes,
    whose pg_id sequence (``community_summaries.id``) is independent of
    ``technical_docs.id`` and can coincide by coincidence (the same collision
    risk as between source_pg_ids and summary_ids).

    A reversed Decision (``superseded = true``) is excluded on BOTH
    ends — as ``m`` so it is never discovered/added to the frontier, and as
    ``n`` (belt-and-braces: by construction a reversed decision can never
    already be in the frontier, since it would have failed the ``m`` check
    the layer that would have discovered it, but the walk must not depend on
    that invariant holding for a reason it cannot see).

    Facts carry no such exclusion here — only DECISION supersession removes a
    node from the walk (the exclusion is decision-specific); a superseded Fact
    is still a valid pass-through connectivity node (measured live, `fact:1157`: two
    judgements sharing a since-superseded fact remain one component).

    Returns ``{src, dst, dst_label, dst_consolidated}`` rows — ``dst_label``
    is one of 'Fact' / 'Decision' / 'Retrospective'; ``dst_consolidated`` is
    only meaningful for judgement rows (freshness is tested on
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
    """Disjoint-set over pg_ids — connectivity backbone for the components.
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
    """THE WALK — undirected, unbounded fixpoint over the
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
    finite. No hop cap, no edge cap, no hub cap — nothing here bounds
    how many layers run.

    Facts are traversed (so two judgements sharing a fact connect) but never
    themselves returned as members of the reached set — the
    ``labels``/``consolidated`` dicts below only ever gain a Decision or
    Retrospective key. The reversed-decision exclusion is enforced by
    ``_walk_step_cypher`` itself: a reversed Decision is never yielded as
    ``dst``, so it can never enter ``seen``/``labels``/a component.

    Returns ``(labels, consolidated, components)``:
      ``labels``       -- {judgement_pg_id: 'Decision' | 'Retrospective'}
      ``consolidated``  -- {judgement_pg_id: bool}
      ``components``   -- list[list[int]], each an UNORDERED judgement-id
                           component — pass to ``order_components``
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
    """G2 + G3 — evaluated once per gating group's FULL reached set, never per
    component (components structure the payload, they do not gate).

    Two later drops are separate from this gate: `_find_fresh_insight_clusters`
    skips a component with no Decision, and a one-judgement component still
    resolves identity (and can append on 'same'), but only an unmatched one is
    left out of the fold.

    G1 (fact gate) is the caller's job — this is only ever invoked on a group
    that has already cleared it (nrem_gate.eligible_domain_level_clusters).

    G2 — at least one Retrospective anywhere in the reached set.
    G3 — at least one judgement (Decision OR Retrospective) with
         ``consolidated = false`` anywhere in the reached set. This reads
         ONLY ``consolidated`` (judgements) — never a fact's own flag, which
         plays no part here or anywhere in this module.
    """
    has_retrospective = any(l == ONT.retrospective for l in labels.values())
    has_fresh = any(not c for c in consolidated.values())
    return has_retrospective and has_fresh


def order_components(components, labels):
    """Deterministic component ordering — stable across re-folds so the
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
    """An insight's identity is the SET of judgement pg_ids it
    covers. Pure set comparison between a freshly-walked component's judgement
    ids and one existing active insight's judgement-id set.

      'same'       -- identical sets. No new insight — `run_insight_cycle`
                       adds the triggering thematic summary/domain as a
                       reference on the existing insight rather than
                       folding again.
      'supersedes' -- ``new_ids`` is a STRICT superset of ``existing_ids``.
                       New insight; it supersedes the old by subset coverage.
      'covered'    -- ``existing_ids`` is a strict superset of ``new_ids``
                       (the reverse of 'supersedes') — the existing insight
                       already covers this reach in full; nothing new to add.
                       Rare, since a walk normally only grows, but the same
                       set logic settles it: no new insight is warranted.
      'overlap'    -- partial intersection, neither a subset of the other.
                       Both coexist (accepted early duplication).
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


def match_existing_insight(new_ids, existing):
    """Pick the active insight a walked component matches, independent of row order.

    ``existing`` is ``fetch_active_insight_rows``' list of ``(id, judgement_id_set,
    metadata)``. Returns ``("same", id, metadata)`` for the smallest-id 'same' row if
    any, else ``("covered", id, metadata)`` for the smallest-id 'covered' row, else
    ``None``. 'same' wins so an equal insight always gains the reference even when
    another insight also covers the set."""
    best = {}
    for iid, iset, imeta in existing:
        rel = classify_identity(new_ids, iset)
        if rel in ("same", "covered") and (rel not in best or iid < best[rel][1]):
            best[rel] = (rel, iid, imeta)
    return best.get("same") or best.get("covered")
