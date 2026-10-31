"""The chat route serves spawned daemons. Each test dies on the failure it names."""
import asyncio
import hashlib
import importlib
import json
import os
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

SCRIPTS = Path(__file__).resolve().parents[1] / "shared-memory" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

BACKEND = "http://a:5000"


def _reload(monkeypatch, *, tokens=""):
    monkeypatch.setenv(
        "LLM_BACKENDS_JSON",
        json.dumps([{"url": BACKEND, "n_ctx": 8192, "max_output_tokens": 2048}]),
    )
    if tokens:
        monkeypatch.setenv("AGENT_TOKENS", tokens)
    else:
        monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    return coordinator, importlib.reload(g)


def _register(coordinator, token, name):
    digest = hashlib.sha256(token.encode()).hexdigest()
    coordinator._AGENT_TOKENS[digest] = name
    return token


class _Session:
    closed = False

    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        raise RuntimeError("upstream")


def _req(g, method, path, *, token=None, body=b'{"messages":[{"role":"user","content":"hi"}]}'):
    class _R(dict):
        pass
    request = _R()
    request.method = method
    request.rel_url = URL(path, encoded=True)
    request.path = request.rel_url.path
    request.headers = {"Authorization": f"Bearer {token}"} if token else {}
    request.can_read_body = True
    request.content_length = len(body)

    async def read():
        return body
    request.read = read
    return request


def test_anonymous_chat_is_refused_without_the_identity_fixture(monkeypatch):
    """No chat_caller_is_consolidation fixture: an unidentified caller is not rewritten to consolidation."""
    _c, g = _reload(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _Session()
    resp = asyncio.run(proxy.handle_proxy(_req(g, "POST", "/v1/chat/completions")))
    assert resp.status == 403
    assert json.loads(resp.body)["error"] == "framework_only"
    assert proxy.session.calls == []


def test_f3_non_daemon_is_refused_and_a_daemon_is_forwarded(monkeypatch):
    os.environ["SM_TEST_REAL_CHAT_IDENTITY"] = "1"
    try:
        c, g = _reload(monkeypatch, tokens="claude:sha256:" + "ab" * 32)
        _register(c, "outsider", "claude")
        _register(c, "daemon-tok", "consolidation")
        proxy = g.AsyncHiveMindProxy()
        proxy.session = _Session()
        resp = asyncio.run(proxy.handle_proxy(_req(g, "POST", "/v1/chat/completions", token="outsider")))
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "framework_only"
        assert resp.headers["X-SM-Fault-Origin"] == "gateway"
        assert proxy.session.calls == []

        proxy.session = _Session()
        forwarded = asyncio.run(proxy.handle_proxy(_req(g, "POST", "/v1/chat/completions", token="daemon-tok")))
        assert proxy.session.calls
        assert forwarded.status == 500
    finally:
        os.environ.pop("SM_TEST_REAL_CHAT_IDENTITY", None)


def test_f3_unknown_path_is_404_for_a_write_token_and_401_without_one(monkeypatch):
    os.environ["SM_TEST_REAL_CHAT_IDENTITY"] = "1"
    try:
        c, g = _reload(monkeypatch, tokens="claude:plaintext-not-used")
        _register(c, "writer", "claude")
        proxy = g.AsyncHiveMindProxy()
        proxy.session = _Session()
        app = web.Application(middlewares=[c.auth_middleware])
        g.attach_coordinator(app, __import__("unittest").mock.AsyncMock())
        app.router.add_post("/v1/chat/completions", proxy.handle_proxy)

        async def probe():
            server = TestServer(app)
            client = TestClient(server)
            await client.start_server()
            try:
                out = []
                for token in ("writer", None):
                    headers = {"Authorization": f"Bearer {token}"} if token else {}
                    resp = await client.post("/memory/not-a-route", headers=headers, data=b"{}")
                    body = await resp.read()
                    out.append((resp.status, dict(resp.headers), body))
                return out
            finally:
                await client.close()

        (status, headers, _body), (status2, headers2, _body2) = asyncio.run(probe())
        assert status == 404
        assert headers.get("X-SM-Fault-Origin") == "gateway"
        assert proxy.session.calls == []
        assert status2 == 401
        assert headers2.get("X-SM-Fault-Origin") == "gateway"
    finally:
        os.environ.pop("SM_TEST_REAL_CHAT_IDENTITY", None)


@pytest.mark.parametrize("spelling", ["/v1/chat/completion%73", "/v1/chat/completions?a=b"])
def test_f3_encoded_and_query_spellings_are_refused_before_upstream(monkeypatch, spelling):
    os.environ["SM_TEST_REAL_CHAT_IDENTITY"] = "1"
    try:
        c, g = _reload(monkeypatch)
        _register(c, "daemon-tok", "consolidation")
        proxy = g.AsyncHiveMindProxy()
        proxy.session = _Session()
        resp = asyncio.run(proxy.handle_proxy(_req(g, "POST", spelling, token="daemon-tok")))
        assert resp.status == 403
        assert proxy.session.calls == []
    finally:
        os.environ.pop("SM_TEST_REAL_CHAT_IDENTITY", None)


def test_f4_and_f10_auth_off_only_a_spawned_daemon_passes(monkeypatch):
    os.environ["SM_TEST_REAL_CHAT_IDENTITY"] = "1"
    try:
        monkeypatch.setenv("LLM_FRAMEWORK_AGENT_NAMES", "manual")
        c, g = _reload(monkeypatch)
        assert c.AUTH_CONFIGURED_AT_STARTUP is False
        env, read_fd = g._daemon_env_and_token_fd("consolidation")
        try:
            os.close(read_fd)
        except OSError:
            pass
        assert c.AUTH_CONFIGURED_AT_STARTUP is False
        digest = g._ephemeral_daemon_token_digests["consolidation"]
        # The minted value is not asserted. The registered name is.
        assert c._AGENT_TOKENS[digest] == "consolidation"
        # Recover the token only from the pipe we already closed. Re-mint is the
        # registration the chat route checks; present that digest's bearer by
        # minting once more and keeping the returned string in this test only.
        g._revoke_daemon_token("consolidation")
        held = g._mint_daemon_token("consolidation")
        assert c.AUTH_CONFIGURED_AT_STARTUP is False

        proxy = g.AsyncHiveMindProxy()
        proxy.session = _Session()
        forwarded = asyncio.run(proxy.handle_proxy(_req(g, "POST", "/v1/chat/completions", token=held)))
        assert proxy.session.calls
        assert forwarded.status == 500

        proxy.session = _Session()
        anon = asyncio.run(proxy.handle_proxy(_req(g, "POST", "/v1/chat/completions")))
        assert anon.status == 403
        assert json.loads(anon.body)["error"] == "framework_only"
        assert proxy.session.calls == []

        _register(c, "manual-tok", "manual")
        proxy.session = _Session()
        manual = asyncio.run(proxy.handle_proxy(_req(g, "POST", "/v1/chat/completions", token="manual-tok")))
        assert manual.status == 403
        assert proxy.session.calls == []
        g._revoke_daemon_token("consolidation")
    finally:
        os.environ.pop("SM_TEST_REAL_CHAT_IDENTITY", None)
