"""Stage 1.2b reference resolution — pure extraction + deterministic classification.

The LLM judge is config-gated I/O (mode=llm) and not exercised here; the resolver
defaults to deterministic. Live motivation: 145/146 context-gated numeric refs in
the corpus resolved to real records (e.g. "refines decision 381").
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
from ontology import ONT  # noqa: E402
import reference_resolver as rr  # noqa: E402

VALID = {120, 173, 214, 257, 276, 381}


def _ids(content, src):
    return [r[0] for r in rr.extract_references(content, src, VALID)]


def test_cue_gated_numbers_resolve():
    assert _ids("This refines decision 381 substantially.", 400) == [381]
    assert _ids("Addendum to pg_id 257 (monitor).", 400) == [257]
    assert _ids("See retrospective on decision 214.", 400) == [214]


def test_bare_numbers_ignored():
    # No record-reference cue → not a reference (avoid '256 MB', '8070', years).
    assert _ids("Allocate 256 MB and bind port 8070 in 2026.", 400) == []


def test_unresolvable_id_dropped():
    # Cue present but id not a real record.
    assert _ids("refines decision 999", 400) == []


def test_self_reference_skipped():
    assert _ids("This decision 276 supersedes nothing.", 276) == []


def test_dedup_by_referenced_id():
    out = _ids("decision 276 ... again decision 276 ... pg_id 276", 400)
    assert out == [276]


def test_snippet_and_cue_captured():
    refs = rr.extract_references("...prior work, refines decision 381 here", 400, VALID)
    ref_id, cue, snippet = refs[0]
    assert ref_id == 381
    assert "381" in cue and "decision" in cue.lower()
    assert "381" in snippet


def test_non_string_content():
    assert rr.extract_references(None, 1, VALID) == []
    assert rr.extract_references(42, 1, VALID) == []


def test_deterministic_relation_decision_to_decision():
    assert rr.deterministic_relation(ONT.decision, ONT.decision) == ONT.informed_by


def test_deterministic_relation_other_pairings_reference():
    assert rr.deterministic_relation(ONT.fact, ONT.decision) == ONT.references
    assert rr.deterministic_relation(ONT.decision, ONT.fact) == ONT.references
    assert rr.deterministic_relation(ONT.fact, ONT.fact) == ONT.references


def test_classify_defaults_to_deterministic_when_judge_disabled():
    # Default mode is deterministic — no network call, returns the label-based rule.
    assert rr.classify_relation(ONT.decision, ONT.decision, "addendum to 257") == ONT.informed_by
    assert rr.classify_relation(ONT.fact, ONT.decision, "see 276") == ONT.references


def test_references_in_known_relationships():
    from ontology import KNOWN_RELATIONSHIPS
    assert ONT.references in KNOWN_RELATIONSHIPS
    assert ONT.informed_by in KNOWN_RELATIONSHIPS
