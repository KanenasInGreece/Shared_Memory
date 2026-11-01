"""rerank_shim.py: the shim must look, to the gateway, like a reranker it talks
to directly. Each test names the failure that would break it; every test runs a
real shim server against a fake upstream on loopback."""
import http.client
import http.server
import importlib.util
import json
import os
import select
import socket
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "shared-memory" / "scripts" / "rerank_shim.py"


def _load():
    spec = importlib.util.spec_from_file_location("rerank_shim_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


shim = _load()


class Upstream:
    """A fake vLLM: routes maps "METHOD path" -> handler(request_handler)."""

    def __init__(self):
        self.seen = []
        self.routes = {}
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _serve(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                outer.seen.append((self.command, self.path, body, dict(self.headers)))
                outer.routes.get(f"{self.command} {self.path}", outer.not_found)(self)

            do_GET = do_POST = _serve

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @staticmethod
    def not_found(h):
        Upstream.reply(h, 404, b'{"detail":"Not Found"}')

    @staticmethod
    def reply(h, status, body, headers=()):
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(body)

    def paths(self):
        return [p for _, p, _, _ in self.seen]


@pytest.fixture
def rig(monkeypatch):
    for k in ("SHIM_TIMEOUT_S", "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.delenv(k, raising=False)
    up = Upstream()
    monkeypatch.setenv("SHIM_VLLM_URL", f"http://127.0.0.1:{up.port}")
    server = shim.Threaded(("127.0.0.1", 0), shim.Shim)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    up.shim_port = server.server_address[1]
    try:
        yield up
    finally:
        server.shutdown()
        server.server_close()
        up.server.shutdown()
        up.server.server_close()


def _call(rig, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", rig.shim_port, timeout=10)
    try:
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read(), r
    finally:
        c.close()


def _raw(rig, data):
    """Send raw bytes, read until the shim closes; return everything received."""
    s = socket.create_connection(("127.0.0.1", rig.shim_port), timeout=10)
    try:
        s.sendall(data)
        out = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return out
            out += chunk
    finally:
        s.close()


RERANK_BODY = json.dumps({"query": "q", "documents": ["a", "b"], "top_n": 2}).encode()
RERANK_REPLY = b'{"results":[{"index":1,"relevance_score":0.9},{"index":0,"relevance_score":0.1}]}'


# 1. /v1/models not forwarded: the window probe would see a 404.
def test_models_is_forwarded(rig):
    body = b'{"data":[{"id":"bge-reranker-v2-m3","max_model_len":8192}]}'
    rig.routes["GET /v1/models"] = lambda h: Upstream.reply(h, 200, body)
    status, got, _ = _call(rig, "GET", "/v1/models")
    assert (status, got) == (200, body)


# 2. A default deadline re-introduced: the upstream connection gets a timeout.
@pytest.mark.parametrize("env,expected", [(None, None), ("2.5", 2.5)])
def test_upstream_timeout_is_none_unless_set(rig, monkeypatch, env, expected):
    if env is not None:
        monkeypatch.setenv("SHIM_TIMEOUT_S", env)
    seen = []
    real = http.client.HTTPConnection

    class Spy(real):
        def __init__(self, *a, **kw):
            seen.append(kw.get("timeout", "absent"))
            super().__init__(*a, **kw)

    monkeypatch.setattr(shim.http.client, "HTTPConnection", Spy)
    rig.routes["GET /health"] = lambda h: Upstream.reply(h, 200, b"{}")
    assert _call(rig, "GET", "/health")[0] == 200
    # The test's own client is the first connection; the shim's relay is the last.
    assert seen[-1] == expected


# 3. A bad setting accepted, or refused at import instead of at startup.
@pytest.mark.parametrize("raw", ["", "x", "nan", "inf", "+inf", "Infinity", "0", "-1", "<unset>"])
def test_bad_timeout_is_refused(raw):
    with pytest.raises(ValueError):
        shim._timeout_from_env(raw)


def test_good_timeout_values():
    assert shim._timeout_from_env("2.5") == 2.5
    assert shim._timeout_from_env(None) is None


@pytest.mark.parametrize("raw", ["nan", "x"])
def test_entry_point_exits_2_naming_the_variable(raw):
    env = dict(os.environ, SHIM_PORT="0", SHIM_TIMEOUT_S=raw)
    r = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True,
                       text=True, timeout=10)
    assert r.returncode == 2
    assert "SHIM_TIMEOUT_S" in r.stdout + r.stderr


def test_import_with_bad_setting_does_not_raise(monkeypatch):
    monkeypatch.setenv("SHIM_TIMEOUT_S", "x")
    monkeypatch.setenv("SHIM_PORT", "x")
    _load()


# 4. Request-body framing: the unread body of a refused request must not be
#    parsed as the next request.
@pytest.mark.parametrize("head,code", [
    (b"", 411),
    (b"Transfer-Encoding: chunked\r\n", 411),
    (b"Content-Length: -1\r\n", 400),
    (b"Content-Length: abc\r\n", 400),
])
def test_bad_framing_is_refused_and_closes(rig, head, code):
    req = b"POST /v1/reranking HTTP/1.1\r\nHost: x\r\n" + head + b"\r\n"
    out = _raw(rig, req + b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
    assert out.startswith(f"HTTP/1.1 {code} ".encode())
    assert b"Connection: close" in out
    assert out.count(b"HTTP/1.1 ") == 1
    assert rig.seen == []


# 5. Unmapped POST: 404, and its unread body is never parsed as a request.
def test_unmapped_post_is_404_and_closes(rig):
    body = b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n"
    req = (b"POST /nope HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(body)) + body
    out = _raw(rig, req)
    assert out.startswith(b"HTTP/1.1 404 ")
    assert b"Connection: close" in out
    assert out.count(b"HTTP/1.1 ") == 1
    assert rig.seen == []


# 6. A redirect is relayed, never followed.
def test_redirect_is_not_followed_on_get(rig):
    rig.routes["GET /health"] = lambda h: Upstream.reply(
        h, 302, b'{"moved":true}', [("Location", f"http://127.0.0.1:{rig.port}/target")])
    rig.routes["GET /target"] = lambda h: Upstream.reply(h, 200, b'{"other":true}')
    status, body, _ = _call(rig, "GET", "/health")
    assert (status, body) == (302, b'{"moved":true}')
    assert "/target" not in rig.paths()


def test_redirect_is_not_followed_on_post(rig):
    rig.routes["POST /v1/rerank"] = lambda h: Upstream.reply(
        h, 302, b'{"moved":true}', [("Location", f"http://127.0.0.1:{rig.port}/target")])
    rig.routes["POST /target"] = lambda h: Upstream.reply(h, 200, b'{"other":true}')
    status, body, _ = _call(rig, "POST", "/v1/reranking", RERANK_BODY)
    assert (status, body) == (302, b'{"moved":true}')
    assert "/target" not in rig.paths()


# 7. An abandoned request: when the gateway hangs up, the upstream connection
#    is closed instead of held for as long as vLLM takes.
def test_abandoned_request_closes_upstream(rig):
    release, closed = threading.Event(), threading.Event()

    def slow(h):
        while not release.is_set():
            if select.select([h.connection], [], [], 0.05)[0]:
                try:
                    if h.connection.recv(1, socket.MSG_PEEK) == b"":
                        closed.set()
                        return
                except OSError:
                    closed.set()
                    return
        Upstream.reply(h, 200, RERANK_REPLY)

    rig.routes["POST /v1/rerank"] = slow
    s = socket.create_connection(("127.0.0.1", rig.shim_port), timeout=10)
    s.sendall(b"POST /v1/reranking HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n"
              % len(RERANK_BODY) + RERANK_BODY)
    s.close()
    try:
        assert closed.wait(10), "upstream connection stayed open after the gateway hung up"
    finally:
        release.set()


# 8. The legitimate path: path rewritten, body untouched, answer relayed.
def test_rerank_reaches_upstream_unchanged(rig):
    rig.routes["POST /v1/rerank"] = lambda h: Upstream.reply(h, 200, RERANK_REPLY)
    status, body, r = _call(rig, "POST", "/v1/reranking", RERANK_BODY,
                            {"Content-Type": "application/json"})
    assert (status, body) == (200, RERANK_REPLY)
    assert r.getheader("Content-Type") == "application/json"
    method, path, got, _ = rig.seen[0]
    assert (method, path, got) == ("POST", "/v1/rerank", RERANK_BODY)


def test_upstream_status_is_relayed(rig):
    rig.routes["POST /v1/rerank"] = lambda h: Upstream.reply(h, 503, b'{"busy":1}')
    assert _call(rig, "POST", "/v1/reranking", RERANK_BODY)[:2] == (503, b'{"busy":1}')


# 9. No proxy environment variable is ever consulted.
def test_proxy_environment_is_ignored(rig, monkeypatch):
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    proxy = f"http://127.0.0.1:{dead.getsockname()[1]}"
    for k in ("http_proxy", "HTTP_PROXY"):
        monkeypatch.setenv(k, proxy)
    monkeypatch.setenv("no_proxy", "")
    try:
        rig.routes["POST /v1/rerank"] = lambda h: Upstream.reply(h, 200, RERANK_REPLY)
        status, body, _ = _call(rig, "POST", "/v1/reranking", RERANK_BODY)
        assert (status, body) == (200, RERANK_REPLY)
        assert rig.seen[0][2] == RERANK_BODY
    finally:
        dead.close()


def _header(headers, name):
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


# 11. The shim forwards the caller's Authorization and invents none when none came.
def test_shim_forwards_the_callers_key_and_invents_none(rig):
    rig.routes["POST /v1/rerank"] = lambda h: Upstream.reply(h, 200, RERANK_REPLY)
    rig.routes["GET /health"] = lambda h: Upstream.reply(h, 200, b"{}")
    status, _, _ = _call(
        rig, "POST", "/v1/reranking", RERANK_BODY,
        {"Authorization": "Bearer SHIM-CANARY", "Content-Type": "application/json"},
    )
    assert status == 200
    assert _header(rig.seen[0][3], "Authorization") == "Bearer SHIM-CANARY"
    rig.seen.clear()
    status, _, _ = _call(rig, "GET", "/health")
    assert status == 200
    assert _header(rig.seen[0][3], "Authorization") is None


def test_shim_opens_a_public_http_upstream_when_a_key_came(monkeypatch):
    """The shim has no transport check. A key that arrived is forwarded, including toward a public http address."""
    monkeypatch.setenv("SHIM_VLLM_URL", "http://8.8.8.8:9")
    seen = []

    class _Conn:
        def __init__(self, host, port, timeout=None):
            seen.append((host, port))
            raise OSError("stopped before the network")

        def close(self):
            pass

    monkeypatch.setattr(shim.http.client, "HTTPConnection", _Conn)
    server = shim.Threaded(("127.0.0.1", 0), shim.Shim)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    raw = (
        b"POST /v1/reranking HTTP/1.1\r\n"
        b"Host: x\r\n"
        b"Authorization: Bearer SHIM-CANARY\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 2\r\n"
        b"Connection: close\r\n"
        b"\r\n"
        b"{}"
    )
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.sendall(raw)
        chunks = []
        while True:
            piece = sock.recv(65536)
            if not piece:
                break
            chunks.append(piece)
        sock.close()
    finally:
        server.shutdown()
        server.server_close()
    body = b"".join(chunks)
    assert seen == [("8.8.8.8", 9)]
    assert b"public http" not in body
    assert b"SHIM-CANARY" not in body


def _shim_cert(tmp_path):
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


def test_shim_https_verifies_the_certificate_and_forwards_the_key(monkeypatch, tmp_path):
    key, cert = _shim_cert(tmp_path)
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            seen.append({k: v for k, v in self.headers.items()})
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert), keyfile=str(key))
    upstream.socket = context.wrap_socket(upstream.socket, server_side=True)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    port = upstream.server_address[1]
    monkeypatch.setenv("SHIM_VLLM_URL", f"https://127.0.0.1:{port}")
    server = shim.Threaded(("127.0.0.1", 0), shim.Shim)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    contexts = []
    real = ssl.create_default_context

    def _wrapped(*args, **kwargs):
        made = real(*args, **kwargs)
        contexts.append(made)
        made.load_verify_locations(cafile=str(cert))
        return made

    raw = (
        b"POST /v1/reranking HTTP/1.1\r\n"
        b"Host: x\r\n"
        b"Authorization: Bearer SHIM-CANARY\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 2\r\n"
        b"Connection: close\r\n"
        b"\r\n"
        b"{}"
    )

    def _once():
        sock = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=10)
        try:
            sock.sendall(raw)
            chunks = []
            while True:
                piece = sock.recv(65536)
                if not piece:
                    return b"".join(chunks)
                chunks.append(piece)
        finally:
            sock.close()

    try:
        refused = _once()
        assert seen == []
        assert b"HTTP/1.1 502 " in refused
        assert b"SHIM-CANARY" not in refused
        monkeypatch.setattr(shim.ssl, "create_default_context", _wrapped)
        accepted = _once()
        assert b"HTTP/1.1 200 " in accepted
        assert contexts
        assert contexts[0].verify_mode == ssl.CERT_REQUIRED
        assert contexts[0].check_hostname is True
        assert _header(seen[0], "Authorization") == "Bearer SHIM-CANARY"
    finally:
        server.shutdown()
        server.server_close()
        upstream.shutdown()
        upstream.server_close()
