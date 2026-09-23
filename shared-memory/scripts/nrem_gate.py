"""Pure (project, domain) fold partitioner without DB drivers for telemetry import.

Imports must stay stdlib-only (besides project_axis and ontology), enforced by
test_nrem_gate_import_purity.py.
"""

from project_axis import fold_eligible


def eligible_domain_level_clusters(contents, pg_ids, project_map, domains_map,
                                   threshold, registered_sections):
    """Partition facts into ``(project, section)`` buckets for registered sections,
    fanning out multi-section facts. Shared by ``_consolidate_clusters`` and
    ``_nrem_cycle_counts`` so fold processing and telemetry census match.

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
    """Count-only wrapper around ``eligible_domain_level_clusters`` used by
    ``coordinator._nrem_cycle_counts`` for the `fact_cycles` census in
    ``GET /memory/telemetry``.
    """
    contents = [""] * len(pg_ids)
    return len(eligible_domain_level_clusters(
        contents, pg_ids, project_map, domains_map, threshold,
        registered_sections))
