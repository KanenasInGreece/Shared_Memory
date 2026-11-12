#!/usr/bin/env python3
"""Plan or ledger a canonical Postgres citation backfill and legacy graph removal."""
import argparse
import base64
import hashlib
import json
import os
import re
from pathlib import Path
import sys
import uuid

import psycopg2
from psycopg2.extras import Json
from neo4j import GraphDatabase
from neo4j import spatial, time

import secure_env
from reference_resolver import extract_citations, references_from
from record_ref import ID_MAX


class Refusal(ValueError):
    """A script-authored message safe to show without a driver payload."""


PG_UPDATE = """UPDATE technical_docs
SET metadata = jsonb_set(COALESCE(metadata,'{}'::jsonb), '{references}', %s::jsonb)
WHERE id = %s AND jsonb_typeof(COALESCE(metadata,'{}'::jsonb)) = 'object'
AND (metadata -> 'references') IS NOT DISTINCT FROM %s::jsonb"""
GRAPH_READ = """MATCH (s)-[r]->(t)
WHERE type(r) IN ['REFERENCES', 'INFORMED_BY']
RETURN elementId(r) AS eid, type(r) AS type, labels(s) AS source_labels,
labels(t) AS target_labels, s.pg_id AS source, t.pg_id AS target,
properties(r) AS properties,
[k IN keys(r) WHERE split(valueType(r[k]), ' NOT NULL')[0] IN
 ['ZONED DATETIME', 'LOCAL DATETIME', 'DATE', 'ZONED TIME', 'LOCAL TIME', 'DURATION'] |
 {k:k, t:valueType(r[k]), v:toString(r[k])}] AS temporal"""


TEMPORAL_CONSTRUCTORS = {'ZONED DATETIME', 'LOCAL DATETIME', 'DATE', 'ZONED TIME',
                         'LOCAL TIME', 'DURATION'}


def encode_value(value):
    """Keep graph property types through JSON, including driver temporal values."""
    if value is None:
        return {'t': 'Null', 'v': None}
    if isinstance(value, bool):
        return {'t': 'Boolean', 'v': value}
    if isinstance(value, int):
        return {'t': 'Integer', 'v': value}
    if isinstance(value, float):
        return {'t': 'Float', 'v': value.hex()}
    if isinstance(value, str):
        return {'t': 'String', 'v': value}
    if isinstance(value, (bytes, bytearray)):
        return {'t': 'Bytes', 'v': base64.b64encode(value).decode('ascii')}
    if isinstance(value, spatial.Point):
        return {'t': 'Point', 'v': {'srid': value.srid, 'coordinates': list(value)}}
    for name in ('DateTime', 'Date', 'Time', 'Duration'):
        if isinstance(value, getattr(time, name)):
            if name == 'DateTime':
                zone = getattr(value.tzinfo, 'zone', None) or getattr(value.tzinfo, 'key', None)
                if zone:
                    return {'t': name, 'v': value.iso_format(), 'zone': zone}
            return {'t': name, 'v': value.iso_format()}
    if isinstance(value, (list, tuple)):
        return {'t': 'List', 'v': [encode_value(item) for item in value]}
    raise Refusal('unsupported graph property type: ' + type(value).__name__)


def decode_value(tagged):
    kind, value = tagged['t'], tagged['v']
    if kind in ('Null', 'Boolean', 'Integer', 'String'):
        return value
    if kind == 'Float':
        return float.fromhex(value)
    if kind == 'Bytes':
        return base64.b64decode(value)
    if kind == 'Point':
        cls = {4326: spatial.WGS84Point, 4979: spatial.WGS84Point,
               7203: spatial.CartesianPoint, 9157: spatial.CartesianPoint}[value['srid']]
        return cls(value['coordinates'])
    if kind in ('DateTime', 'Date', 'Time', 'Duration'):
        restored = getattr(time, kind).from_iso_format(value)
        if kind == 'DateTime' and tagged.get('zone'):
            import pytz
            restored = restored.as_timezone(pytz.timezone(tagged['zone']))
        return restored
    if kind == 'List':
        return [decode_value(item) for item in value]
    raise Refusal('unsupported captured graph property type')


def canonical_list(value):
    if not isinstance(value, list):
        return False
    pattern = re.compile(r'(fact|decision|retrospective):([1-9][0-9]{0,18})')
    seen = set()
    for item in value:
        if not isinstance(item, str) or (match := pattern.fullmatch(item)) is None:
            return False
        if int(match.group(2)) > ID_MAX or item in seen:
            return False
        seen.add(item)
    return True


def _declared(metadata, target):
    if not isinstance(metadata, dict) or target is None:
        return False
    grounded = metadata.get('grounded_in')
    roles = metadata.get('grounded_roles')

    def matches(value):
        if isinstance(value, bool):
            return False
        if isinstance(value, str):
            if not re.fullmatch(r'[0-9]+', value):
                return False
            value = value.lstrip('0')
            if not value or len(value) > 19:
                return False
            value = int(value)
        return isinstance(value, int) and 1 <= value <= ID_MAX and value == target
    return ((isinstance(grounded, list) and any(matches(value) for value in grounded))
            or (isinstance(roles, dict) and any(matches(value) for value in roles)))


def plan_changes(rows, edges):
    """Compute owed lists, snapshot drift, and the captured graph population."""
    counts = dict.fromkeys(('a1', 'a2', 'b', 'c', 'd', 'e', 'f'), 0)
    metadata_by_id = {pid: meta if isinstance(meta, dict) else None for pid, _, meta in rows}
    changes = []
    for pid, content, meta in rows:
        if meta is not None and not isinstance(meta, dict):
            counts['f'] += 1
            continue
        new = references_from(extract_citations(content), metadata_by_id, pid)
        meta = meta or {}
        if 'references' in meta and meta['references'] == new:
            continue
        before = {'value': meta['references']} if 'references' in meta else {'absent': True}
        population = 'a2' if 'references' in meta and canonical_list(meta['references']) else 'a1'
        counts[population] += 1
        changes.append({'id': pid, 'before': before, 'new': new})
    captured = []
    for edge in edges:
        props = edge['properties']
        if edge['type'] == 'REFERENCES':
            counts['b'] += 1
        elif edge['type'] == 'INFORMED_BY' and props.get('asserted_by') is None:
            shaped = ('Decision' in edge['source_labels'] and 'Decision' in edge['target_labels']
                      and props.get('resolved_from') == 'content')
            if not shaped:
                counts['d'] += 1
                continue
            if _declared(metadata_by_id.get(edge['source']), edge['target']):
                counts['e'] += 1
                continue
            counts['c'] += 1
        else:
            continue
        temporal = {item['k']: {'t': item['t'], 'v': item['v']} for item in edge.get('temporal', [])}
        captured.append(dict(edge, properties={key: temporal[key] if key in temporal else encode_value(value)
                                               for key, value in props.items()}))
    return {'rows': changes, 'edges': captured, 'counts': counts}


def _directory_fsync(path):
    fd = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_before(path, payload):
    body = (json.dumps(payload, ensure_ascii=True, sort_keys=True) + '\n').encode()
    marker = {'complete': True, 'sha256': hashlib.sha256(body).hexdigest()}
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(body)
        stream.write((json.dumps(marker, sort_keys=True) + '\n').encode())
        stream.flush()
        os.fsync(stream.fileno())
    _directory_fsync(path)


def read_before(path):
    try:
        data = Path(path).read_bytes()
        lines = data.splitlines(keepends=True)
        if len(lines) != 2 or not lines[-1].endswith(b'\n'):
            raise ValueError()
        marker = json.loads(lines[1])
        if marker.get('complete') is not True or marker.get('sha256') != hashlib.sha256(lines[0]).hexdigest():
            raise ValueError()
        return json.loads(lines[0])
    except FileNotFoundError:
        raise Refusal('missing before-file: no store was touched; provide the complete before-file') from None
    except IsADirectoryError:
        raise Refusal('cannot read before-file: no store was touched; not a readable file') from None
    except OSError:
        raise Refusal('cannot read before-file: no store was touched; check file permissions') from None
    except (ValueError, KeyError, TypeError):
        raise Refusal('incomplete before-file: no store was touched; remove it and rerun --apply') from None


def create_log(path):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    _directory_fsync(path)


def append_log(path, op, event, **details):
    data = (json.dumps(dict(details, op=op, event=event), sort_keys=True) + '\n').encode()
    fd = os.open(path, os.O_RDWR | os.O_APPEND)
    with os.fdopen(fd, 'a+b') as stream:
        if stream.seek(0, os.SEEK_END):
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b'\n':
                stream.write(b'\n')
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def read_log(path):
    lines = Path(path).read_bytes().splitlines(keepends=True)
    events = []
    unreadable = 0
    for line in lines:
        try:
            events.append(json.loads(line))
        except (ValueError, UnicodeDecodeError):
            unreadable += 1
    if unreadable:
        print(f'{unreadable} unreadable log line(s) skipped')
    return events


def check_binding(expected, actual):
    if expected != actual:
        raise Refusal('store binding mismatch; refusing mutation')


def store_binding(conn, session):
    with conn:
        with conn.cursor() as cur:
            try:
                cur.execute('SAVEPOINT binding_identity')
                cur.execute('SELECT system_identifier::text FROM pg_control_system()')
                pg = {'system_identifier': cur.fetchone()[0]}
                cur.execute('RELEASE SAVEPOINT binding_identity')
            except psycopg2.Error:
                cur.execute('ROLLBACK TO SAVEPOINT binding_identity')
                cur.execute('SELECT oid, current_database(), inet_server_addr()::text, inet_server_port() '
                            'FROM pg_database WHERE datname = current_database()')
                row = cur.fetchone()
                if not row or row[2] is None or row[3] is None:
                    raise Refusal('cannot establish Postgres store binding')
                pg = dict(zip(('oid', 'database', 'address', 'port'), row))
    # A cluster identifier alone does not identify a database within that cluster.
    with conn:
        with conn.cursor() as cur:
            cur.execute('SELECT oid, current_database() FROM pg_database WHERE datname = current_database()')
            oid, name = cur.fetchone()
            pg.update(oid=oid, database=name)
    info = session.run('CALL db.info() YIELD id RETURN id').single()
    if not info or not info['id']:
        raise Refusal('cannot establish Neo4j store binding')
    return {'postgres': pg, 'neo4j': info['id']}


def read_plan(conn, session):
    with conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id, content, metadata FROM technical_docs ORDER BY id')
            rows = cur.fetchall()
    edges = [dict(record) for record in session.run(GRAPH_READ)]
    return plan_changes(rows, edges)


def apply_postgres(conn, rows):
    """Return outcomes only after the transaction commits; absent binds as SQL NULL."""
    events = []
    with conn:
        with conn.cursor() as cur:
            for item in rows:
                cur.execute('SELECT metadata FROM technical_docs WHERE id = %s', (item['id'],))
                row = cur.fetchone()
                if row is None or (row[0] is not None and not isinstance(row[0], dict)):
                    events.append({'event': 'skipped', 'id': item['id'], 'reason': 'postgres drift'})
                    continue
                metadata = row[0] or {}
                current = {'value': metadata['references']} if 'references' in metadata else {'absent': True}
                if current == {'value': item['new']}:
                    continue
                if current != item['before']:
                    events.append({'event': 'skipped', 'id': item['id'], 'reason': 'postgres drift'})
                    continue
                captured = None if item['before'].get('absent') else Json(item['before']['value'])
                cur.execute(PG_UPDATE, (Json(item['new']), item['id'], captured))
                if cur.rowcount != 1:
                    events.append({'event': 'skipped', 'id': item['id'], 'reason': 'postgres CAS changed'})
    return events


def graph_statements(edge):
    """Lock and recheck server temporal text; legacy tags keep strict Bolt equality.

    Old before-files remain readable, but ambiguous legacy UTC tags can still
    skip as drift. Never weaken equality to guess the original zone identity.
    """
    ends = []
    for name, key in (('s', 'source'), ('t', 'target')):
        ends.append(f'{name}.pg_id IS NULL' if edge[key] is None else f'{name}.pg_id = ${key}')
    identity = ' AND '.join(ends) + ' AND type(r) = $type'
    identity += ' AND size(labels(s)) = size($source_labels) AND all(l IN labels(s) WHERE l IN $source_labels)'
    identity += ' AND size(labels(t)) = size($target_labels) AND all(l IN labels(t) WHERE l IN $target_labels)'
    temporal = any(value['t'].split(' NOT NULL')[0] in TEMPORAL_CONSTRUCTORS
                   for value in edge.get('properties', {}).values())
    exact = ('all(k IN keys($properties) WHERE r[k] = $properties[k])'
             ' AND all(p IN $temporal WHERE toString(r[p.k]) = p.v AND valueType(r[p.k]) = p.t)'
             ' AND size(keys(r)) = size(keys($properties)) + size($temporal)') if temporal else 'properties(r) = $properties'
    lock = ('MATCH (s)-[r]->(t) WHERE elementId(r) = $eid AND ' + identity
            + ' AND ' + exact + ' SET r.backfill_op = $op RETURN count(r) AS n')
    fresh = (identity + ' AND all(k IN keys($fresh_properties) WHERE r[k] = $fresh_properties[k])'
             + " AND size([k IN keys(r) WHERE k <> 'backfill_op']) = size(keys($fresh_properties))")
    if temporal:
        fresh = fresh.replace('size(keys($fresh_properties))', 'size(keys($fresh_properties)) + size($fresh_temporal)')
        fresh += ' AND all(p IN $fresh_temporal WHERE toString(r[p.k]) = p.v AND valueType(r[p.k]) = p.t)'
    if edge['type'] == 'INFORMED_BY':
        fresh += ' AND r.asserted_by IS NULL'
    recheck = ('MATCH (s)-[r]->(t) WHERE elementId(r) = $eid WITH s, r, t, coalesce((' + fresh + '), false) AS matches '
               'FOREACH (_ IN CASE WHEN matches THEN [1] ELSE [] END | DELETE r) '
               'FOREACH (_ IN CASE WHEN NOT matches THEN [1] ELSE [] END | REMOVE r.backfill_op) '
               'RETURN CASE WHEN matches THEN 1 ELSE 0 END AS n')
    return lock, recheck


def apply_graph(session, edges, op, committed=None):
    events = []
    for edge in edges:
        temporal = [dict(k=key, **value) for key, value in edge['properties'].items()
                    if value['t'].split(' NOT NULL')[0] in TEMPORAL_CONSTRUCTORS]
        props = {key: decode_value(value) for key, value in edge['properties'].items()
                 if value['t'].split(' NOT NULL')[0] not in TEMPORAL_CONSTRUCTORS}
        params = {key: value for key, value in edge.items() if key != 'properties'}
        params.update(properties=props, fresh_properties={k: v for k, v in props.items() if k != 'backfill_op'}, op=op)
        params.update(temporal=temporal, fresh_temporal=[p for p in temporal if p['k'] != 'backfill_op'])
        lock, recheck = graph_statements(edge)
        with session.begin_transaction() as tx:
            matched = tx.run(lock, **params).single()['n']
            if not matched:
                present = tx.run('MATCH ()-[r]->() WHERE elementId(r) = $eid RETURN count(r) AS n', eid=edge['eid']).single()['n']
                event = ({'event': 'skipped', 'eid': edge['eid'], 'reason': 'graph drift'} if present
                         else {'event': 'graph_committed', 'eid': edge['eid'], 'already_absent': True})
            else:
                deleted = tx.run(recheck, **params).single()
                event = ({'event': 'graph_committed', 'eid': edge['eid']} if deleted and deleted['n']
                         else {'event': 'skipped', 'eid': edge['eid'], 'reason': 'graph recheck drift'})
            tx.commit()
        events.append(event)
        if committed is not None:
            committed(event)
    return events


def print_plan(plan, binding):
    print('store binding: ' + json.dumps(binding, sort_keys=True))
    print(' '.join(f'({key})={value}' for key, value in plan['counts'].items()))
    print(f"non-empty references={sum(bool(row['new']) for row in plan['rows'])}")
    if any(plan['counts'][key] for key in ('d', 'e', 'f')):
        print('UNRESOLVED: (d), (e), (f) require operator review; these items are unchanged.')


def run_operation(conn, session, before, *, resume=False):
    log = Path(str(before) + '.log')
    if resume:
        payload = read_before(before)
        check_binding(payload['binding'], store_binding(conn, session))
        if not log.exists():
            create_log(log)
        read_log(log)
    else:
        refuse_existing_log(before)
        binding = store_binding(conn, session)
        payload = dict(read_plan(conn, session), binding=binding, op=str(uuid.uuid4()))
        write_before(before, payload)
        create_log(log)
    op = payload['op']
    append_log(log, op, 'planned')
    check_binding(payload['binding'], store_binding(conn, session))
    events = apply_postgres(conn, payload['rows'])
    append_log(log, op, 'postgres_committed')
    for event in events:
        append_log(log, op, **event)
    def log_committed(event):
        append_log(log, op, **event)
    events += apply_graph(session, payload['edges'], op, log_committed)
    check_binding(payload['binding'], store_binding(conn, session))
    verification = read_plan(conn, session)
    print_plan(verification, payload['binding'])
    skips = [event for event in events if event['event'] == 'skipped']
    for event in skips:
        print('skipped: ' + json.dumps(event, sort_keys=True))
    if all(verification['counts'][key] == 0 for key in ('a1', 'b', 'c')):
        append_log(log, op, 'verified')
        print('complete')
        return 0
    print('completed with skips; (a1), (b), (c) remain owed')
    return 1


def refuse_existing_log(before):
    log = Path(str(before) + '.log')
    if log.exists():
        raise Refusal(f'log file exists: {log}; use --resume or a new path')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--resume', type=Path)
    parser.add_argument('--before', type=Path)
    args = parser.parse_args(argv)
    if args.apply != (args.before is not None):
        parser.error('--apply requires --before PATH, and --before requires --apply')
    # Refuse an incomplete ledger before opening either store.
    if args.resume:
        try:
            read_before(args.resume)
        except Refusal as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.before and args.before.exists():
        print('before-file exists; refusing overwrite', file=sys.stderr)
        return 2
    if args.before:
        try:
            refuse_existing_log(args.before)
        except Refusal as exc:
            print(str(exc), file=sys.stderr)
            return 2
    secure_env.load_split_env()
    pg_password = secure_env.get_secret('PG_PASSWORD', '')
    neo_password = secure_env.get_secret('NEO4J_PASSWORD', '')
    if not pg_password or not neo_password:
        print('PG_PASSWORD and NEO4J_PASSWORD are required', file=sys.stderr)
        return 2
    conn = driver = None
    try:
        conn = psycopg2.connect(host=os.environ.get('PG_HOST', 'localhost'),
                                port=os.environ.get('PG_PORT', '5432'),
                                user=os.environ.get('PG_USER', 'postgres'),
                                dbname=os.environ.get('PG_DATABASE', 'agent_data'), password=pg_password)
        driver = GraphDatabase.driver(os.environ.get('NEO4J_URI', 'bolt://localhost:7687'),
                                      auth=(os.environ.get('NEO4J_USER', 'neo4j'), neo_password))
        with driver.session() as session:
            if args.apply or args.resume:
                return run_operation(conn, session, args.resume or args.before, resume=bool(args.resume))
            binding = store_binding(conn, session)
            print_plan(read_plan(conn, session), binding)
            return 0
    except Refusal as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        # Driver exceptions can contain connection settings or private captured properties.
        if args.apply and not Path(args.before).exists():
            recovery = 'no before-file was written; rerun --apply'
        elif args.apply or args.resume:
            recovery = 'resume with the complete before-file'
        else:
            recovery = 'plan wrote no before-file; rerun the plan'
        print('backfill failed; no error payload printed; ' + recovery, file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()
        if driver is not None:
            driver.close()


if __name__ == '__main__':
    raise SystemExit(main())
