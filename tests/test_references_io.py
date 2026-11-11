"""Release-3 save and read isolation failures from brief section 5."""
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared-memory/scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator as c
from test_resave_axis_conflict import _coord, _save


@pytest.mark.asyncio
@pytest.mark.parametrize('prior', [None, {'id': 99, 'metadata': {'project': 'shared-memory'}}])
async def test_save_overwrites_client_and_removes_returned_self(prior):
    # Section 5: client survives, superseded target dropped, re-save/concurrent self kept, invalid fails save.
    coord, conn = _coord()
    async def row(sql, *args):
        if 'FOR UPDATE' in sql:
            return prior
        return {'id': 99}
    conn.fetchrow = AsyncMock(side_effect=row)
    conn.fetch = AsyncMock(return_value=[{'id': 12, 'metadata': {}}, {'id': 99, 'metadata': {'project': 'shared-memory'}}])
    response, _ = await _save(coord, {'source': 'test', 'project': 'shared-memory', 'references': ['fact:123'], 'entities': []},
                              'fact:12 fact:99 fact:0 fact:' + '9' * 4301)
    assert response.status == 200, response.text
    inserts = [call for call in conn.fetchrow.call_args_list if 'INSERT INTO technical_docs' in call.args[0]]
    stored = inserts[0].args[2]
    assert stored['references'] == ['fact:12']
    if prior is None:
        corrections = [call for call in conn.execute.call_args_list if 'jsonb_set' in call.args[0]]
        assert corrections and corrections[0].args[1:] == (99, ['fact:12'])
    assert 'references' not in json.loads(response.text)


@pytest.mark.asyncio
async def test_save_always_stores_empty_list():
    # Section 5: key absent when empty; client value survives.
    coord, conn = _coord()
    conn.fetchrow = AsyncMock(side_effect=lambda sql, *args: None if 'FOR UPDATE' in sql else {'id': 99})
    response, _ = await _save(coord, {'source': 'test', 'project': 'shared-memory', 'references': ['fact:12'], 'entities': []}, 'no citations')
    assert response.status == 200, response.text
    insert = next(call for call in conn.fetchrow.call_args_list if 'INSERT INTO technical_docs' in call.args[0])
    assert insert.args[2]['references'] == []


@pytest.mark.parametrize('value', [None, 12, {}, 'fact:12'])
def test_non_list_removed(value):
    # Section 5: a non-list passes.
    hit = {'metadata': {'references': value}}
    c._redact_hit_reference_fields(hit, {}, {12: 'fact'})
    assert 'references' not in hit['metadata']


def test_citation_shape_range_kind_and_failure_redaction():
    # Section 5/r3: invalid entries bind SQL, wrong kind passes, failed lookup erases unrelated fields.
    entries = ['fact:12', 12, '12', 'summary:5', 'Fact:3', 'fact:9999999999999999999',
               'decision:13', 'fact:14', 'fact:12\n']
    hit = {'metadata': {'references': entries, 'other': 'kept', 'grounded_in': [12]}}
    ids, citations = set(), set()
    c._collect_hit_reference_ids(hit, ids, citations)
    assert citations == {12, 13, 14}
    assert ('technical_docs', 13) not in ids
    rows = {('technical_docs', 12): {'readable': True}}
    c._redact_hit_reference_fields(hit, rows, {12: 'fact', 13: 'fact'})
    assert hit['metadata']['references'] == ['fact:12']
    hit['metadata']['references'] = entries
    c._redact_hit_reference_fields(hit, rows, None)
    assert hit['metadata'] == {'other': 'kept', 'grounded_in': [12]}
    hit['metadata']['references'] = ['fact:12']
    c._redact_hit_reference_fields(hit, None, {12: 'fact'})
    assert 'references' not in hit['metadata']


@pytest.mark.asyncio
@pytest.mark.parametrize('viewer,scope,expected', [('owner', None, {12: 'fact'}),
                                                  ('other', None, {}),
                                                  ('other', 'shared-memory', {13: 'decision'})])
async def test_citation_lookup_uses_visibility_without_content(viewer, scope, expected):
    # Section 5/r3: private id reaches non-owner, owner loses it, wrong scope, full-content hydration.
    coord, conn = _coord()
    conn.fetch = AsyncMock(return_value=[
        {'id': 12, 'type': None, 'visibility': 'private', 'agent_id': 'owner', 'scope': 'global'},
        {'id': 13, 'type': 'decision', 'visibility': 'scope', 'agent_id': 'owner', 'scope': 'shared-memory'}])
    result = await coord._lookup_citation_kinds({12, 13}, viewer, scope)
    assert result == expected
    assert 'content' not in conn.fetch.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('prior', [None, {'id': 99}])
async def test_retrospective_notes_overwrite_client_and_remove_self(prior):
    # Section 5: retrospective notes not scanned, client survives, re-save/concurrent self kept.
    from test_resave_axis_conflict import _request
    from unittest.mock import patch
    coord, conn = _coord()
    async def row(sql, *args):
        if 'FOR SHARE' in sql:
            return {'id': 240, 'type': 'decision', 'project': 'shared-memory'}
        if 'FOR UPDATE' in sql:
            return prior
        return {'id': 99}
    conn.fetchrow = AsyncMock(side_effect=row)
    conn.fetch = AsyncMock(return_value=[{'id': 12, 'metadata': {}, 'type': None, 'source_ref': None},
                                        {'id': 99, 'metadata': {'type': 'retrospective'}, 'type': 'retrospective', 'source_ref': None}])
    with patch.object(coord, '_embed', new=AsyncMock(return_value=[0.1])):
        response = await coord.handle_retrospective(_request({
            'pg_id': 240, 'rating': 'validated', 'notes': 'fact:12 retrospective:99 fact:0',
            'references': ['fact:999'], 'grounded_in': [12]}))
    assert response.status == 200, response.text
    insert = next(call for call in conn.fetchrow.call_args_list if 'INSERT INTO technical_docs' in call.args[0])
    assert insert.args[2]['references'] == ['fact:12']
    assert 'references' not in json.loads(response.text)
    reads = [call.args[0] for call in conn.fetchrow.call_args_list]
    assert next(i for i, sql in enumerate(reads) if 'FOR SHARE' in sql) < next(i for i, sql in enumerate(reads) if 'FOR UPDATE' in sql)


@pytest.mark.asyncio
async def test_reply_citations_never_enter_hydration_and_lookup_failure_is_local():
    # Section 5/r3: reference ids enter hydration; lookup failure erases unrelated fields or leaves key.
    coord, conn = _coord()
    coord.hydrate = AsyncMock(return_value={('technical_docs', 1): {'readable': True}})
    coord._lookup_citation_kinds = AsyncMock(return_value=None)
    hit = {'pg_id': 1, 'ref': 'fact:1', 'record_type': 'fact', 'metadata': {'references': ['fact:12'], 'other': 'kept'}}
    await coord._compose_search_hits([hit], ranked=True, viewer='other', viewer_scope='shared-memory', session=None)
    assert coord.hydrate.call_args.args[0] == {('technical_docs', 1)}
    coord._lookup_citation_kinds.assert_awaited_once_with({12}, 'other', 'shared-memory')
    assert hit['metadata'] == {'other': 'kept'}


@pytest.mark.asyncio
async def test_declared_informed_by_writer_keeps_assertion():
    # Section 5/r3: a new undeclared INFORMED_BY writer.
    from unittest.mock import MagicMock
    coord, conn = _coord()
    conn.fetch = AsyncMock(return_value=[{'id': 12, 'type': 'decision', 'source_ref': None}])
    resolved = await coord._resolve_typed_grounding(conn, [12], {'12': 'informed_by'})
    assert resolved[0]['rel'] == 'INFORMED_BY'
    assert resolved[0]['asserted_by'] == 'operator'
    session = MagicMock()
    session.run = AsyncMock()
    await coord._write_typed_grounding(session, 'Decision', 99, resolved)
    query = session.run.call_args.args[0]
    assert '{asserted_by: g.asserted_by}' in query
    assert all(row['asserted_by'] for row in session.run.call_args.kwargs['grounded'] if row['rel'] == 'INFORMED_BY')


@pytest.mark.asyncio
async def test_decision_scans_stored_content_not_alternatives():
    # Section 5: client value survives; alternatives are wrongly scanned instead of stored content.
    coord, conn = _coord()
    conn.fetchrow = AsyncMock(side_effect=lambda sql, *args: None if 'FOR UPDATE' in sql else {'id': 99})
    conn.fetch = AsyncMock(return_value=[{'id': 12, 'metadata': {}}, {'id': 13, 'metadata': {}}])
    response, _ = await _save(coord, {'source': 'test', 'type': 'decision', 'project': 'shared-memory',
        'decision': {'decided_by': 'Operator', 'project': 'shared-memory',
                     'rationale': 'fact:12', 'alternatives': ['fact:13']},
        'references': ['fact:13']}, 'title\nfact:12')
    assert response.status == 200, response.text
    insert = next(call for call in conn.fetchrow.call_args_list if 'INSERT INTO technical_docs' in call.args[0])
    assert insert.args[2]['references'] == ['fact:12']
    lookup = next(call for call in conn.fetch.call_args_list if 'SELECT id, metadata FROM technical_docs' in call.args[0])
    assert lookup.args[1] == [12]
    assert ' '.join(lookup.args[0].split()) == 'SELECT id, metadata FROM technical_docs WHERE id = ANY($1::bigint[])'


@pytest.mark.asyncio
async def test_citation_lookup_failure_returns_no_map():
    # Section 5/r3: citation-lookup failure leaves references usable.
    coord, conn = _coord()
    conn.fetch = AsyncMock(side_effect=RuntimeError('fixture failure'))
    assert await coord._lookup_citation_kinds({12}, 'owner', None) is None


@pytest.mark.parametrize('metadata', ['legacy scalar', ['legacy array'], 7, True, [], None])
@pytest.mark.asyncio
async def test_nonobject_target_does_not_break_save_lookup(metadata):
    # Legacy non-object targets are facts, regardless of their source eligibility.
    coord, conn = _coord()
    conn.fetch = AsyncMock(return_value=[{'id': 12, 'metadata': metadata}])
    assert await coord._references_for_save(conn, 'fact:12', None) == ['fact:12']


@pytest.mark.asyncio
async def test_keyword_fallback_cannot_select_through_private_citation():
    from test_visibility import _coordinator_with_mocks, _make_request
    from unittest.mock import patch
    import sqlite3
    coord, conn = _coordinator_with_mocks()
    database = sqlite3.connect(':memory:')
    database.create_function('jsonb_typeof', 1, lambda value: 'object')
    database.create_function('without_references', 1,
                             lambda value: json.dumps({k: v for k, v in json.loads(value).items() if k != 'references'}))
    database.executescript('CREATE TABLE technical_docs (id INTEGER, content TEXT, metadata TEXT, superseded BOOLEAN, visibility TEXT, agent_id TEXT);')
    database.executemany('INSERT INTO technical_docs VALUES (?, ?, ?, ?, ?, ?)', [
        (1, 'public narrative', json.dumps({'references': ['fact:12']}), False, 'global', 'owner'),
        (12, 'private narrative', '{}', False, 'private', 'owner')])
    async def fetch(sql, *args):
        # SQLite evaluates the actual fallback WHERE (including any added OR).
        # Translate only PostgreSQL operators/casts; keep all boolean predicates.
        import re
        sql = sql.replace("(metadata - 'references')::text", 'without_references(metadata)')
        sql = sql.replace('metadata::text', 'metadata').replace(' ILIKE ', ' LIKE ')
        sql = re.sub(r'\$(\d+)', lambda m: '?' + m.group(1), sql)
        return [dict(zip(('id', 'content', 'metadata'), (row[0], row[1], json.loads(row[2]))))
                for row in database.execute(sql, args)]
    conn.fetch = AsyncMock(side_effect=fetch)
    coord._compose_search_hits = AsyncMock()
    with patch.object(coord, '_embed', new=AsyncMock(return_value=None)):
        response = await coord.handle_search(_make_request({'query': 'fact:12'}, authenticated_agent='other'))
        assert json.loads(response.text)['results'] == []
        response = await coord.handle_search(_make_request({'query': 'public narrative'}, authenticated_agent='other'))
        assert [row['pg_id'] for row in json.loads(response.text)['results']] == [1]
    database.close()
