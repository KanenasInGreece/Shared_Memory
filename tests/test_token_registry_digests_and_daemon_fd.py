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
     tokens() as its FIRST act (RULED, Xenofon 2026-08-14: a plaintext
     AGENT_TOKENS entry refuses gateway startup outright as of v0.9.3 — no
     deprecation window).
"""
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
    """RULED (Xenofon, 2026-08-14): the refusal must be the FIRST thing
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
