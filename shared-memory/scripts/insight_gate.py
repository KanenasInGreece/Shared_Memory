"""Insight gate: from a fact-group seed, require a retrospective (G2) and a fresh
judgement (G3), then componentise the reached set, as `fact:1157` specifies (the v0.8.66
record of G2/G3, the unbounded walk and the reversed-decision exclusion).

Imports only ontology and stdlib; the walk takes an already-built Neo4j driver as a parameter. Server-side only.
"""
from ontology import GROUNDING_RELATIONS, ONT

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


def carried_readiness_cypher() -> str:
    """Rows for the ids one insight fold would put in its prompt.

    The alias ``AS rem_invalid`` appears only here, so a rig can recognise
    this read by its text. The caller issues it even when ``$ids`` is empty:
    a graph outage must fail the readiness check, not a later walk.
    """
    return (
        f"MATCH (n) WHERE (n:{ONT.fact} OR n:{ONT.decision} OR n:{ONT.retrospective})"
        f" AND n.pg_id IN $ids"
        f" RETURN n.pg_id AS pg_id,"
        f" coalesce(n.rem_processed, false) AS rem_processed,"
        f" coalesce(n.superseded, false) AS superseded,"
        f" coalesce(n.rem_invalid, false) AS rem_invalid,"
        f" labels(n) AS labels"
    )


CARRIED_READINESS_CYPHER: str = carried_readiness_cypher()


def _flag(value) -> bool:
    if value is True or value == 1:
        return True
    if isinstance(value, str) and value.strip().lower() == "true":
        return True
    return False


def _row_labels(row) -> set:
    raw = row.get("labels")
    if raw is None:
        return set()
    if isinstance(raw, str):
        return {raw}
    try:
        return {str(item) for item in raw}
    except TypeError:
        return set()


def unprocessed_carried(carried_ids, rows) -> tuple[list[int], list[int]]:
    """Return carried ids that are not ready to fold, and the subset with more than one spine node.

    A missing node waits; one superseded row is exempt; any other id is ready
    only when rem_processed is set, an invalid node included; a second spine
    node is never exempted (fact:3458 measured no pg_id with two spine nodes);
    both lists are ascending ints without repeats, and a row outside the
    carried ids is ignored. One Decision node is ready on presence alone:
    its stored rem_processed is not a gate (decision:3582). A row with no
    labels keeps the fact and retrospective rule, so a caller that does not
    return labels still judges those rows by rem_processed.
    """
    wanted: list[int] = []
    seen: set[int] = set()
    for raw in carried_ids:
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            continue
        if pid not in seen:
            seen.add(pid)
            wanted.append(pid)
    by_id: dict[int, list] = {}
    for row in rows:
        try:
            pid = int(row["pg_id"])
        except (TypeError, ValueError, KeyError):
            continue
        if pid not in seen:
            continue
        by_id.setdefault(pid, []).append(row)
    unprocessed: list[int] = []
    duplicated: list[int] = []
    for pid in sorted(wanted):
        group = by_id.get(pid, [])
        if len(group) == 0:
            unprocessed.append(pid)
        elif len(group) > 1:
            # Multiplicity is first. Two Decision nodes still defer and raise
            # the duplicate error (decision:3582).
            unprocessed.append(pid)
            duplicated.append(pid)
        elif ONT.decision in _row_labels(group[0]):
            continue
        elif _flag(group[0].get("superseded")):
            continue
        elif not _flag(group[0].get("rem_processed")):
            unprocessed.append(pid)
    return unprocessed, duplicated


def decision_readiness_cypher() -> str:
    """Per pg_id: ready, blocking_ids, and spine_nodes.

    A Fact on an outgoing grounding edge, or a Retrospective on an outgoing
    HAD_OUTCOME, blocks while it is live, has a pg_id, and is neither
    rem_processed nor rem_invalid. A node with no pg_id is not a blocker
    (fact:3587). Ready is false when any Decision twin of that pg_id has
    such a blocker. spine_nodes counts Fact, Decision and Retrospective
    nodes that carry the pg_id. This text has no ``AS rem_invalid`` alias:
    that alias belongs to carried_readiness_cypher so a rig can tell the
    two reads apart.
    """
    grounding = ", ".join(repr(rel) for rel in GROUNDING_RELATIONS)
    return (
        "UNWIND $ids AS pid "
        "OPTIONAL MATCH (spine) "
        "WHERE spine.pg_id = pid "
        f"AND (spine:{ONT.fact} OR spine:{ONT.decision} OR spine:{ONT.retrospective}) "
        "WITH pid, count(spine) AS spine_nodes "
        f"OPTIONAL MATCH (d:{ONT.decision} {{pg_id: pid}})-[rel]->(b) "
        f"WHERE ((type(rel) IN [{grounding}] AND b:{ONT.fact}) "
        f"OR (type(rel) = '{ONT.had_outcome}' AND b:{ONT.retrospective})) "
        "AND b.pg_id IS NOT NULL "
        "AND coalesce(b.superseded, false) = false "
        "AND coalesce(b.rem_processed, false) = false "
        "AND coalesce(b.rem_invalid, false) = false "
        "WITH pid, spine_nodes, collect(DISTINCT CASE "
        f"WHEN b:{ONT.fact} THEN 'fact:' + toString(b.pg_id) "
        f"WHEN b:{ONT.retrospective} THEN 'retrospective:' + toString(b.pg_id) "
        "ELSE null END) AS blocking_ids "
        "RETURN pid AS pg_id, (size(blocking_ids) = 0) AS ready, "
        "blocking_ids, spine_nodes"
    )


def _readiness_by_id(readiness) -> dict:
    by_id: dict = {}
    if isinstance(readiness, dict) and "pg_id" not in readiness and "ready" not in readiness:
        for key, row in readiness.items():
            if not isinstance(row, dict):
                continue
            try:
                pid = int(row.get("pg_id", key))
            except (TypeError, ValueError):
                continue
            by_id[pid] = row
        return by_id
    rows = [readiness] if isinstance(readiness, dict) else list(readiness or [])
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            by_id[int(row["pg_id"])] = row
        except (TypeError, ValueError, KeyError):
            continue
    return by_id


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def admit_members(labels, consolidated, components, readiness):
    """Drop unconsolidated Decisions that are not ready.

    An admitted Decision (consolidated true) stays a member even when it is
    not ready (decision:3582). A pg_id with spine_nodes greater than 1 stays,
    so the duplicate refusal still fires. The walk is not repeated. Returns
    the filtered labels, consolidated map, components, and the excluded
    decisions as ``{pg_id, blocking_ids}``. A missing readiness row is not
    an exclusion: a failed read is the caller's exception path.
    """
    by_id = _readiness_by_id(readiness)
    consolidated = consolidated or {}
    drop: dict[int, list] = {}
    for raw_pid, label in (labels or {}).items():
        pid = _as_int(raw_pid)
        if pid is None or label != ONT.decision:
            continue
        if _flag(consolidated.get(raw_pid, consolidated.get(pid))):
            continue
        row = by_id.get(pid)
        if row is None or "ready" not in row:
            continue
        ready = row.get("ready")
        if ready is True or ready == 1:
            continue
        if ready is not False and ready != 0:
            continue
        raw_spine = row.get("spine_nodes")
        if raw_spine is None:
            spine_n = 1
        else:
            spine_n = _as_int(raw_spine)
            if spine_n is None:
                spine_n = 1
        if spine_n > 1:
            continue
        blockers = row.get("blocking_ids") or []
        drop[pid] = list(blockers)
    excluded = [
        {"pg_id": pid, "blocking_ids": drop[pid]} for pid in sorted(drop)
    ]
    if not drop:
        return labels, consolidated, components, excluded
    new_labels = {
        key: label for key, label in (labels or {}).items()
        if _as_int(key) not in drop
    }
    new_consolidated = {
        key: value for key, value in consolidated.items()
        if _as_int(key) not in drop
    }
    new_components = []
    for comp in components or []:
        kept = [item for item in comp if _as_int(item) not in drop]
        if kept:
            new_components.append(kept)
    return new_labels, new_consolidated, new_components, excluded


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
