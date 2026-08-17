"""S-04 (Critical, Credential_Custody_Plan PR A5): a request bound for a
credentialed backend (one with a resolved token_env) may only be POST to a
framework-owned endpoint. Everything else gets 403 before any upstream call
is attempted, plus one credential-audit line — never the key itself.

Binds ONLY the credentialed branch: an uncredentialed backend (no token_env)
keeps today's full pass-through, unaffected.

Reload pattern (coordinator then hive_mind_proxy) mirrors tests/test_llm_
fault_origin.py's proven approach for tests that need a real credential-
audit-log file write."""
import asyncio
import importlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


@pytest.fixture(autouse=True)
def _isolated_route_denial_counter():
    """Same isolation contract as test_llm_fault_origin.py's
    _isolated_fault_counters — this counter is a process-lifetime module
    global mutated directly by the real proxy code path."""
    import coordinator
    coordinator._credential_counters["credentialed_route_denied"] = 0
    yield
    coordinator._credential_counters["credentialed_route_denied"] = 0
    coordinator._credential_audit_writer = None


class _MustNotCallSession:
    """.request() raising AssertionError proves the 403 short-circuit fired
    before any upstream call was attempted — a RuntimeError (as the capture
    sessions elsewhere in this suite use) would be swallowed by handle_proxy's
    own exception handling and read as "passed the gate, then failed to
    connect", which is the wrong signal here."""
    closed = False

    def request(self, *a, **kw):
        raise AssertionError("must not reach the upstream call — the allowlist should have 403'd first")


class _HeaderCaptureSession:
    """Records headers/body then aborts before any real network call —
    mirrors the same-named class in test_llm_backend_secrets.py."""
    closed = False

    def __init__(self):
        self.captured_headers = None

    def request(self, *a, **kw):
        self.captured_headers = kw.get("headers")
        raise RuntimeError("capture-only session — no real upstream call")


def _req(method: str, path: str):
    class _Req:
        pass
    r = _Req()
    r.method = method
    r.path = path
    r.rel_url = path
    r.headers = {}
    r.can_read_body = True

    async def read():
        return b'{"messages":[],"model":"local-model"}'
    r.read = read
    return r


def _load_credentialed_gateway(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-allowlist-test")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY", "model": "deepseek-chat"},
    ]))
    import hive_mind_proxy as g
    importlib.reload(g)
    return g


def test_post_chat_completions_to_credentialed_backend_passes(monkeypatch):
    g = _load_credentialed_gateway(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    session = _HeaderCaptureSession()
    proxy.session = session
    resp = asyncio.run(proxy.handle_proxy(_req("POST", "/v1/chat/completions")))
    # The capture session raises RuntimeError once .request() is actually
    # called -- handle_proxy's own exception handling turns that into a 500,
    # which is proof the allowlist let the call through (not a 403).
    assert resp.status != 403
    assert session.captured_headers is not None
    assert session.captured_headers["Authorization"] == "Bearer sk-allowlist-test"


def test_get_to_credentialed_backend_403s_before_any_upstream_call(monkeypatch):
    g = _load_credentialed_gateway(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _MustNotCallSession()
    resp = asyncio.run(proxy.handle_proxy(_req("GET", "/v1/chat/completions")))
    assert resp.status == 403
    body = json.loads(resp.body.decode())
    assert "sk-allowlist-test" not in json.dumps(body)
    # Honest, non-leaky: names the RULE, not the backend roster.
    assert "framework endpoints" in body["error"]
    assert "deepseek" not in body["error"].lower()


def test_arbitrary_path_to_credentialed_backend_403s(monkeypatch):
    """Not just wrong method -- a POST to a path the framework never calls
    (e.g. an admin endpoint on the provider) is denied too."""
    g = _load_credentialed_gateway(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _MustNotCallSession()
    resp = asyncio.run(proxy.handle_proxy(_req("POST", "/v1/admin/delete-everything")))
    assert resp.status == 403


def test_uncredentialed_backend_keeps_full_pass_through(monkeypatch):
    """The allowlist binds ONLY the credentialed branch -- a local backend
    with no token_env is unaffected, same as before this change."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    import hive_mind_proxy as g
    importlib.reload(g)

    proxy = g.AsyncHiveMindProxy()
    session = _HeaderCaptureSession()
    proxy.session = session
    resp = asyncio.run(proxy.handle_proxy(_req("GET", "/some/arbitrary/path")))
    assert resp.status != 403
    assert session.captured_headers is not None


def test_denied_route_bumps_credential_counter_and_writes_audit_line(monkeypatch, tmp_path):
    log_path = tmp_path / "credential-audit.jsonl"
    monkeypatch.setenv("CREDENTIAL_AUDIT_LOG_PATH", str(log_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-allowlist-test")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY"},
    ]))
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _MustNotCallSession()
    resp = asyncio.run(proxy.handle_proxy(_req("DELETE", "/v1/models")))
    assert resp.status == 403

    asyncio.run(coordinator._credential_audit_writer.flush())
    assert coordinator._credential_counters["credentialed_route_denied"] == 1
    assert coordinator._credential_last_ts["credentialed_route_denied"] is not None
    content = log_path.read_text()
    assert '"event":"credentialed_route_denied"' in content
    assert '"method":"DELETE"' in content
    assert '"path":"/v1/models"' in content
    assert "sk-allowlist-test" not in content


# ── Mutation check target ────────────────────────────────────────────────────
# See A5_HANDOFF.md's mutation-check table: inverting the `route not in
# CREDENTIALED_BACKEND_ALLOWED_ROUTES` condition in handle_proxy makes
# test_get_to_credentialed_backend_403s_before_any_upstream_call and
# test_post_chat_completions_to_credentialed_backend_passes both fail (the
# allowed route starts 403ing, the denied one starts reaching the session).
