"""fact_kind — soft epistemic tag DERIVED from source_ref (decision 552 + the
fact-overload discussion). Pure, deterministic; no infra."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
from ontology import (  # noqa: E402
    fact_kind_from_source_ref as fk,
    origin_location as ol,
    DISCUSSION_CONTEXT,
    OBSERVATION_CONTEXT,
    LIVE_PREFIX,
)


def test_discussion_is_the_floor_when_no_source():
    """Every fact comes out of a conversation — that is the base case, not a
    degenerate one. An unmarked fact is `discussion`, which the advisory gate
    grounds softly (INFORMED_BY): an unqualified claim must not enter synthesis
    weighted as evidence."""
    assert fk(None) == "discussion"
    assert fk("") == "discussion"
    assert fk("   ") == "discussion"
    assert fk(123) == "discussion"


def test_discussion_sentinel():
    assert DISCUSSION_CONTEXT == "discussion_context"
    assert fk(DISCUSSION_CONTEXT) == "discussion"
    assert fk("discussion_context") == "discussion"


def test_observation_is_a_qualifier_not_a_default():
    """`observation` now means 'a conclusion reasoned out in the discussion' and
    must be asserted explicitly — it is never what an unmarked fact falls to."""
    assert OBSERVATION_CONTEXT == "observation_context"
    assert fk(OBSERVATION_CONTEXT) == "observation"
    assert ol(OBSERVATION_CONTEXT) == ""      # nothing external to cite


def test_live_system_readings_are_tested():
    """A reading off the RUNNING system is empirical, not code-derived: a graph
    census, /health, the journal. Without this it fell to the floor and a
    measurement of thousands of live nodes weighed the same as a remark."""
    assert fk(LIVE_PREFIX + "neo4j/entity-census") == "tested"
    assert fk("live:health") == "tested"
    assert fk("neo4j://agent_data") == "tested"
    assert fk("postgresql://localhost/agent_data") == "tested"
    # the locus cites what was read, without the marker
    assert ol("live:neo4j/entity-census") == "neo4j/entity-census"


def test_test_path_matches_a_component_not_a_substring():
    """Evidence weight must never inflate by accident. A substring check
    promoted any path containing 'latest'/'greatest' to `tested`, the highest
    weight, which the insight prompt treats as outranking discussion."""
    assert fk("scripts/latest_run.py") == "measured"
    assert fk("notes/greatest_hits.md") == "researched"
    assert fk("docs/attestation.md") == "researched"
    # genuine test paths still resolve, on any component separator
    assert fk("src/test/helpers.py") == "tested"
    assert fk("a_test.py") == "tested"
    assert fk("tests/test_coordinator.py") == "tested"


def test_researched_url():
    assert fk("https://arxiv.org/abs/2401.00001") == "researched"
    assert fk("http://example.com/paper") == "researched"


def test_tested_from_test_path():
    assert fk("tests/test_vector_skill.py") == "tested"
    assert fk("tests/test_vector_skill.py::test_case") == "tested"


def test_measured_from_code():
    assert fk("shared-memory/scripts/coordinator.py#L1061") == "measured"
    assert fk("ontology.py") == "measured"
    assert fk("ontology.yaml") == "measured"


def test_researched_from_other_docs():
    assert fk("research/2026-07-11-spine-vs-configurable-ontology-discussion.html") == "researched"
    assert fk("design-doc.pdf#p12") == "researched"
    assert fk("CHANGELOG.md") == "researched"


# ── origin_location — the citable ORIGIN locus (decision 916) ──────────────────

def test_origin_location_empty_when_no_citable_locus():
    # A bare observation has no origin; a discussion's locus is the conversation
    # itself (already conveyed by kind='discussion'), so no location either.
    assert ol(None) == ""
    assert ol("") == ""
    assert ol(123) == ""
    assert ol(DISCUSSION_CONTEXT) == ""


def test_origin_location_url_returns_domain():
    assert ol("https://arxiv.org/abs/2401.00001") == "arxiv.org"
    assert ol("http://example.com/a/b?c=d") == "example.com"


def test_origin_location_path_strips_subdocument_locator():
    # The path is the locus; the #Lnn / @time sub-locator is stripped.
    assert ol("shared-memory/scripts/coordinator.py#L1061") == "shared-memory/scripts/coordinator.py"
    assert ol("tests/test_vector_skill.py::test_case") == "tests/test_vector_skill.py::test_case"
    assert ol("ontology.py") == "ontology.py"
    assert ol("lecture.mp4@00:04") == "lecture.mp4"


def test_origin_location_agrees_with_fact_kind_on_who_has_a_locus():
    # Exactly the kinds that are neither observation nor discussion carry a locus.
    for ref in ("ontology.py", "tests/test_x.py", "https://arxiv.org/x"):
        assert ol(ref) != "" and fk(ref) in ("measured", "tested", "researched")
    for ref in (None, "", DISCUSSION_CONTEXT):
        assert ol(ref) == "" and fk(ref) in ("observation", "discussion")
