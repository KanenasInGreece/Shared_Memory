"""check_config reports a keepalive value invalid exactly where the gateway falls back.

These tests measure the platform with their own socket rather than a literal
ceiling and check that both consumers agree through keepalive_probe.py.
"""
import ast
import importlib
import inspect
import socket
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'shared-memory' / 'scripts'
sys.path.insert(0, str(SCRIPTS))

import check_config  # noqa: E402
import keepalive_probe  # noqa: E402

SETTINGS = [
    ('ENCODER_TCP_KEEPALIVE_IDLE_S', 'TCP_KEEPIDLE', 60),
    ('ENCODER_TCP_KEEPALIVE_INTERVAL_S', 'TCP_KEEPINTVL', 15),
    ('ENCODER_TCP_KEEPALIVE_COUNT', 'TCP_KEEPCNT', 4),
]
ENVS = [s[0] for s in SETTINGS]
IDLE = 'ENCODER_TCP_KEEPALIVE_IDLE_S'


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    import secure_env
    monkeypatch.setattr(secure_env, 'load_split_env', lambda: None)
    monkeypatch.setenv(
        'LLM_BACKENDS_JSON',
        '[{"url":"http://localhost:5000","n_ctx":8192,"max_output_tokens":2048}]')
    monkeypatch.setenv('CAPACITY_LOG_PATH', str(tmp_path / 'capacity.jsonl'))
    import coordinator
    return importlib.reload(coordinator)


def _clear_env(monkeypatch):
    for name in ENVS:
        monkeypatch.delenv(name, raising=False)


def _need(attr):
    if not hasattr(socket, attr):
        pytest.skip(f'this platform has no {attr}')


def _accepts(attr, value):
    """Probe setsockopt on a fresh socket directly, so the tests check keepalive_probe
    against an independent measurement."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.IPPROTO_TCP, getattr(socket, attr), value)
        return True
    except (OSError, TypeError, OverflowError):
        return False
    finally:
        probe.close()


def _ceiling(attr):
    """Return the largest accepted value below 2**31 by bisection, or None if the
    platform accepts 2**31 - 1."""
    lo, hi = 1, 2 ** 31 - 1
    if _accepts(attr, hi):
        return None
    while hi - lo > 1:
        mid = (lo + hi) // 2
        lo, hi = (mid, hi) if _accepts(attr, mid) else (lo, mid)
    return lo


def _effective(env, raw, monkeypatch):
    monkeypatch.setenv(env, raw)
    return check_config._verdict(env)


# check_config reports a value the platform rejects as invalid and keeps one it accepts.

@pytest.mark.parametrize('env, attr, default', SETTINGS)
def test_a_value_the_platform_rejects_is_reported_invalid(
        monkeypatch, env, attr, default):
    _need(attr)
    _clear_env(monkeypatch)
    candidates = [str(10 ** 12)]  # 10**12 makes setsockopt raise TypeError.
    ceiling = _ceiling(attr)
    if ceiling is not None:
        # One above the ceiling makes setsockopt raise OSError.
        candidates.append(str(ceiling + 1))
    for raw in candidates:
        assert not _accepts(attr, int(raw))
        v = _effective(env, raw, monkeypatch)
        assert v['effective'] == default, raw
        assert v['note'] == f'invalid — the gateway uses {default}', raw


@pytest.mark.parametrize('env, attr, default', SETTINGS)
def test_a_value_the_platform_accepts_is_kept_without_a_note(
        monkeypatch, env, attr, default):
    _need(attr)
    _clear_env(monkeypatch)
    ceiling = _ceiling(attr)
    value = ceiling if ceiling is not None else 2 ** 31 - 1
    if value == default:
        value -= 1
    assert _accepts(attr, value)
    v = _effective(env, str(value), monkeypatch)
    assert v['effective'] == value and v['note'] == ''


# For any input, check_config and the gateway must resolve the same effective value.

@pytest.mark.parametrize('env, attr, default', SETTINGS)
def test_check_config_and_the_gateway_decide_the_same_for_every_input(
        gateway, monkeypatch, env, attr, default):
    _need(attr)
    c = gateway
    ceiling = _ceiling(attr)
    raws = [None, '   ', '60', ' 60 ', '+60', '6_0', '60.0', '1e3', '', '0', '-5',
            'abc', '٦٠', '9' * 5000, str(10 ** 12), '1', str(default), str(default + 1)]
    if ceiling is not None:
        raws += [str(ceiling), str(ceiling + 1)]
    option_id = getattr(socket, attr)
    for raw in raws:
        _clear_env(monkeypatch)
        c._keepalive_warned.clear()
        c._keepalive_accepted.clear()
        if raw is not None:
            monkeypatch.setenv(env, raw)
        applied = [r[2] for r in c.encoder_keepalive_options() if r[1] == option_id]
        assert applied == [check_config._verdict(env)['effective']], repr(raw)


@pytest.mark.parametrize('env, attr, default', SETTINGS)
def test_both_sides_omit_an_option_the_platform_lacks(
        gateway, monkeypatch, env, attr, default):
    _need(attr)
    option_id = getattr(socket, attr)
    monkeypatch.delattr(socket, attr)
    for raw in [None, str(default), 'abc', str(default + 1)]:
        _clear_env(monkeypatch)
        if raw is not None:
            monkeypatch.setenv(env, raw)
        rows = gateway.encoder_keepalive_options()
        assert [r for r in rows if r[1] == option_id] == []
        assert f'no {attr}' in check_config._verdict(env)['note'], repr(raw)


# When the platform lacks a socket constant, the gateway omits that option for every
# value, so check_config adds a note whatever the value is.

MISSING = 'this platform has no TCP_KEEPIDLE; the gateway does not set it'


@pytest.mark.parametrize('raw, effective, note', [
    (None, 60, MISSING),
    ('60', 60, MISSING),
    (' 60 ', 60, MISSING),
    ('+60', 60, MISSING),
    ('', 60, f'invalid — the gateway uses 60; {MISSING}'),
    ('0', 60, f'invalid — the gateway uses 60; {MISSING}'),
    ('abc', 60, f'invalid — the gateway uses 60; {MISSING}'),
    ('61', 61, MISSING),
])
def test_a_missing_platform_constant_is_noted_for_every_value(
        monkeypatch, raw, effective, note):
    monkeypatch.delattr(socket, 'TCP_KEEPIDLE', raising=False)
    _clear_env(monkeypatch)
    if raw is not None:
        monkeypatch.setenv(IDLE, raw)
    v = check_config._verdict(IDLE)
    assert v['effective'] == effective and v['note'] == note


# These tests exercise the probe helpers directly and through their callers.

def test_the_sentinel_is_never_read_as_accepted(monkeypatch):
    assert bool(keepalive_probe.NO_SUCH_OPTION) is False
    monkeypatch.delattr(socket, 'TCP_KEEPIDLE', raising=False)
    got = keepalive_probe.platform_accepts('IPPROTO_TCP', 'TCP_KEEPIDLE', 61)
    assert got is keepalive_probe.NO_SUCH_OPTION
    assert keepalive_probe.option_exists('IPPROTO_TCP', 'TCP_KEEPIDLE') is False


def test_an_undecidable_probe_keeps_the_value_with_no_note(monkeypatch):
    def refuse(*a, **k):
        raise OSError('no descriptors')
    monkeypatch.setattr(keepalive_probe.socket, 'socket', refuse)
    got = keepalive_probe.platform_accepts('IPPROTO_TCP', 'TCP_KEEPIDLE', 10 ** 12)
    assert got is None
    _clear_env(monkeypatch)
    v = _effective(IDLE, str(10 ** 12), monkeypatch)
    assert v['effective'] == 10 ** 12 and v['note'] == ''


def test_the_gateway_wrapper_returns_none_and_remembers_nothing(gateway, monkeypatch):
    def refuse(*a, **k):
        raise OSError('no descriptors')
    gateway._keepalive_accepted.clear()
    monkeypatch.setattr(keepalive_probe.socket, 'socket', refuse)
    got = gateway._kernel_accepts(
        socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 'TCP_KEEPIDLE', 61)
    assert got is None
    assert not gateway._keepalive_accepted


@pytest.mark.parametrize('raw', [None, '60', ' 60 ', '+60'])
def test_a_parsed_default_is_never_probed(monkeypatch, raw):
    _clear_env(monkeypatch)
    calls = []
    monkeypatch.setattr(check_config, 'platform_accepts', lambda *a: calls.append(a))
    if raw is not None:
        monkeypatch.setenv(IDLE, raw)
    v = check_config._verdict(IDLE)
    assert calls == [] and v['effective'] == 60 and v['note'] == ''
    monkeypatch.setenv(IDLE, '61')
    check_config._verdict(IDLE)
    assert len(calls) == 1


@pytest.mark.parametrize('rejects', [False, True])
def test_the_throwaway_socket_is_always_closed(monkeypatch, rejects):
    made = []

    class Sock:
        def __init__(self, *a):
            self.closed = False
            made.append(self)

        def setsockopt(self, *a):
            if rejects:
                raise OSError('rejected')

        def close(self):
            self.closed = True

    monkeypatch.setattr(keepalive_probe.socket, 'socket', Sock)
    got = keepalive_probe.platform_accepts('IPPROTO_TCP', 'TCP_KEEPIDLE', 61)
    assert got is (not rejects)
    assert len(made) == 1 and made[0].closed


def test_the_probe_keeps_no_memo(monkeypatch):
    calls = []

    class Sock:
        def __init__(self, *a):
            pass

        def setsockopt(self, *a):
            calls.append(a)

        def close(self):
            pass

    monkeypatch.setattr(keepalive_probe.socket, 'socket', Sock)
    for _ in range(3):
        keepalive_probe.platform_accepts('IPPROTO_TCP', 'TCP_KEEPIDLE', 61)
    assert len(calls) == 3


def test_the_probe_module_is_stdlib_only_has_no_state_and_no_warning():
    tree = ast.parse(inspect.getsource(keepalive_probe))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            raise AssertionError(f'`from {node.module} import` escapes patches')
        if isinstance(node, ast.Import):
            assert [a.name for a in node.names] == ['socket']
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets)
        elif isinstance(node, ast.AnnAssign):
            names.add(node.target.id)
    assert names == {'KEEPALIVE_OPTIONS', 'NO_SUCH_OPTION'}


def test_check_config_never_imports_the_coordinator_at_module_scope():
    tree = ast.parse(inspect.getsource(check_config))
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split('.')[0])
    assert 'keepalive_probe' in names
    assert not names & {'coordinator', 'hive_mind_proxy'}


def test_an_invalid_keepalive_value_never_changes_the_exit_code(monkeypatch, capsys):
    monkeypatch.setenv('SECURE_ENV_FILE', '')
    monkeypatch.setenv('ENCODER_TCP_KEEPALIVE_COUNT', str(10 ** 12))
    assert check_config.main(['--phase-a-only']) == 0
    assert 'the gateway uses 4' in capsys.readouterr().out
