"""The gateway must never leak a client's own gateway-auth token to an LLM
backend, and a backend that DOES need its own credential (a paid cloud API)
gets it from LLM_BACKENDS_JSON's token_env — never a literal secret in env,
never the client's header. See shared-memory/ops/README.md."""
import asyncio
import importlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


class _HeaderCaptureSession:
    """Records the headers/body handed to .request() then aborts before any
    real network call — mirrors _BoomSession in test_pool_status.py."""
    closed = False

    def __init__(self):
        self.captured_headers = None
        self.captured_data = None

    def request(self, *a, **kw):
        self.captured_headers = kw.get("headers")
        self.captured_data = kw.get("data")
        raise RuntimeError("capture-only session — no real upstream call")


class _Req:
    method = "POST"
    path = "/v1/chat/completions"        # not in ROUTING_MAP -> the LLM pool branch
    rel_url = "/v1/chat/completions"
    headers = {"Authorization": "Bearer client-gateway-token"}
    can_read_body = True

    async def read(self):
        return b'{"messages":[],"model":"local-model"}'


def test_client_authorization_never_forwarded_to_llm_backend(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    import hive_mind_proxy as g
    importlib.reload(g)

    proxy = g.AsyncHiveMindProxy()
    session = _HeaderCaptureSession()
    proxy.session = session
    asyncio.run(proxy.handle_proxy(_Req()))

    assert session.captured_headers is not None
    assert "Authorization" not in session.captured_headers
    assert "authorization" not in {k.lower() for k in session.captured_headers}


def test_backend_token_env_injected_as_authorization(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-123")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY", "model": "deepseek-chat"},
    ]))
    import hive_mind_proxy as g
    importlib.reload(g)
    assert g.LLM_BACKENDS == ["https://api.deepseek.com/v1"]
    assert g.LLM_BACKEND_TOKENS["https://api.deepseek.com/v1"] == "sk-test-123"

    proxy = g.AsyncHiveMindProxy()
    session = _HeaderCaptureSession()
    proxy.session = session
    asyncio.run(proxy.handle_proxy(_Req()))

    assert session.captured_headers["Authorization"] == "Bearer sk-test-123"


def test_missing_token_env_excludes_backend_not_silently_unauthenticated(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://local:5000"},
        {"url": "https://api.x.ai/v1", "token_env": "XAI_API_KEY"},
    ]))
    import hive_mind_proxy as g
    importlib.reload(g)

    assert g.LLM_BACKENDS == ["http://local:5000"]
    assert "https://api.x.ai/v1" not in g.LLM_BACKENDS


def test_legacy_llm_backends_unaffected_when_json_unset(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000@2,http://b:4000")
    import hive_mind_proxy as g
    importlib.reload(g)

    assert set(g.LLM_BACKENDS) == {"http://a:5000", "http://b:4000"}
    assert g.LLM_WEIGHTS["http://a:5000"] == 2.0
    assert g.LLM_BACKEND_TOKENS["http://a:5000"] is None
    assert g.LLM_BACKEND_MODELS["http://a:5000"] is None


def test_backend_model_override_rewrites_request_body(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "model": "custom-model-id"},
    ]))
    import hive_mind_proxy as g
    importlib.reload(g)

    proxy = g.AsyncHiveMindProxy()
    session = _HeaderCaptureSession()
    proxy.session = session
    asyncio.run(proxy.handle_proxy(_Req()))

    body = json.loads(session.captured_data)
    assert body["model"] == "custom-model-id"


def test_all_json_backends_excluded_falls_back_not_crashes(monkeypatch):
    """Every entry needs a token_env that isn't set -> the pool must still be
    non-empty (falls back to LLM_BACKENDS/DEFAULT_TARGET) so _select_llm_backend
    never raises. Reasoning-LLM outages must degrade per-request (503/504), never
    take down the whole gateway process — save/search don't touch this pool at all."""
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.x.ai/v1", "token_env": "XAI_API_KEY"},
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY"},
    ]))
    import hive_mind_proxy as g
    importlib.reload(g)

    assert len(g.LLM_BACKENDS) > 0                  # fell back, never empty
    assert g._select_llm_backend("", None)           # does not raise


def test_embedder_target_never_gets_authorization_either(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    import hive_mind_proxy as g
    importlib.reload(g)

    class _EmbedReq:
        method = "POST"
        path = "/v1/embeddings"           # IS in ROUTING_MAP -> not the LLM branch
        rel_url = "/v1/embeddings"
        headers = {"Authorization": "Bearer client-gateway-token"}
        can_read_body = True
        content_length = 40

        async def read(self):
            return b'{"input":"hello","model":"bge-m3"}'

    proxy = g.AsyncHiveMindProxy()
    session = _HeaderCaptureSession()
    proxy.session = session
    asyncio.run(proxy.handle_proxy(_EmbedReq()))

    assert "Authorization" not in (session.captured_headers or {})
