"""Plan PR 7 — multi-level NREM fold pure rules (no DB).

Covers entity-level (project, section) partition, domain-level (registered
sections only), P12 same-level subset supersession helper behaviour via the
partitioners, and decision 1080 evidential kind. Trust the functions, not
docstrings — every assertion is against return values.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

from consolidation_loop import (  # noqa: E402
    eligible_entity_level_clusters,
    eligible_domain_level_clusters,
    count_entity_level_cycles,
    count_domain_level_cycles,
    evidential_kind_for_record,
    SECTION_NONE,
    LEVEL_ENTITY,
    LEVEL_DOMAIN,
)


# ── Entity-level (project, section) ──────────────────────────────────────────

def test_entity_level_groups_by_project_and_section():
    contents = ["a", "b", "c", "d"]
    pg_ids = [1, 2, 3, 4]
    project_map = {1: "smg", 2: "smg", 3: "smg", 4: "other"}
    domains_map = {1: ["architecture"], 2: ["architecture"],
                   3: ["operations"], 4: ["architecture"]}
    result = eligible_entity_level_clusters(
        contents, pg_ids, project_map, domains_map, threshold=2)
    keys = {k for k, _c, _p in result}
    assert ("smg", "architecture") in keys
    assert ("smg", "operations") not in keys  # only 1
    assert ("other", "architecture") not in keys


def test_p15_domainless_facts_still_fold_on_project():
    contents = ["a", "b", "c"]
    pg_ids = [1, 2, 3]
    project_map = {1: "smg", 2: "smg", 3: "smg"}
    domains_map = {1: [], 2: [], 3: ["architecture"]}
    result = eligible_entity_level_clusters(
        contents, pg_ids, project_map, domains_map, threshold=2)
    by_key = {k: p for k, _c, p in result}
    assert by_key[("smg", SECTION_NONE)] == [1, 2]
    # single architecture fact below threshold
    assert ("smg", "architecture") not in by_key


def test_multi_domain_fanout_counts_fact_in_each_section():
    contents = ["shared"]
    pg_ids = [1]
    project_map = {1: "smg"}
    domains_map = {1: ["architecture", "operations"]}
    # threshold 1 so both buckets form
    result = eligible_entity_level_clusters(
        contents, pg_ids, project_map, domains_map, threshold=1)
    keys = {k for k, _c, _p in result}
    assert keys == {("smg", "architecture"), ("smg", "operations")}


def test_p2_unresolvable_project_skipped():
    contents = ["a", "b"]
    pg_ids = [1, 2]
    project_map = {1: None, 2: ""}
    domains_map = {1: ["x"], 2: ["x"]}
    assert eligible_entity_level_clusters(
        contents, pg_ids, project_map, domains_map, threshold=1) == []


def test_count_entity_level_matches_partitioner():
    pg_ids = [1, 2, 3, 4]
    project_map = {1: "smg", 2: "smg", 3: "smg", 4: "smg"}
    domains_map = {1: ["a"], 2: ["a"], 3: ["b"], 4: ["b"]}
    n = count_entity_level_cycles(pg_ids, project_map, domains_map, threshold=2)
    items = eligible_entity_level_clusters(
        [""] * 4, pg_ids, project_map, domains_map, threshold=2)
    assert n == len(items) == 2


# ── Domain-level (no entity; registered sections only) ───────────────────────

def test_domain_level_folds_across_entities_without_entity_key():
    """Facts thin per entity but dense in one registered section."""
    contents = ["e1a", "e1b", "e2a", "e2b", "e3a"]
    pg_ids = [1, 2, 3, 4, 5]
    project_map = {i: "smg" for i in pg_ids}
    domains_map = {i: ["architecture"] for i in pg_ids}
    registered = {("smg", "architecture")}
    result = eligible_domain_level_clusters(
        contents, pg_ids, project_map, domains_map,
        threshold=5, registered_sections=registered)
    assert len(result) == 1
    (project, section), c, p = result[0]
    assert project == "smg" and section == "architecture"
    assert p == [1, 2, 3, 4, 5]


def test_p16_unregistered_section_never_forms_domain_level():
    contents = ["a", "b", "c", "d", "e"]
    pg_ids = [1, 2, 3, 4, 5]
    project_map = {i: "smg" for i in pg_ids}
    domains_map = {i: ["not-registered"] for i in pg_ids}
    result = eligible_domain_level_clusters(
        contents, pg_ids, project_map, domains_map,
        threshold=3, registered_sections={("smg", "architecture")})
    assert result == []


def test_p16_blank_section_never_forms_domain_level():
    contents = ["a", "b", "c"]
    pg_ids = [1, 2, 3]
    project_map = {i: "smg" for i in pg_ids}
    domains_map = {i: [] for i in pg_ids}
    result = eligible_domain_level_clusters(
        contents, pg_ids, project_map, domains_map,
        threshold=2, registered_sections={("smg", "architecture")})
    assert result == []


def test_count_domain_level_matches_partitioner():
    pg_ids = [1, 2, 3]
    project_map = {i: "smg" for i in pg_ids}
    domains_map = {i: ["architecture"] for i in pg_ids}
    reg = {("smg", "architecture")}
    n = count_domain_level_cycles(pg_ids, project_map, domains_map, 2, reg)
    items = eligible_domain_level_clusters(
        [""] * 3, pg_ids, project_map, domains_map, 2, reg)
    assert n == len(items) == 1


# ── Decision 1080 ────────────────────────────────────────────────────────────

def test_1080_fact_kind_from_own_source_ref():
    assert evidential_kind_for_record(
        "fact", "shared-memory/scripts/coordinator.py") == "measured"
    assert evidential_kind_for_record("fact", "tests/test_x.py") == "tested"


def test_1080_judgement_ignores_own_source_ref_uses_grounding():
    # Instrument citation would look like measured; grounding is tested.
    assert evidential_kind_for_record(
        "retrospective",
        "shared-memory/scripts/consolidation_loop.py",
        grounding_kinds=["discussion", "tested"],
    ) == "tested"
    assert evidential_kind_for_record(
        "decision",
        "tests/test_x.py",
        grounding_kinds=["researched"],
    ) == "researched"


def test_1080_judgement_with_no_grounding_is_discussion_floor():
    assert evidential_kind_for_record(
        "retrospective", "tests/test_x.py", grounding_kinds=None
    ) == "discussion"
    assert evidential_kind_for_record(
        "decision", "tests/test_x.py", grounding_kinds=[]
    ) == "discussion"


def test_level_constants_stable():
    assert LEVEL_ENTITY == "entity"
    assert LEVEL_DOMAIN == "domain"
    assert SECTION_NONE == ""
