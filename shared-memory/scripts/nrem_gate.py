"""THE v2 FACT GATE PARTITIONER — pure, no DB driver, no I/O (fix wave, 2026-08).

Extracted out of ``consolidation_loop.py`` so it can be imported by anything
that only needs to *count* what the fold would do, without pulling in the
whole daemon module. ``consolidation_loop.py`` imports ``psycopg2`` at module
level (for its own synchronous DB work) — importing it just to reach these
two pure functions means executing that whole module's top-level code
(psycopg2 import included) inside a caller that may not carry that
dependency. `coordinator.py`'s ``_nrem_cycle_counts`` (the `GET
/memory/telemetry` → `telemetry.nrem` gauge) is exactly that caller: the
shipped gateway service (``shared-memory/ops/hive-mind-gateway.service``)
runs with `--with aiohttp --with asyncpg --with neo4j --with httpx --with
json-repair` and never carries psycopg2, so a lazy
``from consolidation_loop import count_domain_level_cycles`` inside that
method raised ``ModuleNotFoundError: No module named 'psycopg2'`` on every
call — caught and rendered as ``{"error": ...}`` rather than crashing, so the
gauge failed silently in production while 1236 unit tests (all DB access
stubbed) stayed green. See CLAUDE.md's Group 3 note: daemon/observability has
no mechanical test tie, and a green suite proves nothing about a path no test
exercises under the real dependency set.

``project_axis`` (``fold_eligible``) and ``ontology`` are this module's only
imports besides stdlib — both stdlib-only themselves (verified: neither
imports psycopg2, asyncpg, neo4j, or httpx). Keep it that way: adding any DB
driver or network client import here reintroduces exactly the defect this
module exists to remove. ``test_nrem_gate_import_purity.py`` enforces it.

``consolidation_loop.py`` re-exports both names (``from nrem_gate import
eligible_domain_level_clusters, count_domain_level_cycles``) so its own fold
code and every existing test/caller of the old location keep working
unchanged — this is a location split, not a rename or a behaviour change.
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
