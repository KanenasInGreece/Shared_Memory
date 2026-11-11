"""Canonical citations: adversarial failures from release-3 brief section 5."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared-memory/scripts'))
import reference_resolver as rr
from record_ref import ID_MAX


def test_canonical_boundaries_order_and_duplicates():
    # Section 5: case, word boundary, digit cut, Unicode digit, canonicalisation, duplicates, order.
    text = 'Fact:12 prefact:12 _fact:5 fact:12٣ summary:4 fact:0012 fact:123abc decision:3 fact:12'
    assert rr.extract_citations(text) == [('fact', 12), ('fact', 123), ('decision', 3)]


def test_invalid_numbers_never_raise_or_survive():
    # Section 5: zero, length-before-int, range drop, non-string content.
    text = f'fact:0 fact:{"9" * 4301} fact:{"9" * 20} fact:{ID_MAX + 1} fact:{ID_MAX} fact:{"0" * 4301}7'
    assert rr.extract_citations(text) == [('fact', ID_MAX), ('fact', 7)]
    for value in (None, 12, [], ''):
        assert rr.extract_citations(value) == []


def test_resolution_checks_kind_missing_and_self():
    # Section 5: wrong kind, missing id, self kept; superseded targets must remain eligible.
    citations = rr.extract_citations('fact:12 decision:13 fact:14 fact:15 retrospective:16')
    kinds = {12: {}, 13: {}, 15: {'superseded': True}, 16: {'type': 'retrospective'}}
    assert rr.references_from(citations, kinds, 12) == ['fact:15', 'retrospective:16']


import pytest


@pytest.mark.parametrize('text', ['Fact:12', 'prefact:12', '_fact:5', 'fact:12٣', 'summary:4'])
def test_each_invalid_lexeme_is_independently_absent(text):
    # Section 5: case/word-boundary/Unicode/type failures must not be masked by later duplicates.
    assert rr.extract_citations(text) == []


@pytest.mark.parametrize('text,expected', [('fact:123', [('fact', 123)]),
                                          ('fact:0012', [('fact', 12)]),
                                          ('fact:12abc', [('fact', 12)])])
def test_each_valid_lexeme_is_independently_whole(text, expected):
    # Section 5: digit cut and leading-zero canonicalisation.
    assert rr.extract_citations(text) == expected
