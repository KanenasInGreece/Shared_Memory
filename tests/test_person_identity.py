"""
Person-axis (principal) enforcement tests.

The principal is the OS account behind the connection, read from the kernel via
SO_PEERCRED — it is NEVER carried in the request or inferred from the agent. These
tests pin the two guarantees that matter:

  1. _peer_identity reads the real OS user over an AF_UNIX socket, and returns None
     on any non-UDS transport (no guessing).
  2. _apply_principal strips any client-supplied person-axis fields and re-stamps the
     kernel-attested value — so an agent told to "save as someone else" cannot move
     it; and with no kernel credential the fields stay absent (honestly unknown).
  3. GATEWAY_REQUIRE_PRINCIPAL, when set, rejects writes that lack a kernel-attested
     principal.
"""

import importlib.util
import os
import pwd
import socket
import sys
from unittest.mock import MagicMock

import pytest


def load_coordinator(agent_tokens: str = "", require_principal: str = "") :
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    if agent_tokens:
        os.environ["AGENT_TOKENS"] = agent_tokens
    else:
        os.environ.pop("AGENT_TOKENS", None)
    if require_principal:
        os.environ["GATEWAY_REQUIRE_PRINCIPAL"] = require_principal
    else:
        os.environ.pop("GATEWAY_REQUIRE_PRINCIPAL", None)
    path = os.path.join(scripts_dir, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator_person_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeTransport:
    def __init__(self, sock):
        self._sock = sock

    def get_extra_info(self, key):
        return self._sock if key == "socket" else None


def _req_with_socket(sock):
    req = MagicMock()
    req.transport = _FakeTransport(sock) if sock is not None else None
    return req


# ── _peer_identity ────────────────────────────────────────────────────────────

def test_peer_identity_reads_os_user_over_unix_socket():
    """SO_PEERCRED over an AF_UNIX socketpair yields this process's own OS account."""
    mod = load_coordinator()
    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        ident = mod._peer_identity(_req_with_socket(a))
    finally:
        a.close(); b.close()
    assert ident is not None
    assert ident["uid"] == os.getuid()
    assert ident["gid"] == os.getgid()
    assert ident["pid"] == os.getpid()
    assert ident["user"] == pwd.getpwuid(os.getuid()).pw_name


def test_peer_identity_none_on_tcp_socket():
    """A TCP (AF_INET) connection has no kernel peer credential — principal unknown."""
    mod = load_coordinator()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        assert mod._peer_identity(_req_with_socket(s)) is None
    finally:
        s.close()


def test_peer_identity_none_when_no_transport():
    mod = load_coordinator()
    assert mod._peer_identity(_req_with_socket(None)) is None


# ── _apply_principal (the deterministic strip-and-stamp) ────────────────────────

def test_apply_principal_strips_client_claim_and_stamps_verified():
    """An agent instructed to 'save as someone else' cannot move the principal:
    its claimed fields are dropped and the kernel-attested user is stamped."""
    mod = load_coordinator()
    meta = {
        "source": "claude",
        "principal": "ceo",                        # forged claim
        "connected_from": {"uid": 0, "user": "root"},  # forged claim
        "entities": ["X"],
    }
    verified = {"user": "alice", "uid": 1001, "gid": 1001, "pid": 42,
                "login_uid": 1001, "login_user": "alice", "session": "7"}
    out = mod._apply_principal(meta, verified)
    assert out["principal"] == "alice"
    assert out["connected_from"]["uid"] == 1001
    assert out["connected_from"]["login_user"] == "alice"
    assert out["connected_from"]["session"] == "7"
    # the forged values are gone, the legitimate metadata survives
    assert out["entities"] == ["X"]
    assert "root" not in str(out["connected_from"])


def test_apply_principal_none_strips_claim_and_leaves_absent():
    """With no kernel credential (TCP) a client claim is still stripped, and nothing
    is invented — the person fields are simply absent."""
    mod = load_coordinator()
    meta = {"source": "claude", "principal": "ceo", "connected_from": {"uid": 0}}
    out = mod._apply_principal(meta, None)
    assert "principal" not in out
    assert "connected_from" not in out
    assert out["source"] == "claude"


# ── GATEWAY_REQUIRE_PRINCIPAL gate ──────────────────────────────────────────────

def _make_request(path, auth_header, method="POST"):
    req = MagicMock()
    req.path = path
    req.method = method
    req.headers = {"Authorization": auth_header} if auth_header else {}
    req.get = MagicMock(return_value=None)
    req.__setitem__ = MagicMock()
    req.transport = None  # non-UDS → no kernel principal
    return req


async def _noop_handler(request):
    from aiohttp import web
    return web.json_response({"ok": True})


@pytest.mark.asyncio
async def test_require_principal_rejects_write_without_kernel_principal():
    from aiohttp.web_exceptions import HTTPForbidden
    mod = load_coordinator("claude:tok_abc", require_principal="1")
    req = _make_request("/memory/save", "Bearer tok_abc")
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req, _noop_handler)


@pytest.mark.asyncio
async def test_require_principal_off_allows_tcp_write():
    """Default (flag unset): a TCP write still succeeds, recorded with no principal."""
    mod = load_coordinator("claude:tok_abc")
    req = _make_request("/memory/save", "Bearer tok_abc")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200
