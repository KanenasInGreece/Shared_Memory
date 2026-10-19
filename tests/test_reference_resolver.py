"""Reference extraction — pure, no I/O.

There is no judge and no relation choice; the backfill writes every citation as REFERENCES. Live
motivation: 145/146 context-gated numeric refs in the corpus resolved to real
records (e.g. "refines decision 381").
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


def test_references_in_known_relationships():
    from ontology import KNOWN_RELATIONSHIPS
    assert ONT.references in KNOWN_RELATIONSHIPS
    assert ONT.informed_by in KNOWN_RELATIONSHIPS


# ── cue anchoring and whole numbers (clean-up 2026-10-18) ────────────────────

def test_a_cue_inside_a_longer_word_is_not_a_cue():
    """'artifact' contains 'fact' and 'indecision' contains 'decision'; neither cites a record."""
    for text in ("The artifact 257 was measured.", "an indecision 381 moment",
                 "prefact 381", "unrelated 214 results", "correlated 381 rows"):
        assert _ids(text, 400) == [], text


def test_a_cue_glued_to_its_number_is_not_a_reference():
    assert _ids("decision381", 400) == []


def test_a_longer_number_is_never_cut_to_a_valid_prefix():
    valid = VALID | {3125, 10000}
    ids = lambda text: [r[0] for r in rr.extract_references(text, 400, valid)]
    assert ids("refines decision 31250") == []
    assert ids("refines decision 10000") == [10000]
    assert [r[0] for r in rr.extract_references("decision 381\u0662", 400, VALID)] == []


def test_colon_and_hash_separators_still_resolve():
    """A lock on the separators the new pattern keeps; it passes before and after."""
    for text in ("decision:381", "decision #381", "decision: #381"):
        assert _ids(text, 400) == [381], text


def test_non_ascii_digits_are_not_an_id():
    """ASCII-only by choice, the same rule record_ref.parse_ref applies to a ref."""
    assert _ids("refines decision \u0663\u0668\u0661", 400) == []


def test_a_one_digit_id_resolves_when_it_is_a_record():
    """No digit floor: the valid-id check, not a length, decides (decision:3101)."""
    valid = VALID | {7}
    assert [r[0] for r in rr.extract_references("refines decision 7", 400, valid)] == [7]
    assert _ids("decision 1 of 3", 400) == []
