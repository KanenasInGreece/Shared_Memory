"""The insight-cluster eligibility gate, as ONE query with two projections.

The daemon folds what this gate returns; telemetry reports how much of it there
is. Before this module those were two different questions asked two different
ways: ``consolidation_loop._find_fresh_insight_clusters`` ran the real
predicate, while ``coordinator``'s ``decision_cycles`` collected every eligible
Decision node flat and partitioned it by Postgres project — no shared entity,
no ≥2-projects rule, no HAD_OUTCOME. It reported 2 on a corpus where the daemon
could fold 0, and its own comment said it had to mirror the fold.

Re-chaining the old count would only have made a meaningless number
better-sourced, so the count is replaced: the same Cypher, projected to a
count. One definition, one place to change it.

Why a module of its own: ``coordinator.py`` and ``consolidation_loop.py``
import nothing from each other, and this is daemon POLICY (≥2 distinct
projects, ≥1 outcome, a hub cap) rather than graph vocabulary, so ``ontology.py``
is the wrong home even though the identifiers come from it.

SERVER-SIDE ONLY — never shipped in a skill.
"""
import os

from ontology import ONT

# Minimum unconsolidated, REM-enriched, non-superseded decisions converging on
# one entity. Lives in the ontology config because the fold threshold is part
# of the consolidation contract an operator may tune.
INSIGHT_THRESHOLD = ONT.insight_threshold

# Entities whose total degree exceeds this cap are mega-hubs (e.g. the project
# itself): clustering through them links everything to everything and produces
# meaningless insights. Context only, never a cluster key. Env-overridable —
# the right cap depends on corpus shape, not on our corpus.
INSIGHT_HUB_DEGREE_CAP = int(os.environ.get("INSIGHT_HUB_DEGREE_CAP", "50"))


def insight_cluster_cypher(count_only: bool = False) -> str:
    """Ratified eligibility gate — pure graph state, no LLM, no rating
    semantics: ≥ INSIGHT_THRESHOLD unconsolidated, REM-enriched, non-superseded
    decisions converging on a shared grounded Entity (non-mega-hub, carrying at
    least one Fact) across ≥2 distinct projects, where at least one decision has
    any HAD_OUTCOME edge — existence means reality has weighed in at least once.

    ADR-017: clusters are keyed on the ALIAS COMPONENT, not the bare entity —
    the same join ``_find_anchored_clusters`` applies to facts, so alias-linked
    surface forms ('Cloe VM'/'CloeVM') merge instead of forming two thinner
    clusters. The canonical name is the lexicographically smallest member,
    matching the fact-fold's rule. No-op-safe: with no ALIASES edges every
    entity is its own component.

    ``count_only`` swaps the projection for a bucket count and nothing else.
    Telemetry needs the gauge, not the payload — but it must be a gauge of the
    SAME predicate, which is the whole reason this is one function.

    ⛔ THE DISCRIMINATOR IS REGISTRY IDENTITY, NOT THE NAME (migration 027).
    "Two distinct projects" is counted over ``p.project_id``, while the payload
    still reports names because names are what a reader renders. Counting the
    name made the gate correct only while the node set happened to be one-to-one
    with the registry, which nothing enforced: two nodes left by a partly-applied
    rename make one project count twice and synthesise a "cross-project" insight
    out of a single project's decisions. It must equally not be the internal node
    id — with no uniqueness constraint two nodes sharing a name collapse
    correctly under the name and would count as two under ``elementId``, which is
    strictly worse than what it replaced.

    ⚠ IT FAILS CLOSED, and that is the point of using ``collect`` rather than a
    null test: ``collect`` DISCARDS nulls, so a project node with no identity
    contributes nothing to ``project_ids`` and cannot carry a cluster over the
    two-project line. An unidentified project therefore costs a fold that might
    have been legitimate, which is the cheap direction — the expensive one is a
    false cross-project insight, and a name fallback would keep that live for the
    whole upgrade window and permanently for any residue. ``GET /health`` reports
    how many project nodes are still unidentified so this is a visible state and
    not a silent one.

    Parameters: ``$hub_cap``, ``$threshold``.
    """
    projection = (
        " RETURN count(*) AS cycles"
        if count_only else
        f" RETURN reduce(c = null, nm IN [x IN members | x.name] |"
        f"          CASE WHEN c IS NULL OR nm < c THEN nm ELSE c END) AS entity,"
        f"        [d IN ds | d.pg_id] AS decision_ids,"
        f"        projects"
    )
    return (
        f"MATCH (d0:{ONT.decision})-[:{ONT.entity_link_alias}|{ONT.entity_link}]->(e0:{ONT.entity})"
        f" WHERE d0.pg_id IS NOT NULL"
        f"   AND coalesce(d0.consolidated, false) = false"
        f"   AND coalesce(d0.rem_processed, false) = true"
        f"   AND coalesce(d0.superseded, false) = false"
        f"   AND size([(e0)--(x) | x]) <= $hub_cap"
        f"   AND size([(e0)<-[:{ONT.entity_link_alias}|{ONT.entity_link}]-(f:{ONT.fact}) | f]) > 0"
        f" WITH DISTINCT e0"
        f" CALL (e0) {{"
        f"   OPTIONAL MATCH (sib:{ONT.entity})"
        f"     WHERE e0.alias_component IS NOT NULL"
        f"       AND sib.alias_component = e0.alias_component"
        f"   WITH e0, collect(sib) AS sibs"
        f"   RETURN CASE WHEN e0.alias_component IS NULL"
        f"               THEN [e0] ELSE sibs END AS members"
        f" }}"
        f" WITH coalesce(e0.alias_component, elementId(e0)) AS comp, members"
        f" WITH comp, head(collect(members)) AS members"   # dedup anchors → 1 row/component
        f" UNWIND members AS m"
        f" MATCH (m)<-[:{ONT.entity_link_alias}|{ONT.entity_link}]-(d:{ONT.decision})"
        f" WHERE d.pg_id IS NOT NULL"
        f"   AND coalesce(d.consolidated, false) = false"
        f"   AND coalesce(d.rem_processed, false) = true"
        f"   AND coalesce(d.superseded, false) = false"
        f" MATCH (d)-[:{ONT.project_of}]->(p:{ONT.project})"
        f" WITH members, collect(DISTINCT d) AS ds,"
        f"      collect(DISTINCT p.name) AS projects,"
        f"      collect(DISTINCT p.project_id) AS project_ids"
        f" WHERE size(ds) >= $threshold"
        f"   AND size(project_ids) >= 2"
        f"   AND any(d IN ds WHERE size([(d)-[:{ONT.had_outcome}]->(x) | x]) > 0)"
        + projection
    )
