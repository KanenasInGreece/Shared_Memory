"""Session-wide safety net (security review R-1, PR A3 fix round).

No test in this suite may ever write to the real ``$HOME``. An autouse,
function-scoped fixture points ``CREDENTIAL_AUDIT_LOG_PATH`` at a per-test
``tmp_path`` before every single test runs — so a test that forgets to manage
the variable explicitly, or one that reloads ``coordinator`` after deleting
it (re-arming the module-level default), still lands somewhere disposable
rather than in the operator's real credential-audit log.

This is a BACKSTOP, not the only line of defence: ``tests/test_credential_
audit_trail.py``'s own ``load_coordinator()`` also defaults to an explicit
disabled state rather than "pop the variable", and ``tests/test_llm_fault_
origin.py``'s isolation fixture resets the shared ``coordinator`` module's
writer at teardown regardless of what an individual test did to it. Belt and
braces: any one of the three would have caught the contamination the review
found (138 synthetic lines in the real ``~/.shared-memory/logs/credential-
audit.jsonl`` from a suite run with no explicit protection at all).
"""
import os

import pytest

# Hermeticity pin (module level, BEFORE any test module imports a daemon):
# the suite must never read the deployer's live shared-memory/.env. The
# secure_env loader re-populates os.environ from that file on every module
# reload, and a test can defeat it only by SETTING a key — never by deleting
# one, because setdefault re-adds what delenv removed. Measured cost of not
# having this: a deployer whose .env used the documented LLM_BACKENDS_JSON
# form got 15 failures from tests that delenv'd the variable and were
# silently overruled by their own config file. Empty string = "load no env
# file at all" (see secure_env._select_env_file).
os.environ["SECURE_ENV_FILE"] = ""
# An unset COORDINATOR_UDS auto-detects $XDG_RUNTIME_DIR/shared-memory-gw.sock.
# A live socket on this host turns a ConnectError into the socket message, and
# test_s6_cli_read_timeout then fails. Empty means do not auto-detect.
os.environ["COORDINATOR_UDS"] = ""


def _arm_chat_identity(module) -> None:
    """Treat an unidentified chat caller as the consolidation daemon.

    Only the chat_caller_is_consolidation fixture calls this. A test that
    does not request it sees the real anonymous refusal.
    """
    cls = getattr(module, "AsyncHiveMindProxy", None)
    if cls is None:
        return
    handle = cls.handle_proxy
    if getattr(handle, "_v1041_armed", False):
        return

    async def _wrapped(self, request, _handle=handle, _module=module):
        real = _module._safe_resolve_identity

        def _as_daemon(req):
            name = real(req)
            return name if name is not None else "consolidation"

        _module._safe_resolve_identity = _as_daemon
        try:
            return await _handle(self, request)
        finally:
            _module._safe_resolve_identity = real

    _wrapped._v1041_armed = True
    _wrapped._original = handle
    cls.handle_proxy = _wrapped


@pytest.fixture
def chat_caller_is_consolidation(monkeypatch):
    """Opt in when a chat call with no bearer must be forwarded as consolidation.

    importlib.reload stays the real function. This fixture patches it only
    for the test that requests the fixture, and puts the original handler
    back afterwards. A test that does not request it can see an anonymous forward refused.
    """
    import importlib
    real_reload = importlib.reload

    def _reload(module):
        result = real_reload(module)
        if str(getattr(module, "__name__", "")).endswith("hive_mind_proxy"):
            _arm_chat_identity(module)
        return result

    monkeypatch.setattr(importlib, "reload", _reload)
    try:
        import hive_mind_proxy as gateway
    except Exception:
        gateway = None
    if gateway is not None:
        _arm_chat_identity(gateway)
    yield
    if gateway is not None:
        try:
            import hive_mind_proxy as live
        except Exception:
            live = None
        if live is not None:
            current = live.AsyncHiveMindProxy.handle_proxy
            if getattr(current, "_v1041_armed", False):
                live.AsyncHiveMindProxy.handle_proxy = current._original


@pytest.fixture(autouse=True)
def _credential_audit_log_path_never_touches_real_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_AUDIT_LOG_PATH", str(tmp_path / "credential-audit.jsonl"))
    yield


@pytest.fixture(autouse=True)
def _hermetic_config_home_and_systemctl(tmp_path, monkeypatch):
    """A forgotten test must not write the real ~/.config, and must not read the live gateway unit.

    fastmcp's version cache is a third-party write. This fixture does not move it.
    """
    xdg = tmp_path / "xdg-config"
    xdg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    logs = tmp_path / "memory-logs"
    logs.mkdir()
    monkeypatch.setenv("MEMORY_LOG_PATH", str(logs))
    bindir = tmp_path / "hermetic-bin"
    bindir.mkdir()
    stub = bindir / "systemctl"
    stub.write_text("#!/bin/sh\nprintf '%s\\n' 'LoadState=not-found' 'RestartPreventExitStatus='\nexit 0\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    yield


@pytest.fixture(autouse=True)
def _capacity_log_path_never_touches_real_home(tmp_path, monkeypatch):
    """Same backstop as the credential-audit fixture above, for R0-I's
    capacity-derivation log (default ~/.shared-memory/capacity/
    derivations.jsonl): any test that reloads hive_mind_proxy without
    explicitly setting CAPACITY_LOG_PATH still lands in a disposable
    tmp_path rather than reading/writing the operator's real log."""
    monkeypatch.setenv("CAPACITY_LOG_PATH", str(tmp_path / "capacity-derivations.jsonl"))
    yield


@pytest.fixture(autouse=True)
def _guard_live_postgres_connections(monkeypatch):
    """Live-store guard: test harness enforces isolation from live postgres.

    The autouse fixture RECORDS every asyncpg.connect / asyncpg.create_pool
    attempt not overridden by the test and FAILS in teardown if any happened
    (an assertion inside connect would be swallowed by the lock_connection_failed
    path). Tests that need a connection override it with their own mocks.
    """
    import asyncpg
    recorded_attempts = []

    async def _fail_connect(*args, **kwargs):
        recorded_attempts.append(("connect", args, kwargs))
        raise RuntimeError("test attempted a live Postgres connection (connect)")

    async def _fail_create_pool(*args, **kwargs):
        recorded_attempts.append(("create_pool", args, kwargs))
        raise RuntimeError("test attempted a live Postgres connection (create_pool)")

    monkeypatch.setattr(asyncpg, "connect", _fail_connect)
    monkeypatch.setattr(asyncpg, "create_pool", _fail_create_pool)

    yield

    if recorded_attempts:
        raise AssertionError(
            f"test attempted an unmocked live Postgres connection: {recorded_attempts}"
        )
