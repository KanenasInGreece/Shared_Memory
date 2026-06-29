"""Phase 2 schema-compliance telemetry — pure split logic.

`MemoryCoordinator._compliance_split` partitions a {name: count} distribution
against the ontology vocabulary (KNOWN_LABELS / KNOWN_RELATIONSHIPS). The Neo4j
aggregation around it needs infra; the rule itself is pure and locked here.
Live motivation: foreign labels (DockerContainer, Conversation) and relationship
types (REQUIRES, WRITES_TO) from pre-gate experiments must be flagged.
"""
import importlib.util
import os
import sys

scripts_dir = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
)
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)


def _load_coordinator():
    path = os.path.join(scripts_dir, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["coordinator"] = mod
    spec.loader.exec_module(mod)
    return mod


coordinator_mod = _load_coordinator()
MemoryCoordinator = coordinator_mod.MemoryCoordinator
from ontology import KNOWN_LABELS, KNOWN_RELATIONSHIPS  # noqa: E402

split = MemoryCoordinator._compliance_split


def test_all_known_is_ok():
    counts = {"MENTIONS": 100, "PRODUCES_INSIGHT": 10, "HAD_OUTCOME": 5}
    status, invalid = split(counts, KNOWN_RELATIONSHIPS)
    assert status == "ok"
    assert invalid == []


def test_foreign_relationships_flagged():
    counts = {"MENTIONS": 100, "REQUIRES": 4, "WRITES_TO": 2, "RELATED_TO": 3}
    status, invalid = split(counts, KNOWN_RELATIONSHIPS)
    assert status == "non-compliant"
    # sorted by count desc, then name
    assert invalid == [
        {"name": "REQUIRES", "count": 4},
        {"name": "RELATED_TO", "count": 3},
        {"name": "WRITES_TO", "count": 2},
    ]


def test_foreign_labels_flagged():
    counts = {"Entity": 1700, "Decision": 150, "DockerContainer": 4, "Conversation": 5}
    status, invalid = split(counts, KNOWN_LABELS)
    assert status == "non-compliant"
    assert invalid == [
        {"name": "Conversation", "count": 5},
        {"name": "DockerContainer", "count": 4},
    ]


def test_known_labels_are_clean():
    # A graph using only ontology labels must report ok.
    counts = {lbl: 1 for lbl in KNOWN_LABELS}
    status, invalid = split(counts, KNOWN_LABELS)
    assert status == "ok"
    assert invalid == []


def test_empty_distribution_is_ok():
    status, invalid = split({}, KNOWN_RELATIONSHIPS)
    assert status == "ok"
    assert invalid == []


def test_tie_break_is_name_alphabetical():
    counts = {"ZED": 2, "ABLE": 2, "MID": 2}
    _, invalid = split(counts, frozenset())
    assert [d["name"] for d in invalid] == ["ABLE", "MID", "ZED"]
