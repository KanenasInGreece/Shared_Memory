"""Pure (project, domain) fold partitioner; no DB driver, so the gateway telemetry gauge can import it without psycopg2.

Keep imports stdlib-only besides project_axis and ontology; test_nrem_gate_import_purity.py enforces that.
"""

from project_axis import fold_eligible


def eligible_domain_level_clusters(contents, pg_ids, project_map, domains_map,
                                   threshold, registered_sections):
    """THE v2 FACT GATE PARTITIONER (Dreaming Cycle Plan to v2, §2.1) — the
    only one ``consolidation_loop._consolidate_clusters`` calls, and the only
    one ``coordinator._nrem_cycle_counts`` counts against for its
    `fact_cycles` census, so the fold and its telemetry can never again
    disagree. (project, section) with **no** entity — exactly the plan's
    anchor: "(project, domain), and nothing else."

    Pure. Only **registered** non-empty sections form buckets — an unregistered
    or blank section never qualifies. ``registered_sections`` is a set of
    ``(project_name, section_name)`` pairs. ``_consolidate_clusters`` derives
    it from the SAME graph rows ``_find_grounded_fact_groups`` already proved
    registered (a DOMAIN_OF/PROJECT_OF edge only exists for a registered
    section — coordinator.py's ``_domain_identities`` never writes one
    otherwise), so this is a second, cheap confirmation rather than a second
    source of truth. Fan-out: a fact tagged with several sections counts in
    each bucket, not just one.

    Returns list of ``((project, section), contents, pg_ids)``.
    """
    registered = registered_sections or set()
    buckets: dict = {}
    for content, pid in zip(contents, pg_ids):
        project = project_map.get(pid)
        if not fold_eligible(project):
            continue
        sections = domains_map.get(pid) or []
        for s in sections:
            if not isinstance(s, str):
                continue
            section = s.strip()
            if not section:
                continue
            if (project, section) not in registered:
                continue
            key = (project, section)
            bucket = buckets.setdefault(key, ([], []))
            bucket[0].append(content)
            bucket[1].append(pid)
    return [
        (key, c, p)
        for key, (c, p) in buckets.items()
        if len(p) >= threshold
    ]


def count_domain_level_cycles(pg_ids, project_map, domains_map, threshold,
                              registered_sections):
    """Telemetry twin of ``eligible_domain_level_clusters`` — count only,
    same partitioner, so the gauge and the fold can never again describe
    different populations. Used by ``coordinator._nrem_cycle_counts`` for the
    `fact_cycles` census in ``GET /memory/telemetry``."""
    contents = [""] * len(pg_ids)
    return len(eligible_domain_level_clusters(
        contents, pg_ids, project_map, domains_map, threshold,
        registered_sections))
