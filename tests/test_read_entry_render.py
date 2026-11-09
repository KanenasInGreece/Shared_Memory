"""Failure cases for the shared read renderer."""
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared-memory/scripts'))
import coordinator as c


def row(kind='fact', **kw):
    return dict(id=1, type=kind, ref=f'{kind}:1', content='whole record',
                content_chars=12, readable=True, metadata={'source_ref': 'discussion_context'}, **kw)


@pytest.mark.parametrize('summary', ['longer than the whole record', 'x' * 2000, None])
def test_summary_fallback(summary):
    # Kills: an unprocessed fact showing Postgres text as full_record (fact:3544).
    r = c.render_record_entry(row(), {'rem_summary': summary} if summary is not None else None, 'fact:1', {})
    assert 'text' not in r
    assert r['text_is'] == 'pointer'


def test_processed_invalid_summary_is_full_record():
    # Kills: keeping an invalid rem_summary on a processed fact (F1, F2, F3).
    for summary in ('longer than the whole record', None, ''):
        state = {'rem_processed': True, 'rem_summary': summary, 'rem_prompt_version': 2}
        r = c.render_record_entry(row(), state, 'fact:1', {})
        assert r['text'] == 'whole record'
        assert r['text_is'] == 'full_record'


def test_decision_and_pointer():
    # F5, F6, F10: rationale and pointer identities come from Postgres.
    d = row('decision')
    d['metadata'] = {'decision': {'rationale': 'entire rationale'}}
    assert c.render_record_entry(d, 'wrong', 'decision:1', {})['text'] == 'entire rationale'
    d['metadata']['decision']['rationale'] = ' '
    assert c.render_record_entry(d, None, 'decision:1', {})['text'] == 'whole record'
    for kind in ('summary', 'insight'):
        r = c.render_record_entry(row(kind), 'wrong', f'{kind}:1', {})
        assert 'text' not in r and r['text_is'] == 'pointer'
        assert r['full_record']['read_with'] == f'search --ref {kind}:1'


def test_visibility_obsolete_and_provenance():
    # F4, F4b, F16, F28: visibility precedes retirement; provenance is authoritative.
    d = row(superseded=True, successor_ref='fact:2', successor_readable=False)
    assert c.render_record_entry(dict(d, readable=False), 'secret', 'fact:1', {}) is None
    assert c.render_record_entry(None, 'secret', 'fact:1', {}) is None
    r = c.render_record_entry(d, None, 'fact:1', {})
    assert r['obsolete'] == 'superseded' and 'superseded_by' not in r
    assert r['source_ref'] == 'discussion_context' and r['fact_kind'] == 'discussion'
    assert 'adr_props' not in r
    assert c.render_record_entry(d, None, 'fact:1', {}, ranked=True) is None


def test_marks():
    # F12, F13, F30: marks retain roles and choose the highest readable id.
    marks = {'decisions': {1: {'outcomes': [{'pg_id': 3, 'rating': 'reversed'}, {'pg_id': 4, 'rating': 'validated'}],
                              'grounds': [{'pg_id': 2, 'relation': 'INFORMED_BY'}]}},
             'retrospectives': {3: {'decision_id': 1, 'siblings': [3, 4, 5]},
                                 4: {'decision_id': 1, 'siblings': [3, 4, 5]}},
             'rows': {('technical_docs', 2): row(superseded=True),
                      ('technical_docs', 3): row('retrospective'),
                      ('technical_docs', 4): row('retrospective'),
                      ('technical_docs', 5): dict(row('retrospective'), readable=False)}}
    r = c.render_record_entry(row('decision'), None, 'decision:1', marks, search_hit=True)
    assert r['retired_grounds'] == [{'ref': 'fact:2', 'relation': 'INFORMED_BY'}]
    assert r['lifecycle']['ref'] == 'retrospective:4'
    assert c.render_record_entry(row('retrospective'), None, 'retrospective:3', marks)['latest_verdict'] == 'retrospective:4'
    assert 'latest_verdict' not in c.render_record_entry(
        row('retrospective'), None, 'retrospective:4', marks)
    assert c.redact_reference_ids([3, 5], {3}) == [3]


def test_relation():
    # F7, F18: preserve stored direction and remove legacy text from props.
    assert c.render_relation('fact:2', 'GROUNDED_IN', 'decision:1', {'notes': 'secret', 'x': 1}) == {
        'from': 'fact:2', 'relation': 'GROUNDED_IN', 'to': 'decision:1', 'props': {'x': 1}}


def test_legacy_cut_and_whole_summary():
    # Kills: a stamp-less 2,000-character summary counting as summary (brief §0.2-3).
    d = row()
    d['content'] = 'a' * 4001
    d['content_chars'] = 4001
    unstamped = {'rem_processed': True, 'rem_summary': 's' * 2000}
    r = c.render_record_entry(d, unstamped, 'fact:1', {})
    assert r['text_is'] == 'full_record'
    assert r['text'] == d['content']
    stamped = {'rem_processed': True, 'rem_summary': 's' * 2000, 'rem_prompt_version': 2}
    r = c.render_record_entry(d, stamped, 'fact:1', {})
    assert r['text_is'] == 'summary'
    assert r['text'] == 's' * 2000
    longer = {'rem_processed': True, 'rem_summary': 's' * 2001, 'rem_prompt_version': 2}
    r = c.render_record_entry(d, longer, 'fact:1', {})
    assert r['text_is'] == 'summary'
    assert r['text'] == 's' * 2001


@pytest.mark.parametrize('kind', ['fact', 'retrospective', 'decision', 'summary', 'insight'])
def test_retirement_per_type(kind):
    # F4, F4b, F16: Postgres retirement applies to every type after visibility.
    d = row(kind, superseded=True, successor_readable=True, successor_ref='insight:8')
    marks = {'decisions': {1: {'outcomes': [{'pg_id': 9, 'rating': 'reversed'}]}},
             'rows': {('technical_docs', 9): dict(row('retrospective'), readable=True)}}
    r = c.render_record_entry(d, None, f'{kind}:1', marks)
    assert r['obsolete'] == ('reversed' if kind == 'decision' else 'superseded')
    assert r['superseded_by'] == ('retrospective:9' if kind == 'decision' else 'insight:8')
    assert c.render_record_entry(d, None, f'{kind}:1', marks, ranked=True) is None
    assert c.render_record_entry(dict(d, readable=False), None, f'{kind}:1', marks) is None


def test_private_marks_do_not_win():
    # F12, F13, F16: private outcomes, grounds and successors name nothing.
    marks = {'decisions': {1: {'outcomes': [{'pg_id': 8, 'rating': 'reversed'}],
                              'grounds': [{'pg_id': 9, 'relation': 'REJECTED'}]}},
             'retrospectives': {3: {'decision_id': 1, 'siblings': [3, 8]}},
             'rows': {('technical_docs', 8): dict(row('retrospective'), readable=False),
                      ('technical_docs', 9): dict(row(), readable=False)}}
    result = c.render_record_entry(row('decision', superseded=True), None,
                                    'decision:1', marks, search_hit=True)
    assert result['lifecycle'] == {'rating': 'reversed'}
    assert 'superseded_by' not in result and 'retired_grounds' not in result
    assert 'latest_verdict' not in c.render_record_entry(row('retrospective'), None,
                                                        'retrospective:3', marks)


def test_ground_roles_and_live_ground_exclusion():
    # F12, F30: preserve rejected/considered roles and omit live grounds.
    marks = {'decisions': {1: {'grounds': [
        {'pg_id': 4, 'relation': 'REJECTED'}, {'pg_id': 2, 'relation': 'CONSIDERED'},
        {'pg_id': 3, 'relation': 'GROUNDED_IN'}]}},
        'rows': {('technical_docs', 2): row(superseded=True, successor_readable=True,
                                          successor_ref='fact:8'),
                 ('technical_docs', 3): row(superseded=False),
                 ('technical_docs', 4): row(superseded=True, successor_readable=False,
                                          successor_ref='fact:9')}}
    entry = c.render_record_entry(row('decision'), None, 'decision:1', marks, search_hit=True)
    assert entry['retired_grounds'] == [
        {'ref': 'fact:2', 'relation': 'CONSIDERED', 'superseded_by': 'fact:8'},
        {'ref': 'fact:4', 'relation': 'REJECTED'}]
    assert 'adr_props' not in entry


def test_structured_reference_redaction():
    # F4: redaction preserves readable structured references and omits private ids.
    ids = [{'pg_id': 1, 'role': 'based_on'}, {'pg_id': 2, 'role': 'informed_by'}]
    assert c.redact_reference_ids(ids, lambda item: item['pg_id'] == 1) == ids[:1]


@pytest.mark.parametrize('kind', ['fact', 'decision', 'retrospective', 'summary', 'insight'])
def test_commands_are_built_from_authoritative_refs(kind):
    # F10: every type uses its qualified ref, including decisions and retrospectives.
    entry = c.render_record_entry(row(kind), None, f'{kind}:1', {})
    assert entry['full_record'] == {'ref': f'{kind}:1', 'chars': 12,
                                    'read_with': f'search --ref {kind}:1'}


@pytest.mark.parametrize('kind', ['fact', 'retrospective'])
def test_save_shaped_provenance(kind):
    # F28: a normal save stores source_ref alone, so fact_kind must be derived.
    entry = c.render_record_entry(row(kind), None, f'{kind}:1', {})
    assert entry['source_ref'] == 'discussion_context'
    assert entry['fact_kind'] == 'discussion'
    assert 'adr_props' not in entry


@pytest.mark.parametrize('metadata', [{'decision': 'free text'}, {'decision': ['x']}, ['x'], 'plain'])
def test_bad_metadata(metadata):
    """F6: malformed metadata falls back to the whole record."""
    assert c.render_record_entry(dict(row('decision'), metadata=metadata), None,
                                 'decision:1', {})['text'] == 'whole record'


@pytest.mark.parametrize('rating', ['refined', 'mixed', 'reversed', 'validated'])
def test_lifecycle_whole_text(rating):
    """F13: lifecycle text is whole and validated has none."""
    marks = {'decisions': {1: {'outcomes': [{'pg_id': 3, 'rating': rating}]}},
             'rows': {('technical_docs', 3): row('retrospective')}}
    entry = c.render_record_entry(row('decision'), None, 'decision:1', marks, search_hit=True)
    if rating == 'validated':
        assert 'text' not in entry['lifecycle']
    else:
        assert entry['lifecycle']['text'] == 'whole record'
    explicit = c.render_record_entry(row('decision'), None, 'decision:1', marks)
    assert 'lifecycle' not in explicit and 'retired_grounds' not in explicit


@pytest.mark.parametrize('superseded', [True, False])
def test_failed_marks_and_unrated_outcome(superseded):
    """F31: failed marks stay absent and the latest unrated outcome has no lifecycle."""
    assert 'lifecycle' not in c.render_record_entry(row('decision', superseded=True),
        None, 'decision:1', {'failed': True}, search_hit=True)
    marks = {'decisions': {1: {'outcomes': [{'pg_id': 3, 'rating': 'reversed'},
        {'pg_id': 5, 'rating': 'reversed'}, {'pg_id': 6, 'rating': None}]}},
        'rows': {('technical_docs', i): row('retrospective') for i in [3, 5, 6]}}
    if not superseded:
        marks['decisions'][1]['outcomes'] = [
            {'pg_id': 5, 'rating': 'validated'}, {'pg_id': 6, 'rating': None}]
    entry = c.render_record_entry(row('decision', superseded=superseded), None,
        'decision:1', marks, search_hit=True)
    if superseded:
        assert entry['superseded_by'] == 'retrospective:5'
    assert 'lifecycle' not in entry
    marks['retrospectives'] = {5: {'decision_id': 1, 'siblings': [3, 5, 6]}}
    assert c.render_record_entry(row('retrospective'), None, 'retrospective:5',
        marks)['latest_verdict'] == 'retrospective:6'


def test_empty_record_is_whole():
    """Kills: an unprocessed empty fact showing as full_record (fact:3544)."""
    entry = c.render_record_entry(dict(row(), content='', content_chars=0), None, 'fact:1', {})
    assert 'text' not in entry and entry['text_is'] == 'pointer'
    assert entry['full_record']['chars'] == 0
    processed = c.render_record_entry(
        dict(row(), content='', content_chars=0),
        {'rem_processed': True, 'rem_prompt_version': 2}, 'fact:1', {})
    assert processed['text'] == '' and processed['text_is'] == 'full_record'
