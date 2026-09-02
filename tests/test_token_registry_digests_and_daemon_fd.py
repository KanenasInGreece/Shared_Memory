"""Credential_Custody_Plan_2026-08-14, PR A2 — token registry digests +
ephemeral daemon tokens.

Covers what test_auth.py (coordinator._load_agent_tokens shape,
auth_middleware end to end) and test_secrets_out_of_process_env.py
(_daemon_env() never carrying AGENT_TOKEN) do not:

  1. secure_env.read_daemon_token_from_fd() — the daemon-side read primitive,
     in isolation: valid fd, unset env var, non-integer env var, a fd number
     that isn't actually open.
  2. hive_mind_proxy._daemon_env_and_token_fd() / _mint_daemon_token() — the
     proxy-side mint + pipe-write primitive: the token is delivered via the
     pipe, never via the env dict; minting registers the token in
     coordinator._AGENT_TOKENS (digest-keyed) so it verifies exactly like
     any other registry entry; re-minting for the same agent revokes the
     previous ephemeral token.
  3. rem_loop.py / consolidation_loop.py actually reading their AGENT_TOKEN
     from the fd at import time (the mainline, proxy-spawned path), and
     preferring it over the get_secret() fallback when both are present.
  4. hive_mind_proxy.main() calling coordinator.require_no_plaintext_agent_
     tokens() as its FIRST act (RULED, Operator 2026-08-14: a plaintext
     AGENT_TOKENS entry refuses gateway startup outright as of v0.9.3 — no
     deprecation window).
"""
import asyncio
import hashlib
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import secure_env  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_secure_env_state(monkeypatch):
    monkeypatch.setattr(secure_env, "_secrets", {})
    monkeypatch.setattr(secure_env, "_dynamic_secret_names", set())
    yield


@pytest.fixture(autouse=True)
def _isolated_process_env():
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


@pytest.fixture(autouse=True)
def _isolated_agent_tokens_registry():
    """coordinator._AGENT_TOKENS is a process-lifetime module global mutated
    directly by hive_mind_proxy._mint_daemon_token() (no reload in between
    for most tests here) — clear it before and after every test in this file
    so ephemeral registrations from one test never leak into the next."""
    import coordinator
    coordinator._AGENT_TOKENS.clear()
    yield
    coordinator._AGENT_TOKENS.clear()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── 1. secure_env.read_daemon_token_from_fd() in isolation ──────────────────

def test_read_daemon_token_from_fd_reads_and_closes(monkeypatch):
    r, w = os.pipe()
    os.write(w, b"my-daemon-token")
    os.close(w)
    monkeypatch.setenv("AGENT_TOKEN_FD", str(r))

    token = secure_env.read_daemon_token_from_fd()

    assert token == "my-daemon-token"
    # The fd must be closed after the read -- a second read raises OSError.
    with pytest.raises(OSError):
        os.read(r, 10)


def test_read_daemon_token_from_fd_unset_env_returns_none(monkeypatch):
    monkeypatch.delenv("AGENT_TOKEN_FD", raising=False)
    assert secure_env.read_daemon_token_from_fd() is None


def test_read_daemon_token_from_fd_non_integer_env_returns_none(monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN_FD", "not-a-number")
    assert secure_env.read_daemon_token_from_fd() is None


def test_read_daemon_token_from_fd_closed_fd_returns_none(monkeypatch):
    r, w = os.pipe()
    os.close(w)
    os.close(r)  # already closed -- nothing to read
    monkeypatch.setenv("AGENT_TOKEN_FD", str(r))
    assert secure_env.read_daemon_token_from_fd() is None


def test_read_daemon_token_from_fd_respects_custom_env_var_name(monkeypatch):
    r, w = os.pipe()
    os.write(w, b"custom-var-token")
    os.close(w)
    monkeypatch.setenv("MY_CUSTOM_FD_VAR", str(r))
    assert secure_env.read_daemon_token_from_fd("MY_CUSTOM_FD_VAR") == "custom-var-token"


# ── 2. hive_mind_proxy: mint + pipe-fd delivery ──────────────────────────────
#
# _daemon_env_and_token_fd() only mints (and hands back a real fd) when auth
# is CONFIGURED at startup (finding 1, A2 security review) -- an auth-unset
# install must not flip to authenticating just because a daemon watchdog
# fired. These two tests exercise the pipe-delivery mechanism itself, which
# only exists when auth is on, so they force that state deterministically
# by reloading `coordinator` with a real AGENT_TOKENS entry first (rather
# than relying on whatever an earlier-imported copy of the module happened
# to be left at), and restore it to unset afterward so later tests in this
# session see the same "auth off" baseline they did before.

def test_daemon_env_and_token_fd_delivers_token_via_pipe_not_env(monkeypatch):
    monkeypatch.setenv("AGENT_TOKENS", f"placeholder:sha256:{_digest('tok_placeholder')}")
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    assert g.AUTH_CONFIGURED_AT_STARTUP is True  # sanity: minting will actually happen

    env, read_fd = g._daemon_env_and_token_fd("consolidation")
    try:
        assert read_fd is not None
        assert "AGENT_TOKEN" not in env, "the raw token must never sit in the child env dict"
        assert env.get("AGENT_TOKEN_FD") == str(read_fd)
        data = os.read(read_fd, 200)
        assert data, "the pipe must carry the minted token's bytes"
        assert len(data) > 20  # token_urlsafe(32) is well over 20 chars
    finally:
        os.close(read_fd)
        monkeypatch.delenv("AGENT_TOKENS", raising=False)
        importlib.reload(coordinator)


def test_mint_daemon_token_registers_in_coordinator_agent_tokens(monkeypatch):
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import hive_mind_proxy as g
    importlib.reload(g)
    import coordinator

    before = len(coordinator._AGENT_TOKENS)
    token = g._mint_daemon_token("rem_daemon")

    assert len(coordinator._AGENT_TOKENS) == before + 1
    assert coordinator._lookup_agent_by_token(token) == "rem_daemon"
    # Never stored in recoverable plaintext form -- only its digest.
    assert token not in coordinator._AGENT_TOKENS
    assert _digest(token) in coordinator._AGENT_TOKENS


def test_mint_daemon_token_revokes_previous_ephemeral_token_on_remint(monkeypatch):
    """A daemon respawn (crash-restart) mints a fresh token -- the OLD one
    must stop verifying, not accumulate as a second permanently-valid
    credential."""
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import hive_mind_proxy as g
    importlib.reload(g)
    import coordinator

    first = g._mint_daemon_token("rem_daemon")
    assert coordinator._lookup_agent_by_token(first) == "rem_daemon"

    second = g._mint_daemon_token("rem_daemon")
    assert coordinator._lookup_agent_by_token(second) == "rem_daemon"
    assert coordinator._lookup_agent_by_token(first) is None, \
        "the previous ephemeral token must be revoked, not left valid"


def test_mint_daemon_token_for_two_agents_does_not_collide(monkeypatch):
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import hive_mind_proxy as g
    importlib.reload(g)
    import coordinator

    rem_token = g._mint_daemon_token("rem_daemon")
    cons_token = g._mint_daemon_token("consolidation")

    assert coordinator._lookup_agent_by_token(rem_token) == "rem_daemon"
    assert coordinator._lookup_agent_by_token(cons_token) == "consolidation"
    assert rem_token != cons_token


def test_daemon_env_and_token_fd_verifies_through_the_real_registry_lookup(monkeypatch):
    """End to end: mint via the proxy helper, read the token back off the
    pipe as the daemon would, and confirm THAT value authenticates through
    coordinator's own lookup -- not a separately-asserted digest.

    Requires auth CONFIGURED at startup (finding 1) -- forced deterministically
    the same way as the sibling test above, and restored to unset afterward."""
    monkeypatch.setenv("AGENT_TOKENS", f"placeholder:sha256:{_digest('tok_placeholder')}")
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    assert g.AUTH_CONFIGURED_AT_STARTUP is True

    env, read_fd = g._daemon_env_and_token_fd("rem_daemon")
    try:
        assert read_fd is not None
        presented = os.read(read_fd, 200).decode("utf-8")
    finally:
        os.close(read_fd)
        monkeypatch.delenv("AGENT_TOKENS", raising=False)

    try:
        assert coordinator._lookup_agent_by_token(presented) == "rem_daemon"
        assert coordinator._lookup_agent_by_token(presented + "x") is None
    finally:
        importlib.reload(coordinator)


# ── 3. rem_loop.py / consolidation_loop.py read from the fd at import time ──

def test_rem_loop_agent_token_reads_from_fd_when_present(monkeypatch):
    # Ensure the module is ALREADY in sys.modules before the pipe exists --
    # `import` on an uncached module executes its top-level code too, and a
    # pipe fd can only be consumed ONCE. If this were the first-ever import
    # of rem_loop in the session, doing `import rem_loop` AFTER setting up
    # the pipe and THEN `importlib.reload()` would execute the module twice,
    # and the second pass would find the fd already closed by the first.
    import rem_loop

    r, w = os.pipe()
    os.write(w, b"tok_from_fd")
    os.close(w)
    monkeypatch.setenv("AGENT_TOKEN_FD", str(r))
    monkeypatch.delenv("AGENT_TOKEN", raising=False)

    importlib.reload(rem_loop)
    try:
        assert rem_loop._AGENT_TOKEN == "tok_from_fd"
    finally:
        monkeypatch.delenv("AGENT_TOKEN_FD", raising=False)
        importlib.reload(rem_loop)  # restore normal module state for later tests


def test_rem_loop_prefers_fd_token_over_file_store_when_both_present(monkeypatch):
    import rem_loop  # see fd-single-consumption note above

    r, w = os.pipe()
    os.write(w, b"tok_from_fd")
    os.close(w)
    monkeypatch.setenv("AGENT_TOKEN_FD", str(r))
    monkeypatch.delenv("AGENT_TOKEN", raising=False)
    secure_env._secrets["AGENT_TOKEN"] = "tok_from_file"

    importlib.reload(rem_loop)
    try:
        assert rem_loop._AGENT_TOKEN == "tok_from_fd"
    finally:
        monkeypatch.delenv("AGENT_TOKEN_FD", raising=False)
        importlib.reload(rem_loop)


def test_consolidation_loop_agent_token_reads_from_fd_when_present(monkeypatch):
    import consolidation_loop as cl  # see fd-single-consumption note above

    r, w = os.pipe()
    os.write(w, b"tok_from_fd_cons")
    os.close(w)
    monkeypatch.setenv("AGENT_TOKEN_FD", str(r))
    monkeypatch.delenv("AGENT_TOKEN", raising=False)

    importlib.reload(cl)
    try:
        assert cl._AGENT_TOKEN == "tok_from_fd_cons"
    finally:
        monkeypatch.delenv("AGENT_TOKEN_FD", raising=False)
        importlib.reload(cl)


def test_rem_loop_falls_back_when_no_fd_and_no_file_value(monkeypatch, tmp_path):
    """Neither the fd nor the file store has a value -- _AGENT_TOKEN must be
    None, not an empty string or an exception (standalone debug run with no
    auth configured at all)."""
    monkeypatch.delenv("AGENT_TOKEN_FD", raising=False)
    monkeypatch.delenv("AGENT_TOKEN", raising=False)
    secure_env._secrets.pop("AGENT_TOKEN", None)
    # The reload below re-triggers rem_loop's own load_split_env() call,
    # which reads a REAL shared-memory/.env straight off disk if one exists
    # on this checkout -- popping _secrets above doesn't stop that re-read.
    # Point secure_env's own candidate resolution at an empty tmp tree so
    # this test's "nothing configured" premise holds regardless of whether
    # a real .env happens to be present (the class of bug CLAUDE.md's
    # "run the suite twice, once with a fake real .env" rule exists to
    # catch -- this test would otherwise pass in isolation and fail only
    # when a real .env is present, exactly the disagreement that rule is
    # written to surface).
    fake_scripts_dir = tmp_path / "shared-memory" / "scripts"
    fake_scripts_dir.mkdir(parents=True)
    monkeypatch.setattr(secure_env, "__file__", str(fake_scripts_dir / "secure_env.py"))

    import rem_loop
    importlib.reload(rem_loop)
    try:
        assert rem_loop._AGENT_TOKEN is None
    finally:
        importlib.reload(rem_loop)


# ── 4. hive_mind_proxy.main() refuses to start with a plaintext registry ────

@pytest.mark.asyncio
async def test_main_refuses_before_anything_else_when_plaintext_present(monkeypatch):
    """RULED (Operator, 2026-08-14): the refusal must be the FIRST thing
    main() does -- confirmed by making the very next call (AsyncHiveMindProxy
    construction) a hard failure if reached, so this test can only pass if
    require_no_plaintext_agent_tokens() raised before that point."""
    monkeypatch.setenv("AGENT_TOKENS", "claude:tok_plaintext")
    monkeypatch.setattr(sys, "argv", ["hive_mind_proxy.py"])  # avoid pytest's own argv
    import hive_mind_proxy as g
    import coordinator
    importlib.reload(coordinator)
    importlib.reload(g)

    def _must_not_be_reached(*a, **kw):
        raise AssertionError("main() proceeded past the plaintext-registry refusal")

    monkeypatch.setattr(g, "AsyncHiveMindProxy", _must_not_be_reached)

    with pytest.raises(SystemExit, match="plaintext"):
        await g.main()


@pytest.mark.asyncio
async def test_main_does_not_refuse_with_digest_only_registry(monkeypatch):
    """The refusal call itself must not be a blanket abort -- a clean
    digest-only registry (or auth disabled) passes it and execution
    continues (proven by reaching, and failing inside, the next step)."""
    monkeypatch.setenv("AGENT_TOKENS", f"claude:sha256:{_digest('tok_abc')}")
    monkeypatch.setattr(sys, "argv", ["hive_mind_proxy.py"])  # avoid pytest's own argv
    import hive_mind_proxy as g
    import coordinator
    importlib.reload(coordinator)
    importlib.reload(g)

    sentinel = RuntimeError("reached past the refusal check, as expected")

    def _raises_sentinel(*a, **kw):
        raise sentinel

    monkeypatch.setattr(g, "AsyncHiveMindProxy", _raises_sentinel)

    with pytest.raises(RuntimeError) as exc_info:
        await g.main()
    assert exc_info.value is sentinel


# ── G (S6): ephemeral daemon-token revoke on watchdog exit + drain ─────────
# No real subprocess and no listening socket anywhere below — `_start_daemon`
# / `_start_rem_daemon` are monkeypatched to fake coroutines returning a
# minimal process double, per the ground rule (no process may bind a
# network port / spawn a real child here).

class _FakeDaemonProc:
    """Minimal double for asyncio.subprocess.Process — just enough for the
    watchdogs' own `proc.wait()` / `.returncode` / `.terminate()` / `.kill()`
    / `.pid` usage."""
    def __init__(self, returncode=None, pid=4242):
        self.returncode = returncode
        self.pid = pid
        self._done = asyncio.Event()
        if returncode is not None:
            self._done.set()

    def terminate(self):
        self.returncode = -15
        self._done.set()

    def kill(self):
        self.returncode = -9
        self._done.set()

    async def wait(self):
        await self._done.wait()
        return self.returncode


@pytest.mark.asyncio
async def test_watchdog_daemon_clean_exit_leaves_no_live_digest(monkeypatch):
    """Prove failing first: before G's try/finally, a clean (returncode 0)
    exit left the just-minted ephemeral digest live in the registry forever
    (nothing on that path ever called _revoke_daemon_token)."""
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)

    async def _fake_start_daemon():
        g._mint_daemon_token(g._CONSOLIDATION_AGENT_NAME)
        return _FakeDaemonProc(returncode=0)

    monkeypatch.setattr(g, "_start_daemon", _fake_start_daemon)
    await g._watchdog_daemon(asyncio.Event())

    assert g._CONSOLIDATION_AGENT_NAME not in g._ephemeral_daemon_token_digests
    assert len(coordinator._AGENT_TOKENS) == 0


@pytest.mark.asyncio
async def test_start_daemon_publishes_daemon_proc_before_returning(monkeypatch):
    """Fix round finding 9 (QA LOW): `_daemon_proc` must be set the instant
    `asyncio.create_subprocess_exec` returns — inside `_start_daemon()`
    itself — not left to the watchdog's own later assignment a few lines
    after its `await _start_daemon()`. Prove-failing-first: calling
    `_start_daemon()` directly (bypassing the watchdog entirely) leaves
    `g._daemon_proc` at its pre-call value on unmodified code, since only
    the WATCHDOG used to set it. A real cancellation delivered while the
    watchdog's own `await _start_daemon()` is still unwinding back up the
    call stack (after the subprocess is already live) used to leave
    `_daemon_proc` unset — the drain's terminate step then skips a daemon
    that is already running, orphaning it holding a token G's own
    watchdog-`finally` had just revoked."""
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)

    fake_proc = _FakeDaemonProc()

    async def _fake_create_subprocess_exec(*a, **kw):
        return fake_proc

    monkeypatch.setattr(g, "_find_uv", lambda: "/usr/bin/true")
    monkeypatch.setattr(g.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    assert g._daemon_proc is None
    proc = await g._start_daemon()
    assert proc is fake_proc
    assert g._daemon_proc is fake_proc


@pytest.mark.asyncio
async def test_start_rem_daemon_publishes_rem_proc_before_returning(monkeypatch):
    """Parity with test_start_daemon_publishes_daemon_proc_before_returning,
    for the REM daemon's `_rem_proc`."""
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)

    fake_proc = _FakeDaemonProc()

    async def _fake_create_subprocess_exec(*a, **kw):
        return fake_proc

    monkeypatch.setattr(g, "_find_uv", lambda: "/usr/bin/true")
    monkeypatch.setattr(g.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    assert g._rem_proc is None
    proc = await g._start_rem_daemon()
    assert proc is fake_proc
    assert g._rem_proc is fake_proc


@pytest.mark.asyncio
async def test_watchdog_rem_daemon_clean_exit_leaves_no_live_digest(monkeypatch):
    """Parity with the consolidation watchdog above, for the REM daemon."""
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)

    async def _fake_start_rem_daemon():
        g._mint_daemon_token(g._REM_DAEMON_AGENT_NAME)
        return _FakeDaemonProc(returncode=0)

    monkeypatch.setattr(g, "_start_rem_daemon", _fake_start_rem_daemon)
    await g._watchdog_rem_daemon(asyncio.Event())

    assert g._REM_DAEMON_AGENT_NAME not in g._ephemeral_daemon_token_digests
    assert len(coordinator._AGENT_TOKENS) == 0


@pytest.mark.asyncio
async def test_watchdog_daemon_circuit_breaker_leaves_no_live_digest(monkeypatch):
    """Prove failing first: the circuit-breaker `break` is a different exit
    path from the clean-exit one above, and before G's fix it ALSO left the
    digest live forever. _DAEMON_MAX_RESTARTS=0 trips on the very first
    crash, with no backoff sleep -- restart_times starts empty and
    len([]) >= 0 is already True."""
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    monkeypatch.setattr(g, "_DAEMON_MAX_RESTARTS", 0)

    async def _fake_start_daemon():
        g._mint_daemon_token(g._CONSOLIDATION_AGENT_NAME)
        return _FakeDaemonProc(returncode=1)  # non-clean exit

    monkeypatch.setattr(g, "_start_daemon", _fake_start_daemon)
    await g._watchdog_daemon(asyncio.Event())

    assert g._CONSOLIDATION_AGENT_NAME not in g._ephemeral_daemon_token_digests
    assert len(coordinator._AGENT_TOKENS) == 0


@pytest.mark.asyncio
async def test_watchdog_daemon_exactly_one_live_digest_mid_respawn_regression_guard(monkeypatch):
    """REGRESSION GUARD (ADV1-18): this already held before G — re-minting
    for the same agent pops the previous digest first (_mint_daemon_token,
    :2306-2308-area) — this test pins it THROUGH the watchdog loop, with
    G's own revoke logic present. The observation point is inside the
    SECOND FakeProc's own `wait()` — i.e. AFTER `_start_daemon()` has
    already returned and the watchdog has already run its own
    `_daemon_proc = proc; _daemon_healthy = True` lines — so a mutation
    that inserted an extra `_revoke_daemon_token()` call anywhere in the
    loop body between `proc = await _start_daemon()` and `await
    proc.wait()` (the shape ADV1-18 warns about: "moving the revoke inside
    the respawn loop") would revoke the digest the SECOND mint just
    registered before this observation runs, and the assertion below would
    see zero live digests instead of exactly one. Mutation-checked
    (recorded in HANDOFF): inserting
    `_revoke_daemon_token(_CONSOLIDATION_AGENT_NAME)` immediately after
    `proc = await _start_daemon()` inside _watchdog_daemon's loop makes
    this fail."""
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    monkeypatch.setattr(g, "_DAEMON_MAX_RESTARTS", 100)  # never trips in this test

    stop_event = asyncio.Event()
    call_count = 0
    observed = {}

    class _ObservingFakeProc(_FakeDaemonProc):
        """Crashes (returncode=1) like a normal respawn trigger, but
        records the live-digest count the moment the watchdog starts
        awaiting it — i.e. strictly AFTER every line the loop body runs
        between `_start_daemon()` returning and this await."""
        def __init__(self, token):
            super().__init__(returncode=1)
            self._token = token

        async def wait(self):
            observed["live_digest_count"] = len(coordinator._AGENT_TOKENS)
            observed["second_token_verifies"] = (
                coordinator._lookup_agent_by_token(self._token) == g._CONSOLIDATION_AGENT_NAME)
            return await super().wait()

    async def _fake_start_daemon():
        nonlocal call_count
        call_count += 1
        token = g._mint_daemon_token(g._CONSOLIDATION_AGENT_NAME)
        if call_count == 2:
            stop_event.set()
            return _ObservingFakeProc(token)
        return _FakeDaemonProc(returncode=1)  # crash -> triggers a respawn

    monkeypatch.setattr(g, "_start_daemon", _fake_start_daemon)
    await g._watchdog_daemon(stop_event)

    assert observed["live_digest_count"] == 1, (
        "exactly one live digest expected mid-respawn — the previous "
        "ephemeral token must already be popped by the SECOND mint, not by "
        "an out-of-band revoke"
    )
    assert observed["second_token_verifies"] is True
    assert call_count == 2


@pytest.mark.asyncio
async def test_drain_watchdogs_and_daemons_terminates_before_cancelling(monkeypatch):
    """Drain order pinned (ADV1-13): a spying _revoke_daemon_token records
    each daemon's OWN returncode at the moment it is called — every
    observation must show a returncode already set (i.e. terminate() ran
    first). MUTATION CHECK (recorded in HANDOFF): swapping
    _drain_watchdogs_and_daemons() to cancel the watchdog tasks BEFORE
    terminating the daemon processes makes this fail — the cancelled
    watchdog's own finally-revoke fires while its FakeProc's returncode is
    still None (still "alive")."""
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)

    cons_proc = _FakeDaemonProc(returncode=None, pid=111)
    rem_proc  = _FakeDaemonProc(returncode=None, pid=222)

    async def _fake_start_daemon():
        g._mint_daemon_token(g._CONSOLIDATION_AGENT_NAME)
        return cons_proc

    async def _fake_start_rem_daemon():
        g._mint_daemon_token(g._REM_DAEMON_AGENT_NAME)
        return rem_proc

    monkeypatch.setattr(g, "_start_daemon", _fake_start_daemon)
    monkeypatch.setattr(g, "_start_rem_daemon", _fake_start_rem_daemon)

    observations = []
    _real_revoke = g._revoke_daemon_token

    def _spying_revoke(agent_name):
        observations.append((agent_name, cons_proc.returncode, rem_proc.returncode))
        return _real_revoke(agent_name)

    monkeypatch.setattr(g, "_revoke_daemon_token", _spying_revoke)

    stop_event = asyncio.Event()
    watchdog_task = asyncio.create_task(g._watchdog_daemon(stop_event))
    rem_watchdog_task = asyncio.create_task(g._watchdog_rem_daemon(stop_event))

    # Let both watchdogs run past their mint and reach the blocking
    # `await proc.wait()` on their respective FakeProc.
    for _ in range(10):
        await asyncio.sleep(0)

    stop_event.set()  # mirrors _on_shutdown_signal(), which always runs
                       # before the drain sequence in main()
    monkeypatch.setattr(g, "_daemon_proc", cons_proc)
    monkeypatch.setattr(g, "_rem_proc", rem_proc)

    await g._drain_watchdogs_and_daemons(watchdog_task, rem_watchdog_task, ())

    assert observations, "no revoke was observed at all"
    for agent_name, cons_rc, rem_rc in observations:
        if agent_name == g._CONSOLIDATION_AGENT_NAME:
            assert cons_rc is not None, (
                "consolidation daemon's token was revoked BEFORE its "
                "process was terminated — drain order violated")
        if agent_name == g._REM_DAEMON_AGENT_NAME:
            assert rem_rc is not None, (
                "REM daemon's token was revoked BEFORE its process was "
                "terminated — drain order violated")
    assert g._CONSOLIDATION_AGENT_NAME not in g._ephemeral_daemon_token_digests
    assert g._REM_DAEMON_AGENT_NAME not in g._ephemeral_daemon_token_digests
    assert len(coordinator._AGENT_TOKENS) == 0


@pytest.mark.asyncio
async def test_drain_watchdogs_and_daemons_is_an_idempotent_backstop(monkeypatch):
    """ADV2-9: calling the backstop revoke when the watchdogs' own finally
    already cleared everything must not raise and must not resurrect a
    stale entry — pop-with-default is a no-op the second time."""
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)

    async def _noop_task():
        return None

    t1 = asyncio.create_task(_noop_task())
    t2 = asyncio.create_task(_noop_task())
    await g._drain_watchdogs_and_daemons(t1, t2, ())  # no daemons ever started
    assert len(coordinator._AGENT_TOKENS) == 0  # no exception, nothing to revoke
