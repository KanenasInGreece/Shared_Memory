"""fact_kind — soft epistemic tag DERIVED from source_ref (decision 552 + the
fact-overload discussion). Pure, deterministic; no infra."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
from ontology import fact_kind_from_source_ref as fk, DISCUSSION_CONTEXT  # noqa: E402


def test_observation_when_no_source():
    assert fk(None) == "observation"
    assert fk("") == "observation"
    assert fk("   ") == "observation"
    assert fk(123) == "observation"


def test_discussion_sentinel():
    assert DISCUSSION_CONTEXT == "discussion_context"
    assert fk(DISCUSSION_CONTEXT) == "discussion"
    assert fk("discussion_context") == "discussion"


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
