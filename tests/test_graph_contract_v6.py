"""Graph read contract failures, exercised through the handler."""
import json
from unittest.mock import AsyncMock
import pytest
from tests.test_read_hydration import (_make_node, _make_rel, _make_path, _FakeRecord,
                                 _coord_with_mocks, _graph_request)
import coordinator as c


def pg(pid, kind='fact', **extra):
    return dict(id=pid, content=f'Postgres whole {pid}', content_chars=16,
                metadata={'type': kind}, visibility='global', superseded=False,
                **extra)


@pytest.mark.asyncio
async def test_nested_nodes_and_relationship_ends(monkeypatch):
    # F3, F4c, F9, F19, F20, F32: all nested values use the same authority.
    monkeypatch.setattr(c, 'GRAPH_QUERY_ROW_CAP', 1)
    first = _make_node(['Decision'], {'pg_id': 1, 'title': 'keep title',
        'rationale': 'node rationale', 'notes': 'node notes', 'content': 'node copy',
        'rem_summary': 'wrong', 'superseded': True, 'structural': 42})
    private = _make_node(['Fact'], {'pg_id': 2, 'content': 'secret', 'title': 'secret'})
    last = _make_node(['Fact'], {'pg_id': 3, 'content': 'cut'})
    rel = _make_rel('GROUNDED_IN', {'notes': 'secret', 'role': 'keep'}, first, private)
    path = _make_path(first, rel)
    coord, conn, _ = _coord_with_mocks([_FakeRecord({'map': {'list': [first, [private, last]],
                                                         'path': path, 'edge': rel}})])
    coord.resolve_marks = AsyncMock(return_value={'decisions': {}, 'retrospectives': {}})
    conn.fetch = AsyncMock(return_value=[pg(1, 'decision'),
        pg(2, agent_id='owner') | {'visibility': 'private'}, pg(3)])
    response = await coord.handle_graph(_graph_request(agent='other'))
    assert response.status == 200
    result = json.loads(response.text)['records'][0]['map']
    node = result['list'][0]
    assert node['title'] == 'keep title' and node['structural'] == 42
    assert node['text'] == 'Postgres whole 1' and node['superseded'] is False
    assert not {'content', 'rem_summary', 'rationale', 'notes'} & node.keys()
    assert result['list'][1][0] == {'label': 'Fact'}
    assert result['list'][1][1]['ref'] == 'fact:3'
    assert result['path']['nodes'][1] == {'label': 'Fact'}
    assert result['edge'] == {'from': 'decision:1', 'relation': 'GROUNDED_IN',
                              'to': None, 'props': {'role': 'keep'}}
    assert result['path']['relationships'] == [result['edge']]
    assert conn.fetch.await_args.args[1] == [1, 2, 3]


@pytest.mark.asyncio
async def test_lookup_failure_and_missing_nodes():
    # F19, F20: failed hydration must not fall back to graph properties.
    n = _make_node(['Fact'], {'pg_id': 7, 'content': 'secret'})
    rel = _make_rel('SUPERSEDES', {}, n, n)
    coord, conn, _ = _coord_with_mocks([_FakeRecord({'n': n, 'r': rel})])
    conn.fetch = AsyncMock(side_effect=RuntimeError('lookup unavailable'))
    response = await coord.handle_graph(_graph_request())
    result = json.loads(response.text)['records'][0]
    assert result['n'] == {'label': 'Fact'}
    assert result['r'] == {'from': None, 'relation': 'SUPERSEDES', 'to': None, 'props': {}}


@pytest.mark.asyncio
async def test_hydration_joins_successors_and_checks_both_tables():
    # F16, F17: successors outside the requested set need their own visibility.
    coord, conn, _ = _coord_with_mocks()
    conn.fetch = AsyncMock(side_effect=[
        [pg(1, successor_id=9, successor_metadata={'type': 'decision'},
            successor_visibility='private', successor_agent_id='owner')],
        [pg(1, successor_id=8, successor_metadata={'kind': 'insight'},
            successor_visibility='global')]])
    result = await coord.hydrate({('technical_docs', 1), ('community_summaries', 1)}, 'other')
    assert conn.fetch.await_count == 2
    assert result[('technical_docs', 1)]['successor_readable'] is False
    assert result[('technical_docs', 1)]['successor_ref'] == 'decision:9'
    assert result[('community_summaries', 1)]['successor_ref'] == 'insight:8'
    assert result[('community_summaries', 1)]['successor_readable'] is True
    for call in conn.fetch.await_args_list:
        assert 'LEFT JOIN' in call.args[0] and 's.visibility' in call.args[0]


@pytest.mark.asyncio
async def test_marks_batches_all_siblings_and_roles():
    # F12, F13, F30: neighbour-only retrospectives still get every sibling.
    coord, _, session = _coord_with_mocks()
    class Result:
        def __aiter__(self):
            async def rows():
                yield {'decision_id': 1, 'outcomes': [{'pg_id': 4, 'rating': 'mixed'},
                       {'pg_id': 3, 'rating': 'validated'}],
                       'grounds': [{'pg_id': 5, 'relation': 'CONSIDERED'}]}
            return rows()
    session.run = AsyncMock(return_value=Result())
    marks = await coord.resolve_marks(session, [], [3])
    assert session.run.await_count == 1
    assert marks['retrospectives'][3] == {'decision_id': 1, 'siblings': [4, 3]}
    assert marks['decisions'][1]['grounds'] == [{'pg_id': 5, 'relation': 'CONSIDERED'}]
    assert set(session.run.await_args.kwargs['grounding_relations']) == set(c.GROUNDING_RELATIONS)


@pytest.mark.asyncio
async def test_unknown_visibility_is_not_global():
    # F20: unknown row or successor visibility cannot name a readable identity.
    coord, conn, _ = _coord_with_mocks()
    conn.fetch = AsyncMock(return_value=[pg(1, successor_id=2,
        successor_metadata={}, successor_visibility=None) | {'visibility': None}])
    rows = await coord.hydrate({('technical_docs', 1)}, 'other')
    assert rows[('technical_docs', 1)]['readable'] is False
    assert rows[('technical_docs', 1)]['successor_readable'] is False


@pytest.mark.asyncio
async def test_scope_failure_empty_and_metadata():
    """F4, F16: scope applies to both rows; failure differs from missing."""
    coord, conn, _ = _coord_with_mocks()
    assert await coord.hydrate(set(), 'viewer') == {}
    coord._pool.acquire.assert_not_called()
    conn.fetch = AsyncMock(return_value=[pg(1, scope='team', successor_id=2,
        successor_metadata={}, successor_visibility='scope', successor_scope='team') |
        {'visibility': 'scope', 'metadata': ['x']}])
    for scope, expected in [(None, False), ('team', True)]:
        rows = await coord.hydrate({('technical_docs', 1)}, 'viewer', scope)
        assert rows[('technical_docs', 1)]['readable'] is expected
        assert rows[('technical_docs', 1)]['successor_readable'] is expected
    conn.fetch = AsyncMock(side_effect=RuntimeError('failed'))
    assert await coord.hydrate({('technical_docs', 1)}, 'viewer') is None
    conn.fetch = AsyncMock(return_value=[])
    assert await coord.hydrate({('technical_docs', 1)}, 'viewer') == {}


@pytest.mark.asyncio
async def test_graph_marks_wiring():
    """F13, F16: graph hydrates sibling outcomes before rendering marks."""
    nodes = [_make_node([label], {'pg_id': pid}) for label, pid in
             [('Retrospective', 3), ('Decision', 1), ('Fact', 7)]]
    coord, conn, session = _coord_with_mocks([_FakeRecord({'nodes': nodes})])
    initial = session.run.return_value
    class Marks:
        def __aiter__(self):
            async def rows():
                yield {'decision_id': 1, 'outcomes': [
                    {'pg_id': 3, 'rating': 'validated'}, {'pg_id': 5, 'rating': 'reversed'}],
                    'grounds': []}
            return rows()
    async def run(query, **kwargs):
        if 'decision_ids' not in kwargs:
            return initial
        assert kwargs['retro_ids'] == [3]
        return Marks()
    session.run = AsyncMock(side_effect=run)
    conn.fetch = AsyncMock(return_value=[pg(1, 'decision') | {'superseded': True},
        pg(3, 'retrospective'), pg(5, 'retrospective'),
        pg(7, successor_id=8, successor_metadata={'type': 'fact'},
           successor_visibility='global') | {'superseded': True}])
    reply = await coord.handle_graph(_graph_request())
    assert reply.status == 200
    retro, decision, fact = json.loads(reply.text)['records'][0]['nodes']
    assert retro['latest_verdict'] == 'retrospective:5'
    assert decision['obsolete'] == 'reversed'
    assert decision['superseded_by'] == 'retrospective:5'
    assert fact['superseded_by'] == 'fact:8'
    assert 5 in conn.fetch.await_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('field,metadata', [
    ('metadata', value) for value in
    [{'decision': 'free text'}, {'decision': ['x']}, ['x'], 'plain']
] + [('successor_metadata', value) for value in ['plain', ['x']]])
async def test_hydration_bad_metadata_does_not_blank_batch(field, metadata):
    """F6, F16: malformed metadata keeps both rows and default successor refs."""
    coord, conn, _ = _coord_with_mocks()
    conn.fetch = AsyncMock(return_value=[pg(i, successor_id=9,
        successor_metadata={}, successor_visibility='global') | {field: metadata}
        for i in [1, 2]])
    rows = await coord.hydrate({('technical_docs', 1), ('technical_docs', 2)}, 'viewer')
    assert set(rows) == {('technical_docs', 1), ('technical_docs', 2)}
    assert all(r['readable'] and r['successor_ref'] == 'fact:9' for r in rows.values())


@pytest.mark.asyncio
async def test_resolve_marks_run_failure_warns_once(caplog):
    """F31: marks failure returns no partial marks and warns about all three."""
    coord, _, session = _coord_with_mocks()
    session.run = AsyncMock(side_effect=RuntimeError('unavailable'))
    assert await coord.resolve_marks(session, [1], [3]) == {
        'decisions': {}, 'retrospectives': {}, 'failed': True}
    warnings = [r for r in caplog.records if r.levelname == 'WARNING']
    assert len(warnings) == 1
    assert all(name in warnings[0].message for name in
               ['retired_grounds', 'latest_verdict', 'lifecycle'])


@pytest.mark.asyncio
async def test_graph_retrospective_hydrates_its_decision():
    """F17: a retrospective brings its decision into the same hydration batch."""
    retro = _make_node(['Retrospective'], {'pg_id': 3})
    coord, conn, _ = _coord_with_mocks([_FakeRecord({'node': retro})])
    coord.resolve_marks = AsyncMock(return_value={
        'decisions': {}, 'retrospectives': {3: {'decision_id': 1, 'siblings': [3]}}})
    conn.fetch = AsyncMock(return_value=[pg(1, 'decision'), pg(3, 'retrospective')])
    reply = await coord.handle_graph(_graph_request())
    assert reply.status == 200
    assert conn.fetch.await_count == 1
    assert conn.fetch.await_args.args[1] == [1, 3]
