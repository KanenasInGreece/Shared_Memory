"""Trust the measurement before trusting the results — unit tests for the
Stage 1.3 eval harness's pure metric functions (decision 475)."""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "eval"))
import typing_eval as te  # noqa: E402

L = ["A", "B", "C"]


def test_macro_f1_perfect():
    pairs = [("A", "A"), ("B", "B"), ("C", "C")]
    assert te.macro_f1(pairs, L) == 1.0
    assert te.accuracy(pairs) == 1.0


def test_macro_f1_ignores_absent_classes():
    # Only A and B present in gold; C has no support and must not drag macro down.
    pairs = [("A", "A"), ("B", "B")]
    assert te.macro_f1(pairs, L) == 1.0


def test_macro_f1_penalises_minority_miss():
    # 3 A correct, the single B misclassified → macro-F1 well below accuracy.
    pairs = [("A", "A"), ("A", "A"), ("A", "A"), ("B", "A")]
    assert te.accuracy(pairs) == 0.75
    # B recall 0 → B f1 0; A precision 3/4, recall 1 → f1 0.857; macro = (0.857+0)/2
    assert abs(te.macro_f1(pairs, L) - 0.42857) < 1e-3


def test_balanced_accuracy_is_mean_recall():
    pairs = [("A", "A"), ("A", "B"), ("B", "B")]  # A recall .5, B recall 1
    assert abs(te.balanced_accuracy(pairs, L) - 0.75) < 1e-9


def test_weighted_f1_weights_by_support():
    pairs = [("A", "A"), ("A", "A"), ("A", "A"), ("B", "C")]
    # A dominates support → weighted F1 stays high despite B miss
    assert te.weighted_f1(pairs, L) > te.macro_f1(pairs, L)


def test_entropy_collapse_is_low():
    assert te.entropy(["A", "A", "A", "A"]) == 0.0           # all one label
    assert abs(te.entropy(["A", "B"]) - 1.0) < 1e-9          # 2 equal → 1 bit


def test_coverage_and_wrong_rate():
    preds = ["A", "OTHER", "B", "OTHER"]
    assert te.coverage(preds, "OTHER") == 0.5
    pairs = [("A", "A"), ("A", "OTHER"), ("B", "C")]  # last is confident-but-wrong
    assert abs(te.wrong_rate(pairs, "OTHER") - (1 / 3)) < 1e-9


def test_consistency_modal_agreement():
    runs = [["A", "B"], ["A", "C"], ["A", "B"]]  # item0 all A (1.0); item1 B,C,B→2/3
    assert abs(te.consistency(runs) - ((1.0 + 2 / 3) / 2)) < 1e-9
    assert te.consistency([["A"]]) == 1.0  # single run


def test_parse_label_strict_single_token():
    assert te.parse_label("Component", te.ENTITY_TYPES) == "Component"
    assert te.parse_label("the answer is SYSTEM.", te.ENTITY_TYPES) == "System"
    # nothing recognisable → first vocab entry is the safe default for entities
    assert te.parse_label("banana", te.ENTITY_TYPES) == "Component"


def test_baseline_entity_type_rules():
    assert te.baseline_entity_type("rem_loop.py") == "Component"
    assert te.baseline_entity_type("Neo4j") == "System"
    assert te.baseline_entity_type("BGE-M3") == "Model"
    assert te.baseline_entity_type("README") == "Document"
    assert te.baseline_entity_type("OutboxPattern") == "Concept"
    assert te.baseline_entity_type("Xenofon") == "OTHER"


def test_baseline_relation_respects_gate():
    assert te.baseline_relation("Component", "System") == "DEPENDS_ON"
    assert te.baseline_relation("Component", "Concept") == "IMPLEMENTS"
    assert te.baseline_relation("Document", "Component") == "DESCRIBES"
    assert te.baseline_relation("OTHER", "Component") == "NONE"
