"""Offline adversarial backfill tests; each comment names its section-5 failure."""
import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared-memory/scripts'))
import backfill_references as b
from neo4j.time import DateTime


def edge(eid='e', kind='REFERENCES', properties=None, source=1, target=2):
    return {'eid': eid, 'type': kind, 'source_labels': ['Decision'], 'target_labels': ['Decision'],
            'source': source, 'target': target, 'properties': properties or {}}


def test_plan_populations_and_null_metadata():
    # Section 5/r3: absent equal to [], NULL crashes, snapshot drift owed, declared edge deleted, untagged DateTime.
    rows = [(1, 'fact:2', {'type': 'decision', 'grounded_in': [2]}),
            (2, '', None), (3, '', {'references': ['fact:2']}),
            (4, '', 'scalar'), (5, '', []), (6, '', {'references': []})]
    edges = [edge(properties={'created_at': DateTime(2026, 1, 2, 3, 4, 5)}),
             edge('declared', 'INFORMED_BY', {'resolved_from': 'content'}),
             edge('delete', 'INFORMED_BY', {'resolved_from': 'content'}, source=7),
             edge('other', 'INFORMED_BY', {'cue': 'old'})]
    plan = b.plan_changes(rows, edges)
    assert plan['counts'] == {'a1': 2, 'a2': 1, 'b': 1, 'c': 1, 'd': 1, 'e': 1, 'f': 2}
    assert plan['rows'][1]['before'] == {'absent': True}
    assert {e['eid'] for e in plan['edges']} == {'e', 'delete'}
    typed = plan['edges'][0]['properties']['created_at']
    assert typed['t'] == 'DateTime'
    assert b.decode_value(typed) == edges[0]['properties']['created_at']


def test_before_exclusive_complete_private_and_binding(tmp_path):
    # Section 5/r3: overwrite, truncated resume, digest mismatch, world-readable files, different store.
    path = tmp_path / 'before'
    payload = {'op': 'operation', 'binding': {'pg': 'one', 'neo4j': 'one'}, 'rows': [], 'edges': []}
    b.write_before(path, payload)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert b.read_before(path) == payload
    with pytest.raises(FileExistsError):
        b.write_before(path, payload)
    with pytest.raises(ValueError, match='binding'):
        b.check_binding(payload['binding'], {'pg': 'other', 'neo4j': 'one'})
    data = path.read_bytes()
    path.write_bytes(data[:-1])
    with pytest.raises(ValueError, match='no store was touched'):
        b.read_before(path)
    path.write_bytes(data.replace(b'operation', b'changedxx'))
    with pytest.raises(ValueError):
        b.read_before(path)


def test_log_private_and_ignores_truncated_tail(tmp_path):
    # Section 5/r3: log world-readable, truncated last line blocks resume.
    path = tmp_path / 'before.log'
    b.create_log(path)
    b.append_log(path, 'op', 'planned')
    with path.open('ab') as stream:
        stream.write(b'{"partial":')
    assert b.read_log(path) == [{'op': 'op', 'event': 'planned'}]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_postgres_cas_null_binding_and_after_commit(tmp_path):
    # Section 5/r3: JSON-null binding, metadata overwritten, drift overwritten, rolled-back item logged.
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [(None,), ({'references': ['fact:2']},), ({'references': ['fact:9']},)]
    cursor.rowcount = 1
    rows = [{'id': 1, 'before': {'absent': True}, 'new': []},
            {'id': 2, 'before': {'value': []}, 'new': ['fact:2']},
            {'id': 3, 'before': {'value': []}, 'new': []}]
    events = b.apply_postgres(conn, rows)
    updates = [call for call in cursor.execute.call_args_list if call.args[0].startswith('UPDATE')]
    assert len(updates) == 1
    assert updates[0].args[1][2] is None
    assert "jsonb_set" in updates[0].args[0] and 'IS NOT DISTINCT FROM' in updates[0].args[0]
    assert events == [{'event': 'skipped', 'id': 3, 'reason': 'postgres drift'}]
    conn.__exit__.assert_called_once()
    conn.__exit__.side_effect = RuntimeError('commit failed')
    cursor.fetchone.side_effect = [(None,)]
    with pytest.raises(RuntimeError):
        b.apply_postgres(conn, rows[:1])


def test_graph_each_edge_lock_then_fresh_recheck():
    # Section 5/r3: single transaction, cached recheck, null end fails, replaced/declared edge deleted.
    session = MagicMock()
    tx = session.begin_transaction.return_value
    tx.__enter__.return_value = tx
    tx.run.return_value.single.return_value = {'n': 1}
    captured = b.plan_changes([], [edge(source=None), edge('two')])['edges']
    events = b.apply_graph(session, captured, 'op')
    assert session.begin_transaction.call_count == 2
    calls = tx.run.call_args_list
    assert len(calls) == 4
    assert 'SET r.backfill_op' in calls[0].args[0]
    assert 'DELETE r' in calls[1].args[0]
    assert 'keys(r)' in calls[1].args[0]
    assert 's.pg_id IS NULL' in calls[0].args[0]
    assert tx.commit.call_count == 2
    assert len(events) == 2


def test_lock_failure_skips_without_deleting():
    # Section 5: uncaptured or replaced edge deleted.
    session = MagicMock()
    tx = session.begin_transaction.return_value
    tx.__enter__.return_value = tx
    tx.run.return_value.single.side_effect = [{'n': 0}, {'n': 1}]
    captured = b.plan_changes([], [edge()])['edges']
    events = b.apply_graph(session, captured, 'op')
    assert tx.run.call_count == 2
    assert events[0]['reason'] == 'graph drift'


def test_fsync_before_mutation_and_log_after_commit(tmp_path, monkeypatch):
    # Section 5/r3: store touched before file+directory fsync; log precedes commit; false completion.
    trace = []
    real_fsync = b.os.fsync
    def synced(fd):
        trace.append('directory-fsync' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file-fsync')
        real_fsync(fd)
    monkeypatch.setattr(b.os, 'fsync', synced)
    binding = {'postgres': 'shared-memory', 'neo4j': 'shared-memory'}
    plan = b.plan_changes([(1, '', None)], [])
    monkeypatch.setattr(b, 'store_binding', lambda *args: binding)
    plans = iter([plan, b.plan_changes([(1, '', {'references': []})], [])])
    monkeypatch.setattr(b, 'read_plan', lambda *args: next(plans))
    def postgres(*args):
        assert trace.count('directory-fsync') == 2
        assert 'file-fsync' in trace
        trace.append('postgres-commit')
        return []
    monkeypatch.setattr(b, 'apply_postgres', postgres)
    monkeypatch.setattr(b, 'apply_graph', lambda *args: [])
    real_append = b.append_log
    def logged(path, op, event, **details):
        if event == 'postgres_committed':
            assert 'postgres-commit' in trace
        real_append(path, op, event, **details)
    monkeypatch.setattr(b, 'append_log', logged)
    path = tmp_path / 'before'
    assert b.run_operation(None, None, path) == 0
    assert [e['event'] for e in b.read_log(Path(str(path) + '.log'))] == ['planned', 'postgres_committed', 'verified']


def test_verification_owed_vs_snapshot_drift(tmp_path, monkeypatch, capsys):
    # Section 5/r3: complete with skips; snapshot drift reported as owed; repeat apply not no-op.
    binding = {'postgres': 'shared-memory', 'neo4j': 'shared-memory'}
    monkeypatch.setattr(b, 'store_binding', lambda *args: binding)
    plan = b.plan_changes([(1, '', {'references': ['fact:2']})], [])
    assert plan['counts']['a2'] == 1 and plan['counts']['a1'] == 0
    monkeypatch.setattr(b, 'read_plan', lambda *args: plan)
    monkeypatch.setattr(b, 'apply_postgres', lambda *args: [{'event': 'skipped', 'id': 1, 'reason': 'postgres drift'}])
    monkeypatch.setattr(b, 'apply_graph', lambda *args: [])
    assert b.run_operation(None, None, tmp_path / 'snapshot') == 0
    owed = b.plan_changes([(1, '', None)], [])
    monkeypatch.setattr(b, 'read_plan', lambda *args: owed)
    assert b.run_operation(None, None, tmp_path / 'owed') == 1
    assert 'completed with skips' in capsys.readouterr().out
    assert b.plan_changes([(1, '', {'references': []})], [])['rows'] == []


def test_graph_skips_changed_assertion_and_logs_after_commit():
    # Section 5/r3: gained asserted_by deleted; item logged after rollback.
    session = MagicMock()
    tx = session.begin_transaction.return_value
    tx.__enter__.return_value = tx
    tx.run.return_value.single.side_effect = [{'n': 1}, {'n': 0}]
    captured = b.plan_changes([], [edge(kind='INFORMED_BY', properties={'resolved_from': 'content'})])['edges']
    logged = []
    def committed(event):
        tx.commit.assert_called_once()
        logged.append(event)
    b.apply_graph(session, captured, 'op', committed)
    assert logged[0]['event'] == 'skipped'
    recheck = tx.run.call_args.args[0]
    assert 'asserted_by IS NULL' in recheck and 'REMOVE r.backfill_op' in recheck
    tx.commit.side_effect = RuntimeError('rollback')
    tx.run.return_value.single.side_effect = [{'n': 1}, {'n': 1}]
    logged.clear()
    with pytest.raises(RuntimeError):
        b.apply_graph(session, captured, 'op', committed)
    assert logged == []


def test_plan_cli_writes_nothing_and_never_prints_secret(monkeypatch, capsys):
    # Section 5: plan writes or a secret is printed; no live driver or loader is used.
    monkeypatch.setattr(b.secure_env, 'load_split_env', lambda: None)
    monkeypatch.setattr(b.secure_env, 'get_secret', lambda *args: 'fixture-secret')
    conn, driver, session = MagicMock(), MagicMock(), MagicMock()
    driver.session.return_value.__enter__.return_value = session
    monkeypatch.setattr(b.psycopg2, 'connect', lambda **kwargs: conn)
    monkeypatch.setattr(b.GraphDatabase, 'driver', lambda *args, **kwargs: driver)
    monkeypatch.setattr(b, 'store_binding', lambda *args: {'postgres': 'shared-memory', 'neo4j': 'shared-memory'})
    monkeypatch.setattr(b, 'read_plan', lambda *args: b.plan_changes([], []))
    mutation = MagicMock(side_effect=AssertionError('plan must not mutate'))
    monkeypatch.setattr(b, 'run_operation', mutation)
    monkeypatch.setattr(b, 'write_before', mutation)
    assert b.main([]) == 0
    mutation.assert_not_called()
    assert 'fixture-secret' not in capsys.readouterr().out


def test_resume_absent_graph_edge_is_done():
    # Section 5: resume repeats/skips phase wrongly; absent edge is done, not drift.
    session = MagicMock()
    tx = session.begin_transaction.return_value
    tx.__enter__.return_value = tx
    tx.run.return_value.single.side_effect = [{'n': 0}, {'n': 0}]
    captured = b.plan_changes([], [edge()])['edges']
    assert b.apply_graph(session, captured, 'op') == [
        {'event': 'graph_committed', 'eid': 'e', 'already_absent': True}]


def test_truncated_log_can_be_read_again_after_resume_append(tmp_path):
    # Section 5/r3: resume accepts a truncated log tail but the next append corrupts the next read.
    path = tmp_path / 'before.log'
    b.create_log(path)
    b.append_log(path, 'op', 'planned')
    with path.open('ab') as stream:
        stream.write(b'{"partial":')
    assert b.read_log(path) == [{'op': 'op', 'event': 'planned'}]
    b.append_log(path, 'op', 'postgres_committed')
    assert b.read_log(path)[-1]['event'] == 'postgres_committed'


def test_log_append_preserves_partial_bytes_and_starts_a_new_line(tmp_path):
    path = tmp_path / 'before.log'
    b.create_log(path)
    partial = b'{"partial":'
    path.write_bytes(partial)
    b.append_log(path, 'op', 'planned')
    assert path.read_bytes().startswith(partial + b'\n')
    assert json.loads(path.read_bytes().splitlines()[1])['event'] == 'planned'


def test_log_reader_skips_and_counts_all_invalid_json_lines(tmp_path, capsys):
    path = tmp_path / 'before.log'
    path.write_bytes(b'broken\n{"event": "valid"}\n{bad}\n{"partial":')
    assert b.read_log(path) == [{'event': 'valid'}]
    assert '3 unreadable log line(s) skipped' in capsys.readouterr().out


def test_nonobject_sources_are_untouched_but_are_fact_targets():
    rows = [(1, 'fact:2 fact:3', {}), (2, 'fact:1', 'scalar'), (3, 'fact:1', ['array'])]
    plan = b.plan_changes(rows, [])
    assert plan['counts']['f'] == 2
    assert plan['rows'] == [{'id': 1, 'before': {'absent': True}, 'new': ['fact:2', 'fact:3']}]


@pytest.mark.parametrize('field,value', [('grounded_in', [2]), ('grounded_in', ['2']),
                                        ('grounded_roles', {2: 'informed_by'}),
                                        ('grounded_roles', {'2': 'informed_by'})])
def test_declared_legacy_ids_are_kept(field, value):
    plan = b.plan_changes([(1, '', {field: value})],
                          [edge(kind='INFORMED_BY', properties={'resolved_from': 'content'})])
    assert plan['counts']['e'] == 1
    assert plan['edges'] == []


@pytest.mark.parametrize('value', ['٢', '2\n', '+2', 2.0, True, '0', str(b.ID_MAX + 1)])
def test_declared_rejects_invalid_ids(value):
    assert not b._declared({'grounded_in': [value], 'grounded_roles': {value: 'role'}}, 2)


def test_complete_flag_requires_true_even_with_valid_digest(tmp_path):
    path = tmp_path / 'before'
    b.write_before(path, {'op': 'op'})
    body, marker = path.read_bytes().splitlines(keepends=True)
    marker = json.loads(marker)
    marker['complete'] = False
    path.write_bytes(body + json.dumps(marker).encode() + b'\n')
    with pytest.raises(ValueError, match='incomplete before-file'):
        b.read_before(path)


def test_postgres_update_exact():
    assert ' '.join(b.PG_UPDATE.split()) == " ".join("""UPDATE technical_docs
SET metadata = jsonb_set(COALESCE(metadata,'{}'::jsonb), '{references}', %s::jsonb)
WHERE id = %s AND jsonb_typeof(COALESCE(metadata,'{}'::jsonb)) = 'object'
AND (metadata -> 'references') IS NOT DISTINCT FROM %s::jsonb""".split())


def test_plan_prints_nonempty_count(capsys):
    b.print_plan(b.plan_changes([(1, 'fact:2', {}), (2, '', {})], []), {})
    assert 'non-empty references=1' in capsys.readouterr().out


@pytest.fixture
def cli_stores(monkeypatch):
    monkeypatch.setattr(b.secure_env, 'load_split_env', lambda: None)
    monkeypatch.setattr(b.secure_env, 'get_secret', lambda *args: 'FAKE-PASSWORD-9182')
    conn, driver = MagicMock(), MagicMock()
    connect = MagicMock(return_value=conn)
    monkeypatch.setattr(b.psycopg2, 'connect', connect)
    monkeypatch.setattr(b.GraphDatabase, 'driver', MagicMock(return_value=driver))
    monkeypatch.setattr(b, 'store_binding', lambda *args: {'store': 'A'})
    monkeypatch.setattr(b, 'read_plan', lambda *args: b.plan_changes([], []))
    return connect, driver


@pytest.mark.parametrize('phase', ['postgres', 'graph'])
@pytest.mark.parametrize('error_type', [RuntimeError, ValueError])
def test_main_driver_payload_never_printed(phase, error_type, cli_stores, monkeypatch, tmp_path, capsys):
    connect, driver = cli_stores
    error = error_type('FAKE-PASSWORD-9182 private-cue-123')
    if phase == 'postgres':
        connect.side_effect = error
    else:
        driver.session.return_value.__enter__.return_value.run.side_effect = error
        monkeypatch.setattr(b, 'read_plan', lambda conn, session: session.run('fake query'))
    assert b.main(['--apply', '--before', str(tmp_path / 'before')]) == 2
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert 'backfill failed; no error payload printed' in output
    assert 'FAKE-PASSWORD-9182' not in output
    assert 'private-cue-123' not in output


@pytest.mark.parametrize('case', ['missing', 'truncated', 'existing', 'log', 'binding'])
def test_main_refusals_verbatim(case, cli_stores, tmp_path, capsys):
    connect, _ = cli_stores
    before = tmp_path / 'before'
    argv = ['--resume', str(before)]
    if case == 'missing':
        expected = 'missing before-file: no store was touched; provide the complete before-file'
    elif case == 'truncated':
        before.write_text('{')
        expected = 'incomplete before-file: no store was touched; remove it and rerun --apply'
    elif case == 'existing':
        before.write_text('existing')
        argv = ['--apply', '--before', str(before)]
        expected = 'before-file exists; refusing overwrite'
    elif case == 'log':
        Path(str(before) + '.log').write_text('old log')
        argv = ['--apply', '--before', str(before)]
        expected = f'log file exists: {before}.log; use --resume or a new path'
    else:
        b.write_before(before, dict(b.plan_changes([], []), binding={'store': 'B'}, op='op'))
        expected = 'store binding mismatch; refusing mutation'
    assert b.main(argv) == 2
    assert capsys.readouterr().err.strip() == expected
    if case != 'binding':
        connect.assert_not_called()
    if case == 'log':
        assert not before.exists()
    if case == 'binding':
        assert not Path(str(before) + '.log').exists()


@pytest.mark.parametrize('changed_call', [1, 2, 3])
def test_binding_checked_at_each_boundary(changed_call, tmp_path, monkeypatch):
    before = tmp_path / 'before'
    payload = dict(b.plan_changes([], []), binding={'store': 'A'}, op='op')
    # Resume calls binding at entry, immediately before mutation, then before verification.
    b.write_before(before, payload)
    calls = 0
    def binding(*args):
        nonlocal calls
        calls += 1
        return {'store': 'B' if calls == changed_call else 'A'}
    monkeypatch.setattr(b, 'store_binding', binding)
    postgres, graph, plan = MagicMock(return_value=[]), MagicMock(return_value=[]), MagicMock()
    monkeypatch.setattr(b, 'apply_postgres', postgres)
    monkeypatch.setattr(b, 'apply_graph', graph)
    monkeypatch.setattr(b, 'read_plan', plan)
    with pytest.raises(ValueError, match='store binding mismatch'):
        b.run_operation(None, None, before, resume=True)
    plan.assert_not_called()
    if changed_call < 3:
        postgres.assert_not_called()
        graph.assert_not_called()
    log = Path(str(before) + '.log')
    events = b.read_log(log) if log.exists() else []
    assert 'verified' not in [event['event'] for event in events]
    if changed_call == 1:
        assert events == []


@pytest.mark.parametrize('drift_at', ['lock', 'recheck', 'extra_property'])
def test_graph_property_drift_is_not_deleted(drift_at):
    # Evaluate the property predicates against an edge that changes at either boundary.
    properties = {'cue': 'captured', 'backfill_op': 'old'}
    captured = b.plan_changes([], [edge(properties=properties)])['edges']
    current = dict(properties)
    if drift_at == 'lock':
        current['cue'] = 'rewritten'
    deleted, locked = [], []
    session = MagicMock()
    tx = session.begin_transaction.return_value
    tx.__enter__.return_value = tx
    def run(query, **params):
        result = MagicMock()
        if 'SET r.backfill_op' in query:
            matches = 'properties(r) = $properties' not in query or current == params['properties']
            if matches:
                locked.append(True)
                current['backfill_op'] = params['op']
                if drift_at == 'recheck':
                    current['cue'] = 'rewritten'
                elif drift_at == 'extra_property':
                    current['extra'] = 'uncaptured'
        elif 'DELETE r' in query:
            fresh = {k: v for k, v in current.items() if k != 'backfill_op'}
            matches = True
            if 'all(k IN keys($fresh_properties) WHERE r[k] = $fresh_properties[k])' in query:
                matches &= all(fresh.get(k) == v for k, v in params['fresh_properties'].items())
            if "size([k IN keys(r) WHERE k <> 'backfill_op']) = size(keys($fresh_properties))" in query:
                matches &= len(fresh) == len(params['fresh_properties'])
            if matches:
                deleted.append(True)
        else:
            matches = True  # the edge is still present
        result.single.return_value = {'n': int(matches)}
        return result
    tx.run.side_effect = run
    events = b.apply_graph(session, captured, 'op')
    assert deleted == []
    if drift_at == 'lock':
        assert locked == []
    assert events[0]['event'] == 'skipped'


def test_operation_refuses_leftover_log_before_writing(tmp_path):
    before = tmp_path / 'before'
    Path(str(before) + '.log').write_text('leftover')
    with pytest.raises(ValueError, match='use --resume or a new path'):
        b.run_operation(None, None, before)
    assert not before.exists()
