#!/usr/bin/env python3
"""Path shim: the framework's /v1/reranking -> vLLM's /v1/rerank.

Optional. You only need this if you serve the RERANKER with vLLM instead of
llama.cpp. The embedder needs no shim -- vLLM already serves /v1/embeddings at
the path the framework expects.

WHY THIS EXISTS (measured 2026-08-25, endpoints.md 2.2/3):
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
  SHIM_TIMEOUT_S upstream read timeout       (default 900)
Then point the gateway at it:  RERANKER_URL=http://<SHIM_HOST>:<SHIM_PORT>

SHIM_HOST defaults to LOOPBACK deliberately. Neither vLLM nor a llama.cpp
encoder carries any authentication, so a shim bound to 0.0.0.0 publishes an
unauthenticated reranking endpoint to every interface on the host -- the same
exposure a previous release found in the encoder containers' publish spec.
Widen it only when you know what is listening.
"""
import http.server, socketserver, urllib.request, urllib.error, os, sys

VLLM   = os.environ.get("SHIM_VLLM_URL", "http://127.0.0.1:8090").rstrip("/")
HOST   = os.environ.get("SHIM_HOST", "127.0.0.1").strip()
LISTEN = int(os.environ.get("SHIM_PORT", "8092"))
TIMEOUT = float(os.environ.get("SHIM_TIMEOUT_S", "900"))

# The gateway calls exactly two things on a reranker base: the rerank POST and
# a GET /health liveness probe (hive_mind_proxy.py:3750). Anything else is a
# mistyped framework call and must fail loudly rather than be forwarded blind.
POST_MAP = {"/v1/reranking": "/v1/rerank"}
GET_MAP  = {"/health": "/health"}


class Shim(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _relay(self, upstream_path, body=None, method="GET"):
        req = urllib.request.Request(VLLM + upstream_path, data=body, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                payload, status = r.read(), r.status
        except urllib.error.HTTPError as e:
            payload, status = e.read(), e.code
        except Exception as e:
            payload = f'{{"error":"shim upstream failure: {type(e).__name__}"}}'.encode()
            status = 502
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        target = POST_MAP.get(self.path.split("?")[0])
        if target is None:
            return self._fail(404, f"shim maps only {sorted(POST_MAP)}")
        n = int(self.headers.get("Content-Length") or 0)
        self._relay(target, body=self.rfile.read(n), method="POST")

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
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, fmt, *a):
        sys.stderr.write("shim: " + fmt % a + "\n")


class Threaded(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    print(f"rerank shim: {HOST}:{LISTEN}/v1/reranking -> {VLLM}/v1/rerank", flush=True)
    if HOST not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: binding {HOST} exposes an UNAUTHENTICATED reranking "
              f"endpoint beyond loopback -- see this module's docstring.", flush=True)
    Threaded((HOST, LISTEN), Shim).serve_forever()
