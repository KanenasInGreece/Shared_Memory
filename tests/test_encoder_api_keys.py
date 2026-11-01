"""Isolation tests for optional encoder API keys. Each test dies when its failure returns.

A canary is asserted on the encoder side before any claim that a log, a refusal, or /health omitted it.
"""
import asyncio
import http.server
import importlib
import json
import logging
import os
import ssl
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from multidict import CIMultiDict
from yarl import URL

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "shared-memory" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import secure_env


CANARY_EMBED = "CANARY-EMBEDDER-TOKEN"
CANARY_RERANK = "CANARY-RERANKER-TOKEN"
CANARY_CALLER = "CANARY-CALLER-TOKEN"


def _forget():
    secure_env._secrets.pop("EMBEDDER_API_KEY", None)
    secure_env._secrets.pop("RERANKER_API_KEY", None)


@pytest.fixture(autouse=True)
def _encoder_secrets_do_not_leak():
    _forget()
    yield
    _forget()


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def _load_keys():
    _forget()
    secure_env.load_split_env()


class _Req:
    def __init__(self, path, headers=None):
        self.method = "POST"
        self.rel_url = URL(path)
        self.headers = CIMultiDict(headers or {})
        self.can_read_body = False
        self.content_length = None
        self._store = {}

    def __setitem__(self, key, value):
        self._store[key] = value

    def __getitem__(self, key):
        return self._store[key]


class _Upstream:
    def __init__(self, status):
        self.status = status
        self.headers = {}

    async def read(self, _n=None):
        return b""

    @property
    def content(self):
        return self


class _CM:
    def __init__(self, status):
        self._status = status

    async def __aenter__(self):
        return _Upstream(self._status)

    async def __aexit__(self, *_a):
        return False


class _Session:
    def __init__(self, status=200, by_url=None):
        self.status = status
        self.by_url = by_url or {}
        self.calls = []

    def request(self, method, url, **kw):
        headers = dict(kw.get("headers") or {})
        self.calls.append((method, str(url), headers))
        return _CM(self.by_url.get(str(url), self.status))

    def get(self, url, **kw):
        headers = dict(kw.get("headers") or {})
        self.calls.append(("GET", str(url), headers))
        status = self.by_url.get(str(url), self.status)
        return _CM(status)

    def post(self, url, **kw):
        headers = dict(kw.get("headers") or {})
        self.calls.append(("POST", str(url), headers))
        return _CM(self.by_url.get(str(url), self.status))


class _MustNot:
    def request(self, *_a, **_k):
        raise AssertionError("upstream was called")

    def get(self, *_a, **_k):
        raise AssertionError("upstream was called")


def _gateway(monkeypatch):
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)
    monkeypatch.delenv("RERANKER_API_KEY", raising=False)
    monkeypatch.delenv("EMBEDDER_API_KEY_FILE", raising=False)
    monkeypatch.delenv("RERANKER_API_KEY_FILE", raising=False)
    secure_env._secrets.pop("AGENT_TOKENS", None)
    _forget()
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as gateway
    importlib.reload(gateway)
    return gateway


def _auth(headers):
    for key, value in (headers or {}).items():
        if key.lower() == "authorization":
            return value
    return None


def _json(response):
    return json.loads(response.body.decode())


def _cert(tmp_path):
    key = tmp_path / "key.pem"
    cert = tmp_path / "cert.pem"
    subprocess.check_call(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(key), "-out", str(cert), "-days", "1", "-nodes",
            "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1",
            "-addext", "basicConstraints=CA:TRUE",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return key, cert


def _tls_server(cert, key):
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            seen.append({k: v for k, v in self.headers.items()})
            body = b'{"data":[{"embedding":[0.1],"index":0}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert), keyfile=str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


# 1. No key sends nothing. A plain .env line is sent.
def test_no_key_sends_no_header(monkeypatch):
    monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)
    monkeypatch.delenv("EMBEDDER_API_KEY_FILE", raising=False)
    _forget()
    assert secure_env.encoder_key_configured("embedder") is False
    assert secure_env.encoder_auth_headers("embedder", "http://127.0.0.1:8070") == {}


def test_a_plain_env_line_is_sent(monkeypatch, tmp_path):
    envfile = tmp_path / "env"
    envfile.write_text("EMBEDDER_API_KEY=" + CANARY_EMBED + "\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(envfile))
    monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)
    _load_keys()
    headers = secure_env.encoder_auth_headers("embedder", "http://127.0.0.1:8070")
    assert headers["Authorization"] == "Bearer " + CANARY_EMBED
    assert secure_env.encoder_key_configured("embedder") is True


# 2. An embedder token stays on the embedder, and the reranker token on the reranker.
def test_each_token_stays_on_its_own_encoder(monkeypatch, tmp_path):
    monkeypatch.setenv("EMBEDDER_API_KEY_FILE", _write(tmp_path, "e", CANARY_EMBED + "\n"))
    monkeypatch.setenv("RERANKER_API_KEY_FILE", _write(tmp_path, "r", CANARY_RERANK + "\n"))
    monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)
    monkeypatch.delenv("RERANKER_API_KEY", raising=False)
    _load_keys()
    embed = secure_env.encoder_auth_headers("embedder", "http://127.0.0.1:8070")
    rerank = secure_env.encoder_auth_headers("reranker", "http://127.0.0.1:8071")
    assert embed["Authorization"] == "Bearer " + CANARY_EMBED
    assert rerank["Authorization"] == "Bearer " + CANARY_RERANK


# 3 and 4. The caller's Authorization is dropped. A query string is refused before the header is attached.
def test_callers_authorization_is_not_forwarded(monkeypatch):
    gateway = _gateway(monkeypatch)
    monkeypatch.setenv("EMBEDDER_API_KEY", CANARY_EMBED)
    session = _Session(status=401)
    proxy = gateway.AsyncHiveMindProxy()
    proxy.session = session
    response = asyncio.run(proxy.handle_encoder(_Req(
        "/v1/embeddings", {"Authorization": "Bearer " + CANARY_CALLER})))
    assert session.calls, "the encoder never saw the request"
    assert _auth(session.calls[0][2]) == "Bearer " + CANARY_EMBED
    assert CANARY_CALLER not in json.dumps(session.calls)
    body = response.text or ""
    assert CANARY_EMBED not in body
    assert response.headers["X-SM-Fault-Origin"] == "upstream"


def test_a_query_string_does_not_carry_the_token(monkeypatch):
    gateway = _gateway(monkeypatch)
    monkeypatch.setenv("EMBEDDER_API_KEY", CANARY_EMBED)
    proxy = gateway.AsyncHiveMindProxy()
    proxy.session = _MustNot()
    response = asyncio.run(proxy.handle_encoder(_Req("/v1/embeddings?q=1")))
    assert response.status == 403
    assert CANARY_EMBED not in (response.text or "")
    assert "credentialed backends" in (response.text or "")


# The reranker route must not inherit the embedder token.
def test_reranker_route_sends_the_reranker_token(monkeypatch):
    gateway = _gateway(monkeypatch)
    monkeypatch.setenv("EMBEDDER_API_KEY", CANARY_EMBED)
    monkeypatch.setenv("RERANKER_API_KEY", CANARY_RERANK)
    session = _Session(status=401)
    proxy = gateway.AsyncHiveMindProxy()
    proxy.session = session
    asyncio.run(proxy.handle_encoder(_Req("/v1/reranking")))
    assert session.calls, "the encoder never saw the request"
    assert _auth(session.calls[0][2]) == "Bearer " + CANARY_RERANK
    assert CANARY_EMBED not in json.dumps(session.calls)


# 13. Default plain HTTP carries the key. ENCODER_PLAINTEXT_OK=false withholds it from a public address.
def test_public_http_sends_the_key_by_default(monkeypatch):
    monkeypatch.delenv("ENCODER_PLAINTEXT_OK", raising=False)
    monkeypatch.setenv("EMBEDDER_API_KEY", CANARY_EMBED)
    sent = secure_env.encoder_auth_headers("embedder", "http://8.8.8.8:9/v1")
    assert sent["Authorization"] == "Bearer " + CANARY_EMBED


def test_plaintext_false_withholds_a_public_http_key(monkeypatch, caplog):
    import coordinator
    monkeypatch.setenv("ENCODER_PLAINTEXT_OK", "false")
    monkeypatch.setenv("EMBEDDER_API_KEY", CANARY_EMBED)
    monkeypatch.setattr(coordinator, "EMBED_URL", "http://8.8.8.8:9/v1/embeddings")
    previous = coordinator._encoder_endpoints_logged
    coordinator._encoder_endpoints_logged = False
    try:
        with caplog.at_level(logging.WARNING, logger="coordinator"):
            coordinator.log_encoder_endpoints()
            coordinator.log_encoder_endpoints()
        lines = [r.message for r in caplog.records if "8.8.8.8" in r.message]
        assert len(lines) == 1
        assert CANARY_EMBED not in caplog.text
    finally:
        coordinator._encoder_endpoints_logged = previous
    assert secure_env.encoder_auth_headers("embedder", "http://8.8.8.8:9/v1") == {}
    private = secure_env.encoder_auth_headers("embedder", "http://127.0.0.1:8070")
    assert private["Authorization"] == "Bearer " + CANARY_EMBED
    https = secure_env.encoder_auth_headers("embedder", "https://8.8.8.8/v1")
    assert https["Authorization"] == "Bearer " + CANARY_EMBED
    assert secure_env.encoder_plaintext_warnings(
        {"embedder": "http://8.8.8.8:9/v1"}) == ["http://8.8.8.8:9/v1"]


# 10. The fake encoder receives the canary before /health and the log are checked.
def test_token_text_is_absent_from_health_and_the_log(monkeypatch, caplog):
    gateway = _gateway(monkeypatch)
    monkeypatch.setenv("EMBEDDER_API_KEY", CANARY_EMBED)
    session = _Session(status=401)
    proxy = gateway.AsyncHiveMindProxy()
    proxy.session = session

    async def _run():
        return await gateway._build_health_checks(proxy, None)

    with caplog.at_level("WARNING"):
        checks = asyncio.run(_run())
    assert session.calls, "the encoder never saw the request"
    assert _auth(session.calls[0][2]) == "Bearer " + CANARY_EMBED
    rendered = json.dumps(checks) + caplog.text
    assert CANARY_EMBED not in rendered
    assert checks["config"]["embedder_key_configured"] is True
    assert "embedder_key" not in checks["config"]
    assert checks["embedder"] == "auth_failed"


# 5 and 6. A wrong token is auth_failed. The reranker stays degraded. The embedder is down.
def _health(monkeypatch, kind, status_for):
    gateway = _gateway(monkeypatch)
    monkeypatch.setenv(f"{kind.upper()}_API_KEY", "WRONG")
    session = _Session(status=200, by_url={})
    base = gateway.EMBEDDER_URL if kind == "embedder" else gateway.RERANKER_URL
    session.by_url[f"{base}/health"] = status_for
    proxy = gateway.AsyncHiveMindProxy()
    proxy.session = session

    async def _run():
        return await gateway._build_health_checks(proxy, None)

    checks = asyncio.run(_run())
    return gateway, proxy, checks


def test_a_wrong_reranker_token_is_degraded_not_503(monkeypatch):
    gateway, proxy, checks = _health(monkeypatch, "reranker", 401)
    dep = checks["dependencies"]["reranker"]
    assert dep["state"] == "degraded"
    assert dep["reason"] == "auth_failed"
    assert checks["embedder"] == "ok"

    class _Request:
        app = {"proxy": proxy, "coordinator": None}

    async def _cached(_proxy, _coordinator):
        return checks

    monkeypatch.setattr(gateway, "_health_probe_cached", _cached)
    response = asyncio.run(gateway.handle_health(_Request()))
    assert response.status == 200


def test_a_wrong_embedder_token_is_down_and_503(monkeypatch):
    gateway, proxy, checks = _health(monkeypatch, "embedder", 403)
    dep = checks["dependencies"]["embedder"]
    assert dep["state"] == "down"
    assert dep["reason"] == "auth_failed"
    assert checks["reranker"] == "ok"

    class _Request:
        app = {"proxy": proxy, "coordinator": None}

    async def _cached(_proxy, _coordinator):
        return checks

    monkeypatch.setattr(gateway, "_health_probe_cached", _cached)
    response = asyncio.run(gateway.handle_health(_Request()))
    assert response.status == 503
    assert CANARY_EMBED not in (response.text or "")


# 7. A window verdict does not mask auth_failed, and a refused embedder POST is not an overrun.
def test_window_verdict_does_not_mask_auth_failed(monkeypatch):
    gateway = _gateway(monkeypatch)
    monkeypatch.setenv("EMBEDDER_API_KEY", CANARY_EMBED)
    dep = gateway._encoder_dependency(
        "ok",
        {"status": "auth_failed"},
        {"full_payload_ok": False, "advertised_tokens": 1},
        kind="embedder",
    )
    assert dep == {"state": "down", "reason": "auth_failed"}
    rerank = gateway._encoder_dependency(
        "auth_failed", {"status": "ok"}, {"full_payload_ok": False}, kind="reranker",
    )
    assert rerank == {"state": "degraded", "reason": "auth_failed"}


def test_configured_embedder_401_is_not_a_window_overrun(monkeypatch):
    monkeypatch.setenv("EMBEDDER_API_KEY", CANARY_EMBED)
    import encoder_window
    encoder_window._window_cache.clear()

    class _Resp:
        def __init__(self, status, body=None):
            self.status = status
            self._body = body or {}

        async def json(self):
            return self._body

        async def read(self):
            return b""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class _Sess:
        def __init__(self):
            self.seen = []

        def get(self, url, **kw):
            self.seen.append(("GET", kw.get("headers")))
            if str(url).endswith("/v1/models"):
                return _Resp(200, {"data": [{"id": "bge-m3", "max_model_len": 8192}]})
            return _Resp(404, {})

        def post(self, url, **kw):
            self.seen.append(("POST", kw.get("headers")))
            return _Resp(401)

    try:
        session = _Sess()
        result = asyncio.run(encoder_window.probe_encoder(
            session, "http://127.0.0.1:8070", "/v1/embeddings", "bge-m3"))
        assert session.seen[0][0] == "GET"
        assert session.seen[0][1]["Authorization"] == "Bearer " + CANARY_EMBED
        assert result["full_payload_ok"] is None
        encoder_window.reset_encoder_window_cache()
        monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)
        _forget()
        bare = _Sess()
        unconfigured = asyncio.run(encoder_window.probe_encoder(
            bare, "http://127.0.0.1:8070", "/v1/embeddings", "bge-m3"))
        assert bare.seen[0][1] is None
        assert unconfigured["full_payload_ok"] is False
    finally:
        encoder_window.reset_encoder_window_cache()


def test_reranker_window_probe_sends_the_reranker_token(monkeypatch):
    monkeypatch.setenv("EMBEDDER_API_KEY", CANARY_EMBED)
    monkeypatch.setenv("RERANKER_API_KEY", CANARY_RERANK)
    import encoder_window
    encoder_window._window_cache.clear()

    class _Resp:
        def __init__(self, status):
            self.status = status

        async def json(self):
            return {}

        async def read(self):
            return b""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class _Sess:
        def __init__(self):
            self.seen = []

        def get(self, url, **kw):
            self.seen.append(kw.get("headers"))
            return _Resp(404)

        def post(self, url, **kw):
            self.seen.append(kw.get("headers"))
            return _Resp(404)

    try:
        session = _Sess()
        asyncio.run(encoder_window.probe_encoder(
            session, "http://127.0.0.1:8071", "/v1/reranking", "bge-reranker-v2-m3"))
        assert session.seen, "the probe never called the encoder"
        assert session.seen[0]["Authorization"] == "Bearer " + CANARY_RERANK
        assert CANARY_EMBED not in json.dumps(session.seen)
    finally:
        encoder_window.reset_encoder_window_cache()


# 8. No key and a 401 keeps today's status.
def test_unconfigured_401_stays_http_401(monkeypatch):
    monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)
    monkeypatch.delenv("EMBEDDER_API_KEY_FILE", raising=False)
    gateway = _gateway(monkeypatch)
    session = _Session(status=401)
    proxy = gateway.AsyncHiveMindProxy()
    proxy.session = session

    async def _run():
        return await gateway._build_health_checks(proxy, None)

    checks = asyncio.run(_run())
    assert checks["embedder"] == "http_401"
    assert checks["reranker"] == "http_401"
    assert checks["dependencies"]["embedder"]["reason"] == "probe:http_401"
    assert _auth(session.calls[0][2]) is None
    assert checks["config"]["embedder_key_configured"] is False


# A missing file is the loader's warning. The gateway's check still boots, and no header is sent.
def test_a_missing_key_file_sends_no_header_and_does_not_stop_check_config(monkeypatch, tmp_path, capsys):
    missing = str(tmp_path / "missing.key")
    monkeypatch.setenv("EMBEDDER_API_KEY_FILE", missing)
    monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)
    _load_keys()
    err = capsys.readouterr().err
    assert "not readable" in err
    assert secure_env.encoder_key_configured("embedder") is True
    assert secure_env.encoder_auth_headers("embedder", "http://127.0.0.1:8070") == {}

    env = os.environ.copy()
    env["SECURE_ENV_FILE"] = ""
    env["COORDINATOR_UDS"] = ""
    env["EMBEDDER_API_KEY_FILE"] = missing
    env.pop("AGENT_TOKENS", None)
    env.pop("EMBEDDER_API_KEY", None)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_config.py")],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "would boot" in proc.stdout
    assert "not readable" in proc.stderr
    assert "token_file_missing" not in proc.stdout
    assert "path_not_absolute" not in proc.stdout
    assert CANARY_EMBED not in proc.stdout + proc.stderr


# The four embed attempts stay, for a refused key as for any other failure. The last one is logged at ERROR.
class _Post:
    def __init__(self, status):
        self.status = status
        self.calls = []

    async def post(self, url, json=None, timeout=None, **kw):
        self.calls.append(kw)

        class _Response:
            status_code = self.status
            text = "body echoes " + CANARY_EMBED

            def raise_for_status(self):
                import httpx
                request = httpx.Request("POST", url)
                response = httpx.Response(self.status_code, request=request, text=self.text)
                raise httpx.HTTPStatusError("HTTP " + str(self.status_code), request=request, response=response)

        return _Response()


def test_a_refused_embed_key_is_still_retried_and_logged(monkeypatch, caplog):
    import coordinator
    monkeypatch.setattr(coordinator, "EMBED_BACKOFF", 0)
    monkeypatch.setenv("EMBEDDER_API_KEY", CANARY_EMBED)
    for method, needle in (
            ("_embed", "Embedding failed after"),
            ("_embed_many", "Batch embedding failed after")):
        client = _Post(401)
        target = getattr(coordinator.MemoryCoordinator, method)
        args = ("hi", client) if method == "_embed" else (["hi"], client)
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="coordinator"):
            with pytest.raises(RuntimeError) as caught:
                asyncio.run(target(None, *args))
        assert needle in str(caught.value)
        assert CANARY_EMBED not in str(caught.value)
        assert CANARY_EMBED not in caplog.text
        assert any(r.levelno == logging.ERROR and needle in r.message for r in caplog.records)
        assert len(client.calls) == coordinator.EMBED_RETRIES
        assert client.calls[0]["headers"]["Authorization"] == "Bearer " + CANARY_EMBED
        assert "follow_redirects" not in client.calls[0]


def test_an_unconfigured_401_still_retries_four_times(monkeypatch):
    import coordinator
    monkeypatch.setattr(coordinator, "EMBED_BACKOFF", 0)
    monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)
    monkeypatch.delenv("EMBEDDER_API_KEY_FILE", raising=False)
    _forget()
    for method in ("_embed", "_embed_many"):
        client = _Post(401)
        target = getattr(coordinator.MemoryCoordinator, method)
        args = ("hi", client) if method == "_embed" else (["hi"], client)
        with pytest.raises(RuntimeError):
            asyncio.run(target(None, *args))
        assert len(client.calls) == coordinator.EMBED_RETRIES
        assert "headers" not in client.calls[0]


# The capability probe names auth_failed, and the ceiling treats it as failing.
def test_capability_probe_names_auth_failed_when_configured(monkeypatch):
    gateway = _gateway(monkeypatch)
    monkeypatch.setenv("EMBEDDER_API_KEY", CANARY_EMBED)
    monkeypatch.setenv("RERANKER_API_KEY", CANARY_RERANK)
    session = _Session(status=401)
    result = asyncio.run(gateway._probe_capability(session))
    assert session.calls[0][0] == "POST"
    assert _auth(session.calls[0][2]) == "Bearer " + CANARY_RERANK
    assert _auth(session.calls[1][2]) == "Bearer " + CANARY_EMBED
    assert result["reranker"]["status"] == "auth_failed"
    assert result["embedder"]["status"] == "auth_failed"
    assert "error" not in result["embedder"]
    assert gateway._probe_sleep_s(result) == gateway.CAPABILITY_PROBE_RETRY_S


def _embed_reply_server():
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            seen.append({k: v for k, v in self.headers.items()})
            body = b'{"data":[{"embedding":[0.1],"index":0}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def _run_embed():
    import httpx
    import coordinator

    async def _once():
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            return await coordinator.MemoryCoordinator._embed(None, "hi", client)

    return asyncio.run(_once())


# A key file may end in a space. h11 would otherwise put that value in the retry log and the 503.
def test_embed_strips_a_trailing_space_and_does_not_log_it(monkeypatch, tmp_path, caplog, capsys):
    import coordinator
    server, seen = _embed_reply_server()
    monkeypatch.setattr(coordinator, "EMBED_URL", f"http://127.0.0.1:{server.server_address[1]}/v1/embeddings")
    monkeypatch.setattr(coordinator, "EMBED_BACKOFF", 0)
    spaced = CANARY_EMBED + " "
    try:
        monkeypatch.setenv("EMBEDDER_API_KEY_FILE", _write(tmp_path, "spaced.key", spaced + "\n"))
        monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)
        _load_keys()
        capsys.readouterr()
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="coordinator"):
            assert _run_embed() == [0.1]
        assert seen, "the encoder never saw the request"
        assert _auth(seen[-1]) == "Bearer " + CANARY_EMBED
        assert spaced not in caplog.text
        assert CANARY_EMBED not in caplog.text
        assert spaced not in capsys.readouterr().err
    finally:
        server.shutdown()
        server.server_close()


def test_embed_withholds_a_non_ascii_key_and_does_not_log_it(monkeypatch, tmp_path, caplog, capsys):
    import coordinator
    server, seen = _embed_reply_server()
    monkeypatch.setattr(coordinator, "EMBED_URL", f"http://127.0.0.1:{server.server_address[1]}/v1/embeddings")
    monkeypatch.setattr(coordinator, "EMBED_BACKOFF", 0)
    bad = CANARY_EMBED + "\u00e9"
    try:
        monkeypatch.setenv("EMBEDDER_API_KEY_FILE", _write(tmp_path, "bad.key", bad + "\n"))
        monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)
        _load_keys()
        # The warning is once per process, so this test starts from an empty set.
        monkeypatch.setattr(secure_env, "_warned_non_ascii_encoder_names", set())
        capsys.readouterr()
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="coordinator"):
            assert _run_embed() == [0.1]
            # One warning for the process, including a second embed.
            assert _run_embed() == [0.1]
        assert seen, "the encoder never saw the request"
        assert _auth(seen[-1]) is None
        assert bad not in json.dumps(seen)
        err = capsys.readouterr().err
        warnings = [line for line in err.splitlines() if "EMBEDDER_API_KEY" in line]
        assert warnings == ["[secure_env] WARNING: EMBEDDER_API_KEY is not visible ASCII — withholding the encoder key"]
        assert bad not in err
        assert bad not in caplog.text
        assert CANARY_EMBED not in caplog.text
    finally:
        server.shutdown()
        server.server_close()


# The file loader already refuses a control character, so this value is the environment variable and reaches the header check.
def test_embed_withholds_an_inner_newline(monkeypatch, caplog, capsys):
    import coordinator
    server, seen = _embed_reply_server()
    monkeypatch.setattr(coordinator, "EMBED_URL", f"http://127.0.0.1:{server.server_address[1]}/v1/embeddings")
    monkeypatch.setattr(coordinator, "EMBED_BACKOFF", 0)
    bad = CANARY_EMBED[:7] + "\n" + CANARY_EMBED[7:]
    try:
        monkeypatch.setenv("EMBEDDER_API_KEY", bad)
        monkeypatch.delenv("EMBEDDER_API_KEY_FILE", raising=False)
        _forget()
        monkeypatch.setattr(secure_env, "_warned_non_ascii_encoder_names", set())
        capsys.readouterr()
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="coordinator"):
            assert _run_embed() == [0.1]
        assert seen, "the encoder never saw the request"
        assert _auth(seen[-1]) is None
        assert bad not in json.dumps(seen)
        err = capsys.readouterr().err
        warnings = [line for line in err.splitlines() if "EMBEDDER_API_KEY" in line]
        assert warnings == ["[secure_env] WARNING: EMBEDDER_API_KEY is not visible ASCII — withholding the encoder key"]
        assert bad not in err
        assert bad not in caplog.text
        assert CANARY_EMBED not in caplog.text
    finally:
        server.shutdown()
        server.server_close()


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_a):
        return False


# The search rerank post must not borrow the embedder token.
def test_coordinator_rerank_post_uses_the_reranker_key(monkeypatch):
    import coordinator
    monkeypatch.setenv("EMBEDDER_API_KEY", CANARY_EMBED)
    monkeypatch.setenv("RERANKER_API_KEY", CANARY_RERANK)
    monkeypatch.delenv("EMBEDDER_API_KEY_FILE", raising=False)
    monkeypatch.delenv("RERANKER_API_KEY_FILE", raising=False)
    _forget()
    coord = coordinator.MemoryCoordinator()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content": "a fact", "metadata": {}, "created_at": None},
    ])
    coord._pool = MagicMock()
    coord._pool.acquire = MagicMock(return_value=_Acquire(conn))
    session = AsyncMock()
    session.run = AsyncMock(return_value=None)
    coord._neo4j = MagicMock()
    coord._neo4j.session = MagicMock(return_value=_Acquire(session))
    rerank = MagicMock()
    rerank.raise_for_status = MagicMock()
    rerank.json = MagicMock(return_value={"results": [{"index": 0, "relevance_score": 1.0}]})
    embedded = MagicMock()
    embedded.status_code = 200
    embedded.text = ""
    embedded.raise_for_status = MagicMock()
    embedded.json = MagicMock(return_value={"data": [{"embedding": [0.1], "index": 0}]})
    request = MagicMock()
    request.json = AsyncMock(return_value={"query": "a fact", "limit": 5})
    request.get = MagicMock(return_value=None)
    posts = []

    async def _post(url, **kwargs):
        posts.append((url, kwargs))
        if url == coordinator.RERANK_URL:
            return rerank
        return embedded

    async def _search():
        with patch("httpx.AsyncClient") as mock_cls:
            http = AsyncMock()
            http.post = AsyncMock(side_effect=_post)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=http)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            return await coord.handle_search(request)

    response = asyncio.run(_search())
    assert response.status == 200
    rerank_posts = [item for item in posts if item[0] == coordinator.RERANK_URL]
    assert len(rerank_posts) == 1
    assert rerank_posts[0][1]["headers"]["Authorization"] == "Bearer " + CANARY_RERANK
    assert CANARY_EMBED not in json.dumps(rerank_posts[0][1]["headers"])


def _counting_server(status, location=None):
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _answer(self):
            hits.append(self.command + " " + self.path)
            body = b""
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            self._answer()

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            self._answer()

        def log_message(self, *_a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, hits


# A 302 from the encoder is the client's answer. The Location target is not a second hop.
def test_encoder_forward_does_not_follow_a_redirect(monkeypatch):
    target, target_hits = _counting_server(200)
    encoder, encoder_hits = _counting_server(
        302, f"http://127.0.0.1:{target.server_address[1]}/landed")
    try:
        gateway = _gateway(monkeypatch)
        gateway.ROUTING_MAP["/v1/embeddings"] = f"http://127.0.0.1:{encoder.server_address[1]}"

        async def _quiet(self, *_a, **_k):
            return None

        monkeypatch.setattr(gateway.web.StreamResponse, "prepare", _quiet)
        monkeypatch.setattr(gateway.web.StreamResponse, "write", _quiet)
        monkeypatch.setattr(gateway.web.StreamResponse, "write_eof", _quiet)
        proxy = gateway.AsyncHiveMindProxy()

        async def _run():
            await proxy.start_session()
            try:
                return await proxy.handle_encoder(_Req("/v1/embeddings"))
            finally:
                await proxy.cleanup()

        response = asyncio.run(_run())
        assert encoder_hits, "the encoder never saw the request"
        assert target_hits == []
        assert response.status == 302
    finally:
        encoder.shutdown()
        encoder.server_close()
        target.shutdown()
        target.server_close()


# HTTPS succeeds only when the CA is in the bundle certifi.where returns. SSL_CERT_FILE does not add it.
def test_https_embedder_verifies_certificates(monkeypatch, tmp_path, caplog):
    import certifi
    import coordinator

    key, cert = _cert(tmp_path)
    server, seen = _tls_server(cert, key)
    monkeypatch.setattr(
        coordinator, "EMBED_URL", f"https://127.0.0.1:{server.server_address[1]}/v1/embeddings")
    monkeypatch.setattr(coordinator, "EMBED_BACKOFF", 0)
    monkeypatch.setenv("EMBEDDER_API_KEY", CANARY_EMBED)
    monkeypatch.delenv("EMBEDDER_API_KEY_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    _forget()
    try:
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="coordinator"):
            with pytest.raises(RuntimeError) as caught:
                _run_embed()
        assert seen == []
        assert CANARY_EMBED not in str(caught.value)
        assert CANARY_EMBED not in caplog.text

        monkeypatch.setenv("SSL_CERT_FILE", str(cert))
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="coordinator"):
            with pytest.raises(RuntimeError) as caught:
                _run_embed()
        assert seen == []
        assert CANARY_EMBED not in str(caught.value)
        assert CANARY_EMBED not in caplog.text

        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        bundle = tmp_path / "bundle.pem"
        bundle.write_bytes(Path(certifi.where()).read_bytes() + b"\n" + cert.read_bytes())
        monkeypatch.setattr(certifi, "where", lambda: str(bundle))
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="coordinator"):
            assert _run_embed() == [0.1]
        assert _auth(seen[-1]) == "Bearer " + CANARY_EMBED
        assert CANARY_EMBED not in caplog.text
    finally:
        server.shutdown()
        server.server_close()
