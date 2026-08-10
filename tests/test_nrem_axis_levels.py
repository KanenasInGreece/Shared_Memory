"""v2 FACT GATE (Dreaming Cycle Plan to v2, §2.1) pure rules (no DB).

Covers the (project, domain) partition — registered sections only, no entity,
no project-only level (both removed, C1/C1b) — P12 same-level subset
supersession helper behaviour via the partitioner, and decision 1080
evidential kind. Trust the functions, not docstrings — every assertion is
against return values.

⛔ The former entity-level test block (`eligible_entity_level_clusters`,
`count_entity_level_cycles`) is REMOVED with the functions it tested — see
the block comment above `eligible_domain_level_clusters` in
consolidation_loop.py. `tests/test_v2_fact_gate.py` covers the v2 gate's
invariants (I1/I2/I8) directly against the real discovery Cypher.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

from consolidation_loop import (  # noqa: E402
    eligible_domain_level_clusters,
    count_domain_level_cycles,
    evidential_kind_for_record,
    SECTION_NONE,
    LEVEL_ENTITY,
    LEVEL_DOMAIN,
)


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
