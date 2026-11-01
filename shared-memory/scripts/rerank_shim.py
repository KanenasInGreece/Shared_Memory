#!/usr/bin/env python3
"""Path shim: the framework's /v1/reranking -> vLLM's /v1/rerank.

Optional. You only need this if you serve the RERANKER with vLLM instead of
llama.cpp. The embedder needs no shim -- vLLM already serves /v1/embeddings at
the path the framework expects.

WHY THIS EXISTS (measured 2026-08-25):
coordinator._encoder_url() takes RERANKER_URL as a BASE and appends the FIXED
path "/v1/reranking". vLLM serves /rerank, /v1/rerank and /v2/rerank -- it does
NOT serve /v1/reranking (measured: HTTP 404). Everything else about the contract
already matches, measured against a live vLLM 0.21.1 XPU server:
  * a body with NO "model" field is accepted (HTTP 200) -- no injection needed
  * results[].index is the SUBMITTED array position, not sorted rank
  * results[].relevance_score is the exact field name the coordinator reads
  * top_n is honoured
So this shim rewrites the PATH ONLY. It never touches the body. If it ever needs
to, that is a contract change and belongs in a record, not in this file.

Every address is an env-overridable default, never a literal in a code path:
  SHIM_VLLM_URL  where vLLM listens          (default http://127.0.0.1:8090)
  SHIM_HOST      address the shim binds      (default 127.0.0.1)
  SHIM_PORT      port the shim binds         (default 8092)
  SHIM_TIMEOUT_S upstream socket timeout, seconds (default: none)
Then point the gateway at it:  RERANKER_URL=http://<SHIM_HOST>:<SHIM_PORT>

There is no default deadline: the gateway's own per-call wait decides what its
caller sees, and a second timer here would only cut a slow rerank short. A
request the gateway abandons is released when it hangs up -- the shim then
closes its upstream connection. vLLM may keep computing after that; the shim
cannot stop it.

SHIM_HOST defaults to LOOPBACK deliberately. The shim checks nothing itself, so
a shim bound to 0.0.0.0 publishes the reranking endpoint to every interface on
the host, unless the encoder itself demands a key. Widen it only when you know
what is listening.

An https SHIM_VLLM_URL verifies against the OS store or SSL_CERT_FILE.
"""
import http.client, http.server, socketserver, os, sys, math, select, socket, ssl
import urllib.parse

DEFAULT_VLLM = "http://127.0.0.1:8090"

# The gateway calls three things on a reranker base: the rerank POST (search and
# the capability probe), GET /health (hive_mind_proxy._build_health_checks) and
# GET /v1/models (encoder_window, the window probe). Anything else is a
# mistyped framework call and must fail loudly rather than be forwarded blind.
POST_MAP = {"/v1/reranking": "/v1/rerank"}
GET_MAP  = {"/health": "/health", "/v1/models": "/v1/models"}


def _timeout_from_env(raw):
    """None when unset; otherwise a finite positive number of seconds."""
    if raw is None:
        return None
    value = float(raw.strip())
    if not math.isfinite(value) or value <= 0:
        raise ValueError("must be a positive, finite number of seconds")
    return value


def _upstream_from_env(raw):
    """Split SHIM_VLLM_URL into (scheme, host, port, path prefix). http or https."""
    url = urllib.parse.urlsplit(raw.strip())
    if url.scheme not in ("http", "https") or not url.hostname:
        raise ValueError("must be an http:// or https:// host:port URL")
    default_port = 443 if url.scheme == "https" else 80
    return url.scheme, url.hostname, url.port or default_port, url.path.rstrip("/")


def _open_upstream(scheme, host, port, timeout):
    """A verified TLS context for https. There is no skip."""
    if scheme == "https":
        context = ssl.create_default_context()
        return http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _port_from_env(raw):
    port = int(raw.strip())
    if not 0 <= port <= 65535:
        raise ValueError("must be a port number")
    return port


def _settings():
    """Resolve every setting, naming the variable that is wrong."""
    resolved = {}
    for name, raw, parse in (
            ("SHIM_VLLM_URL", os.environ.get("SHIM_VLLM_URL", DEFAULT_VLLM),
             _upstream_from_env),
            ("SHIM_PORT", os.environ.get("SHIM_PORT", "8092"), _port_from_env),
            ("SHIM_TIMEOUT_S", os.environ.get("SHIM_TIMEOUT_S"), _timeout_from_env)):
        try:
            resolved[name] = parse(raw)
        except ValueError as e:
            raise ValueError(f"{name}={raw!r}: {e}") from None
    return resolved


class Shim(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _client_hung_up(self, upstream):
        """Block until upstream answers (False) or the gateway hangs up (True).

        No timeout: select only detects a vanished party, it bounds nothing.
        """
        watch = [self.connection, upstream.sock]
        while True:
            ready, _, _ = select.select(watch, [], [])
            if upstream.sock in ready:
                return False
            try:
                if not self.connection.recv(1, socket.MSG_PEEK):
                    return True
            except OSError:
                return True
            # A pipelined next request is left unread; wait on upstream alone.
            watch = [upstream.sock]

    def _request_authorization(self):
        """The caller's Authorization, or None. Nothing is invented when the caller sent none."""
        raw = self.headers.get("Authorization")
        if raw is None:
            return None
        text = raw.strip()
        return text or None

    def _relay(self, upstream_path, body=None, method="GET"):
        upstream = None
        auth = self._request_authorization()
        try:
            scheme, host, port, prefix = _upstream_from_env(
                os.environ.get("SHIM_VLLM_URL", DEFAULT_VLLM))
            upstream = _open_upstream(
                scheme, host, port,
                _timeout_from_env(os.environ.get("SHIM_TIMEOUT_S")))
            headers = {} if body is None else {"Content-Type": "application/json"}
            if auth:
                headers["Authorization"] = auth
            upstream.request(method, prefix + upstream_path, body=body,
                             headers=headers)
            if self._client_hung_up(upstream):
                self.close_connection = True
                return
            r = upstream.getresponse()
            payload, status = r.read(), r.status
        except Exception as e:
            payload = f'{{"error":"shim upstream failure: {type(e).__name__}"}}'.encode()
            status = 502
        finally:
            if upstream is not None:
                upstream.close()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        target = POST_MAP.get(self.path.split("?")[0])
        if target is None:
            return self._fail(404, f"shim maps only {sorted(POST_MAP)}")
        raw = self.headers.get("Content-Length")
        if raw is None:
            return self._fail(411, "shim needs Content-Length")
        if not (raw.isascii() and raw.isdigit()):
            return self._fail(400, "bad Content-Length")
        self._relay(target, body=self.rfile.read(int(raw)), method="POST")

    def do_GET(self):
        target = GET_MAP.get(self.path.split("?")[0])
        if target is None:
            return self._fail(404, f"shim maps only {sorted(GET_MAP)}")
        self._relay(target)

    def _fail(self, code, msg):
        b = f'{{"error":"{msg}"}}'.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b)
        # An unread request body must not be parsed as the next request.
        self.close_connection = True

    def log_message(self, fmt, *a):
        sys.stderr.write("shim: " + fmt % a + "\n")


class Threaded(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    try:
        cfg = _settings()
    except ValueError as e:
        print(f"rerank shim: {e}", file=sys.stderr, flush=True)
        sys.exit(2)
    HOST = os.environ.get("SHIM_HOST", "127.0.0.1").strip()
    LISTEN = cfg["SHIM_PORT"]
    VLLM = os.environ.get("SHIM_VLLM_URL", DEFAULT_VLLM).strip().rstrip("/")
    print(f"rerank shim: {HOST}:{LISTEN}/v1/reranking -> {VLLM}/v1/rerank", flush=True)
    if HOST not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: binding {HOST} exposes an UNAUTHENTICATED reranking "
              f"endpoint beyond loopback -- see this module's docstring.", flush=True)
    Threaded((HOST, LISTEN), Shim).serve_forever()
