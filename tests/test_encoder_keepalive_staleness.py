"""Dead peers are detected; outstanding probes cannot leave old measurements green."""
import ast
import asyncio
import copy
import importlib
import socket
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'shared-memory' / 'scripts'
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    import secure_env
    monkeypatch.setattr(secure_env, 'load_split_env', lambda: None)
    monkeypatch.setenv('LLM_BACKENDS_JSON', '[{"url":"http://localhost:5000","n_ctx":8192,"max_output_tokens":2048}]')
    monkeypatch.setenv('CAPACITY_LOG_PATH', str(tmp_path / 'capacity.jsonl'))
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy
    return importlib.reload(hive_mind_proxy)


@pytest.mark.parametrize('backend', ['reranker', 'embedder'])
@pytest.mark.parametrize('age', [None, 1.2, 600.0, 600.01, 601.2])
def test_health_probe_age(gateway, monkeypatch, backend, age):
    g = gateway
    monkeypatch.setattr(g, 'CAPABILITY_PROBE_INTERVAL_S', 600)
    monkeypatch.setattr(g.time, 'monotonic', lambda: 1000.0)
    block = {'projected_full_payload_s': 12.0, 'projection_stale': False}
    g._capability = {backend: block}
    if age is not None:
        calls = g._capability_probe_calls[backend]
        token = calls.begin()
        calls._waiting[token] -= age
    view = g.capability_snapshot()[backend]
    assert view['probe_outstanding_age_s'] == (round(age, 1) if age is not None else None)
    stale = age is not None and age > 600
    assert view['projection_stale'] is stale
    assert 'serves_full_payload' not in view
    assert block == {'projected_full_payload_s': 12.0, 'projection_stale': False}


@pytest.mark.parametrize('measured', [False, True])
def test_failed_reading_preserves_unknown_or_carried_projection(gateway, measured):
    g = gateway
    previous = {'reranker': {'projected_full_payload_s': 12.0}} if measured else {}
    g._capability = g._merge_capability_projection(previous, {'reranker': {'status': 'failing'}})
    stored = copy.deepcopy(g._capability)
    calls = g._capability_probe_calls['reranker']
    token = calls.begin()
    calls._waiting[token] -= g.CAPABILITY_PROBE_INTERVAL_S * 2
    view = g.capability_snapshot()['reranker']
    assert view['projection_stale'] is (True if measured else None)
    assert 'serves_full_payload' not in view
    assert view.get('projected_full_payload_s') == (12.0 if measured else None)
    calls.end(token, completed=False)
    assert g.capability_snapshot()['reranker']['projection_stale'] is (True if measured else None)
    assert g._capability == stored


@pytest.mark.parametrize('backend', ['reranker', 'embedder'])
@pytest.mark.parametrize('outcome', ['response', 'error', 'cancel'])
def test_probe_tracking_lifetime(gateway, backend, outcome):
    g = gateway

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        class Response:
            status = 200

            async def read(self):
                return b'{}'

        class Request:
            def __init__(self, name):
                self.name = name

            async def __aenter__(self):
                if self.name == backend:
                    entered.set()
                    await asyncio.wait_for(release.wait(), 10)
                    if outcome == 'error':
                        raise OSError('dead peer')
                return Response()

            async def __aexit__(self, *args):
                pass

        class Session:
            def post(self, url, **kwargs):
                return Request('reranker' if url.endswith('/reranking') else 'embedder')

        task = asyncio.create_task(g._probe_capability(Session()))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            calls = g._capability_probe_calls[backend]
            assert calls.snapshot()['outstanding_count'] == 1
            if outcome == 'cancel':
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 10)
            else:
                release.set()
                await asyncio.wait_for(task, 10)
            snap = calls.snapshot()
            assert snap['outstanding_count'] == 0
            assert (snap['last_completed_at'] is not None) is (outcome == 'response')
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_keepalive_options(gateway, monkeypatch):
    import coordinator as c
    names = ['ENCODER_TCP_KEEPALIVE_IDLE_S', 'ENCODER_TCP_KEEPALIVE_INTERVAL_S', 'ENCODER_TCP_KEEPALIVE_COUNT']
    for name in names:
        monkeypatch.delenv(name, raising=False)
    c._keepalive_warned.clear()
    c._keepalive_accepted.clear()
    expected = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
                (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60),
                (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 15),
                (socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)]
    assert c.encoder_keepalive_options() == expected
    for name, value in zip(names, [61, 16, 5]):
        monkeypatch.setenv(name, str(value))
    expected = [expected[0]] + [(level, opt, value) for (level, opt, _), value in zip(expected[1:], [61, 16, 5])]
    assert c.encoder_keepalive_options() == expected
    for attr in ['SO_KEEPALIVE', 'TCP_KEEPIDLE', 'TCP_KEEPINTVL', 'TCP_KEEPCNT']:
        with monkeypatch.context() as patch:
            option = getattr(socket, attr)
            patch.delattr(socket, attr)
            assert c.encoder_keepalive_options() == [row for row in expected if row[1] != option or row[0] != (socket.SOL_SOCKET if attr == 'SO_KEEPALIVE' else socket.IPPROTO_TCP)]


def test_real_client_sockets(gateway, monkeypatch):
    import coordinator as c
    for name, value in [('IDLE_S', 61), ('INTERVAL_S', 16), ('COUNT', 5)]:
        monkeypatch.setenv('ENCODER_TCP_KEEPALIVE_' + name, str(value))

    def check(sock):
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
        for option, value in [(socket.TCP_KEEPIDLE, 61), (socket.TCP_KEEPINTVL, 16), (socket.TCP_KEEPCNT, 5)]:
            assert sock.getsockopt(socket.IPPROTO_TCP, option) == value

    async def scenario():
        from aiohttp import web
        app = web.Application()
        app.router.add_get('/', lambda request: web.Response(text='ok'))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        url = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
        proxy = gateway.AsyncHiveMindProxy()
        try:
            await proxy.start_session()
            async with proxy.session.get(url) as response:
                await response.read()
            connections = list(proxy.session.connector._conns.values())
            assert connections
            for pool in connections:
                for protocol, _ in pool:
                    check(protocol.transport.get_extra_info('socket'))
            # Execute the actual constructor expressions at all four production sites.
            tree = ast.parse((SCRIPTS / 'coordinator.py').read_text())
            constructors = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Attribute)
                            and isinstance(node.func.value, ast.Name)
                            and node.func.value.id == 'httpx' and node.func.attr == 'AsyncClient']
            assert len(constructors) == 4
            for node in constructors:
                client = eval(compile(ast.Expression(node), '<production client>', 'eval'), vars(c))
                async with client:
                    async with client.stream('GET', url) as response:
                        check(response.extensions['network_stream'].get_extra_info('socket'))
                        await response.aread()
        finally:
            if proxy.session:
                await proxy.session.close()
            if proxy.llm_session:
                await proxy.llm_session.close()
            await runner.cleanup()

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_probe_age_contract(gateway):
    import telemetry_contract as tc
    key = 'backend_capability.*.probe_outstanding_age_s'
    assert key in tc.HEALTH
    for backend in ['reranker', 'embedder']:
        gateway._capability = {backend: {'projection_stale': None}}
        paths = tc.canonical_paths({'backend_capability': gateway.capability_snapshot()}, tc.HEALTH)
        assert key in paths


def test_coordinator_encoder_clients_have_timeout_none(gateway):
    import coordinator as c
    tree = ast.parse((SCRIPTS / 'coordinator.py').read_text())
    constructors = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == 'httpx' and node.func.attr == 'AsyncClient']
    assert len(constructors) == 4

    async def scenario():
        for node in constructors:
            client = eval(compile(ast.Expression(node), '<production client>', 'eval'), vars(c))
            async with client:
                assert client.timeout.as_dict() == dict(connect=None, read=None, write=None, pool=None)

    asyncio.run(asyncio.wait_for(scenario(), 10))


@pytest.mark.parametrize('value', ['60s', '0', '-5', ''])
def test_invalid_keepalive_uses_defaults_and_warns_once(gateway, monkeypatch, caplog, value):
    import coordinator as c
    names = ['ENCODER_TCP_KEEPALIVE_IDLE_S', 'ENCODER_TCP_KEEPALIVE_INTERVAL_S',
             'ENCODER_TCP_KEEPALIVE_COUNT']
    for name in names:
        monkeypatch.delenv(name, raising=False)
    c._keepalive_warned.clear()
    c._keepalive_accepted.clear()
    defaults = c.encoder_keepalive_options()
    for name in names:
        monkeypatch.setenv(name, value)
    for _ in range(2):
        assert c.encoder_keepalive_options() == defaults
    for name in names:
        assert sum(name in record.getMessage() for record in caplog.records) == 1


@pytest.mark.parametrize('backend', ['embedder', 'reranker'])
def test_window_probe_hang_marks_projection_stale(gateway, monkeypatch, backend):
    import encoder_window as window
    g = gateway
    monkeypatch.setattr(window, '_window_cache', {})
    g._capability = {backend: {'projected_full_payload_s': 12.0, 'projection_stale': False}}

    async def scenario():
        entered = asyncio.Event()

        class Response:
            status = 200

            async def json(self):
                return {'n_ctx': 8192}

            async def read(self):
                return b'{}'

        class Request:
            def __init__(self, hang=False):
                self.hang = hang

            async def __aenter__(self):
                if self.hang:
                    entered.set()
                    await asyncio.Future()
                return Response()

            async def __aexit__(self, *args):
                pass

        class Session:
            def get(self, *args, **kwargs):
                return Request()

            def post(self, url, **kwargs):
                name = 'embedder' if url.endswith('/embeddings') else 'reranker'
                return Request(name == backend)

        task = asyncio.create_task(window.probe_encoder_window(
            Session(), 'http://embedder', 'http://reranker',
            calls=g._capability_probe_calls))
        calls = g._capability_probe_calls[backend]
        try:
            await asyncio.wait_for(entered.wait(), 10)
            view = g.capability_snapshot()[backend]
            assert isinstance(view['probe_outstanding_age_s'], (int, float))
            assert view['projection_stale'] is False
            assert calls.snapshot()['outstanding_count'] == 1
            for token in calls._waiting:
                calls._waiting[token] -= g.CAPABILITY_PROBE_INTERVAL_S + 1
            view = g.capability_snapshot()[backend]
            assert view['probe_outstanding_age_s'] > g.CAPABILITY_PROBE_INTERVAL_S
            assert view['projection_stale'] is True
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert calls.snapshot()['outstanding_count'] == 0
        assert calls.snapshot()['last_completed_at'] is None

    asyncio.run(scenario())


# A value the platform's setsockopt rejects falls back at parse time like a
# non-integer one.
KEEPALIVE_SETTINGS = [
    ('ENCODER_TCP_KEEPALIVE_IDLE_S', 'TCP_KEEPIDLE', 60),
    ('ENCODER_TCP_KEEPALIVE_INTERVAL_S', 'TCP_KEEPINTVL', 15),
    ('ENCODER_TCP_KEEPALIVE_COUNT', 'TCP_KEEPCNT', 4),
]


# List candidate inputs only; whether each is accepted is measured on the
# running machine, never assumed.
KEEPALIVE_VALUES = [10 ** 12, 2 ** 31, 40000, 32767, 128, 127]


def _reset_keepalive(c, monkeypatch):
    for name, _, _ in KEEPALIVE_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    c._keepalive_warned.clear()
    c._keepalive_accepted.clear()


def _platform_accepts(attr, value):
    """Measure the real acceptance on this machine; never assume a literal ceiling."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.IPPROTO_TCP, getattr(socket, attr), value)
        return True
    except (OSError, TypeError, OverflowError):
        return False
    finally:
        probe.close()


@pytest.mark.parametrize('env, attr, default', KEEPALIVE_SETTINGS)
@pytest.mark.parametrize('value', KEEPALIVE_VALUES)
def test_value_the_platform_rejects_falls_back_and_accepted_one_is_kept(
        gateway, monkeypatch, caplog, env, attr, default, value):
    import coordinator as c
    _reset_keepalive(c, monkeypatch)
    monkeypatch.setenv(env, str(value))
    accepted = _platform_accepts(attr, value)
    row = [r for r in c.encoder_keepalive_options() if r[1] == getattr(socket, attr)]
    assert row == [(socket.IPPROTO_TCP, getattr(socket, attr), value if accepted else default)]
    for _ in range(3):
        c.encoder_keepalive_options()
    warnings = [r.getMessage() for r in caplog.records if env in r.getMessage()]
    assert len(warnings) == (0 if accepted else 1)
    if not accepted:
        assert str(value) in warnings[0] and 'rejected' in warnings[0]


def test_the_measurement_is_not_vacuous_on_this_platform(gateway):
    """The parametrised test above must see both an accepted and a rejected value."""
    outcomes = [_platform_accepts(attr, value)
                for _, attr, _ in KEEPALIVE_SETTINGS for value in KEEPALIVE_VALUES]
    assert any(outcomes), 'the platform accepted none of the probe values'
    if all(outcomes):
        pytest.skip('this platform has no ceiling on these options')


class _ProbeSocket:
    made = []

    def __init__(self, *args, accept=True, exc=OSError('rejected'), **kwargs):
        self.accept, self.exc, self.closed, self.options = accept, exc, False, []
        _ProbeSocket.made.append(self)

    def setsockopt(self, *args):
        self.options.append(args)
        if not self.accept:
            raise self.exc

    def close(self):
        self.closed = True


def _use_probe_sockets(monkeypatch, c, **kwargs):
    _ProbeSocket.made = []
    monkeypatch.setattr(c.socket, 'socket', lambda *a, **kw: _ProbeSocket(*a, **kwargs))


def test_the_check_is_memoised_per_setting_and_value(gateway, monkeypatch):
    import coordinator as c
    _reset_keepalive(c, monkeypatch)
    monkeypatch.setenv('ENCODER_TCP_KEEPALIVE_IDLE_S', '77')
    _use_probe_sockets(monkeypatch, c)
    for _ in range(5):
        c.encoder_keepalive_options()
    assert len(_ProbeSocket.made) == 1
    monkeypatch.setenv('ENCODER_TCP_KEEPALIVE_IDLE_S', '78')
    c.encoder_keepalive_options()
    assert len(_ProbeSocket.made) == 2


@pytest.mark.parametrize('accept, exc', [(True, None), (False, OSError('no')),
                                         (False, TypeError('no')), (False, OverflowError('no'))])
def test_the_throwaway_socket_is_always_closed_and_any_rejection_means_default(
        gateway, monkeypatch, accept, exc):
    import coordinator as c
    _reset_keepalive(c, monkeypatch)
    monkeypatch.setenv('ENCODER_TCP_KEEPALIVE_COUNT', '9')
    _use_probe_sockets(monkeypatch, c, accept=accept, exc=exc)
    row = [r for r in c.encoder_keepalive_options() if r[1] == socket.TCP_KEEPCNT]
    assert row[0][2] == (9 if accept else 4)
    assert len(_ProbeSocket.made) == 1 and _ProbeSocket.made[0].closed


def test_a_throwaway_socket_that_cannot_be_made_keeps_the_value_and_is_not_remembered(
        gateway, monkeypatch, caplog):
    import coordinator as c
    _reset_keepalive(c, monkeypatch)
    monkeypatch.setenv('ENCODER_TCP_KEEPALIVE_INTERVAL_S', '33')
    real = socket.socket

    def refuse(*args, **kwargs):
        raise OSError('no descriptors')

    monkeypatch.setattr(c.socket, 'socket', refuse)
    row = [r for r in c.encoder_keepalive_options() if r[1] == socket.TCP_KEEPINTVL]
    assert row[0][2] == 33
    assert not [r for r in caplog.records if 'ENCODER_TCP_KEEPALIVE_INTERVAL_S' in r.getMessage()]
    assert not c._keepalive_accepted
    monkeypatch.setattr(c.socket, 'socket', real)
    monkeypatch.setenv('ENCODER_TCP_KEEPALIVE_INTERVAL_S', str(10 ** 12))
    assert [r for r in c.encoder_keepalive_options() if r[1] == socket.TCP_KEEPINTVL][0][2] == 15


def test_a_missing_platform_option_runs_no_check_and_remembers_nothing(gateway, monkeypatch):
    import coordinator as c
    _reset_keepalive(c, monkeypatch)
    monkeypatch.setenv('ENCODER_TCP_KEEPALIVE_IDLE_S', str(10 ** 12))
    idle_option = socket.TCP_KEEPIDLE
    monkeypatch.delattr(socket, 'TCP_KEEPIDLE')
    _use_probe_sockets(monkeypatch, c, accept=False)
    rows = c.encoder_keepalive_options()
    assert len(rows) == 3 and all(row[1] != idle_option for row in rows)
    assert not [s for s in _ProbeSocket.made if s.options]
    assert not c._keepalive_accepted


def test_the_defaults_are_never_checked_even_when_the_platform_would_reject_them(
        gateway, monkeypatch, caplog):
    import coordinator as c
    _reset_keepalive(c, monkeypatch)
    _use_probe_sockets(monkeypatch, c, accept=False)
    assert [r[2] for r in c.encoder_keepalive_options()] == [1, 60, 15, 4]
    assert not _ProbeSocket.made and not caplog.records
