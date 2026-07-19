"""
Tests for qualified record references (decision 822).

A record id is unique only WITHIN ITS TABLE: `technical_docs` and
`community_summaries` run independent sequences, so the same integer names two
unrelated real records. Search returns that id under the same field name for
both namespaces, so a bare id lifted off a summary result and handed back to a
lookup used to resolve against technical_docs and return a confident, unrelated
record. These tests pin the qualification that closes it — and pin the
compatibility concession, which is the one place the ambiguity survives.
"""

import importlib.util
import os
import sys

import pytest


def load_coordinator():
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    path = os.path.join(scripts_dir, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator_refs", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["coordinator_refs"] = mod
    spec.loader.exec_module(mod)
    return mod


co = load_coordinator()


# ── parse_ref / make_ref ─────────────────────────────────────────────────────

def test_bare_integer_stays_unqualified_and_means_technical_docs():
    """The compatibility concession, pinned deliberately: a bare id parses to a
    None type, which every caller reads as 'assume technical_docs'. This is the
    ONE surviving ambiguity — if it ever changes, it must change knowingly."""
    assert co.parse_ref("816") == (None, 816)
    assert co.parse_ref("  816  ") == (None, 816)


@pytest.mark.parametrize("raw,expected", [
    ("fact:816",           ("fact", 816)),
    ("decision:822",       ("decision", 822)),
    ("retrospective:813",  ("retrospective", 813)),
    ("summary:87",         ("summary", 87)),
    ("insight:92",         ("insight", 92)),
    ("FACT:816",           ("fact", 816)),      # case-insensitive
])
def test_qualified_refs_parse_to_type_and_id(raw, expected):
    assert co.parse_ref(raw) == expected


@pytest.mark.parametrize("bad", ["banana:1", "fact:", "fact:abc", "", "abc", ":5"])
def test_malformed_refs_raise_rather_than_coerce(bad):
    """A malformed ref must fail loudly. Silently coercing it to a number is
    precisely how a wrong reference becomes a confident wrong answer."""
    with pytest.raises(ValueError):
        co.parse_ref(bad)


def test_make_ref_round_trips_through_parse():
    for rtype in co.REF_TYPES_DOCS + co.REF_TYPES_SUMMARIES:
        assert co.parse_ref(co.make_ref(rtype, 42)) == (rtype, 42)


def test_the_two_namespaces_are_disjoint():
    """The collision only exists BETWEEN these sets. Facts and decisions share
    one table, so they can never collide with each other — which is why the
    seven phantom nodes were fabrication, not collision."""
    assert not set(co.REF_TYPES_DOCS) & set(co.REF_TYPES_SUMMARIES)


# ── record-type derivation ───────────────────────────────────────────────────

@pytest.mark.parametrize("meta,expected", [
    ({"type": "decision"},      "decision"),
    ({"type": "retrospective"}, "retrospective"),
    ({"type": "fact"},          "fact"),
    ({},                        "fact"),
    (None,                      "fact"),
    ({"type": "nonsense"},      "fact"),
])
def test_doc_record_type_collapses_unknowns_to_fact(meta, expected):
    """Must agree with the enrichment daemon's own collapse, or the two
    disagree about what a record IS while sharing its id."""
    assert co.doc_record_type(meta) == expected


@pytest.mark.parametrize("meta,expected", [
    ({"kind": "insight"},  "insight"),
    ({"kind": "thematic"}, "summary"),
    ({},                   "summary"),
    (None,                 "summary"),
])
def test_summary_record_type_distinguishes_insights(meta, expected):
    assert co.summary_record_type(meta) == expected


def test_same_integer_yields_different_refs_in_each_namespace():
    """The whole point: id 16 is a real fact AND a real summary. Qualification
    is what stops those two being the same reference."""
    assert co.make_ref(co.doc_record_type({"type": "fact"}), 16) != \
           co.make_ref(co.summary_record_type({"kind": "thematic"}), 16)
