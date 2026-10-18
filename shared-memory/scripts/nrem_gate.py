"""Pure (project, domain) fold partitioner without DB drivers for telemetry import.

Imports are the stdlib plus ``project_axis`` and ``ontology`` only, enforced by
``test_nrem_gate_import_purity.py``.
"""

from ontology import ONT
from project_axis import fold_eligible


def grounded_fact_groups_cypher(with_content: bool) -> str:
    """The walk is GROUNDED_IN, then DOMAIN_OF, then PROJECT_OF, never MENTIONS, and
    an edge exists only for a registered section, so its presence proves registration.
    ``with_content`` adds the fact text to the rows."""
    walk = (
        f"MATCH (j) WHERE j:{ONT.decision} OR j:{ONT.retrospective}"
        f" MATCH (j)-[:{ONT.grounded_in}]->(f:{ONT.fact})"
        f" WHERE coalesce(f.superseded, false) = false"
        f" MATCH (f)-[:{ONT.domain_of}]->(dom:{ONT.domain})"
        f"           -[:{ONT.project_of}]->(proj:{ONT.project})"
        f" WITH DISTINCT f, proj.name AS project, dom.name AS domain"
    )
    if not with_content:
        return walk + " RETURN f.pg_id AS pg_id, project, domain"
    return (
        walk
        # rem_summary when REM condensed a long fact; otherwise the curated text.
        + " RETURN f.pg_id AS pg_id,"
        + "        coalesce(f.rem_summary, f.content) AS content,"
        + "        project, domain"
    )


def project_domain_maps(rows):
    """Fold the discovery rows ``{pg_id, project, domain}`` into per-fact maps.

    Returns ``(project_map, domains_map, registered_sections, pg_ids_all)``;
    ``pg_ids_all`` is first-seen pg_id order, the order the partitioner pairs
    with contents. Pure.
    """
    project_map: dict = {}
    domains_map: dict = {}
    registered_sections: set = set()
    for r in rows:
        pid = r["pg_id"]
        project_map[pid] = r["project"]
        doms = domains_map.setdefault(pid, [])
        if r["domain"] not in doms:
            doms.append(r["domain"])
        registered_sections.add((r["project"], r["domain"]))
    return project_map, domains_map, registered_sections, list(project_map)


def eligible_domain_level_clusters(contents, pg_ids, project_map, domains_map,
                                   threshold, registered_sections):
    """The one partitioner shared by ``consolidation_loop._consolidate_clusters``,
    ``consolidation_loop._find_fresh_insight_clusters`` and
    ``coordinator._nrem_cycle_counts``; a multi-section fact fans out to each section.
    Only registered sections count (the caller derives the registry from the same
    edges, a cheap confirmation, not a second source of truth), and only buckets
    with at least ``threshold`` members are returned as ``((project, section),
    contents, pg_ids)``.
    """
    registered = registered_sections or set()
    buckets: dict = {}
    for content, pid in zip(contents, pg_ids):
        project = project_map.get(pid)
        if not fold_eligible(project):
            continue
        sections = domains_map.get(pid) or []
        for section in sections:
            if not isinstance(section, str) or not section.strip():
                continue
            key = (project, section)
            if key not in registered:
                continue
            bucket = buckets.setdefault(key, ([], []))
            if pid in bucket[1]:
                continue
            bucket[0].append(content)
            bucket[1].append(pid)
    return [
        (key, c, p)
        for key, (c, p) in buckets.items()
        if len(p) >= threshold
    ]


def count_domain_level_cycles(pg_ids, project_map, domains_map, threshold,
                              registered_sections):
    """Count-only wrapper kept for callers that need only the number (tests and the
    consolidation_loop re-export)."""
    contents = [""] * len(pg_ids)
    return len(eligible_domain_level_clusters(
        contents, pg_ids, project_map, domains_map, threshold,
        registered_sections))
