"""The gateway must never leak a client's own gateway-auth token to an LLM
backend, and a backend that DOES need its own credential (a paid cloud API)
gets it from LLM_BACKENDS_JSON's token_env — never a literal secret in env,
never the client's header. See shared-memory/ops/README.md."""
import asyncio
import hashlib
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
    # private_ok: true (M-5, Model_Attributes_Routing_Plan_2026-08-18) — this
    # test is about token_env→Authorization injection, not the M-5 startup
    # choice (that has its own coverage in test_model_attributes_routing.py);
    # explicit here so role-less traffic stays eligible for this backend.
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY",
         "model": "deepseek-chat", "private_ok": True},
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


def test_literal_secret_field_rejected_not_silently_dropped(monkeypatch):
    """A mistaken raw "token"/"api_key" field (instead of token_env) must exclude
    that backend loudly, not silently strip the field and leave it tokenless --
    the whole point is to catch the exact mistake that would otherwise put a
    real secret in a file this framework reads (.env / an EnvironmentFile)."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://local:5000"},
        {"url": "https://api.deepseek.com/v1", "token": "sk-oops-a-literal-secret"},
        {"url": "https://api.x.ai/v1", "api_key": "xai-oops-another-literal-secret"},
    ]))
    import hive_mind_proxy as g
    importlib.reload(g)

    assert g.LLM_BACKENDS == ["http://local:5000"]
    assert "https://api.deepseek.com/v1" not in g.LLM_BACKENDS
    assert "https://api.x.ai/v1" not in g.LLM_BACKENDS
    assert "sk-oops-a-literal-secret" not in (g.LLM_BACKEND_TOKENS.values())
    assert "xai-oops-another-literal-secret" not in (g.LLM_BACKEND_TOKENS.values())


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


def test_backend_whose_keyfile_holds_a_control_character_is_excluded(
    monkeypatch, tmp_path, caplog, capsys
):
    """End-to-end for v0.9.63 (measured live 2026-08-26): an operator writes
    the key file with an editor/paste that leaves an embedded control
    character. Before, the value reached the Authorization header and aiohttp
    refused EVERY upstream request with "Forbidden control character detected
    in headers" — per call, backend '?', nothing naming the key file. Now the
    reader refuses ONCE at load and the backend takes the SAME exclusion path
    an unset token_env already takes: excluded from the pool, one log line
    naming it. Fake key value only."""
    import logging
    import secure_env
    # This test is the only one here that drives secure_env's FILE tier, so
    # it owns the in-process store for its duration — monkeypatch hands the
    # original dict back at teardown, so a resolved value can never leak into
    # a later test's view of get_secret().
    monkeypatch.setattr(secure_env, "_secrets", {})

    keyfile = tmp_path / "deepseek_api_key"
    keyfile.write_bytes(b"sk-test\r-embedded-cr\n")   # fake, corrupt on purpose
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY_FILE", str(keyfile))
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://local:5000"},
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY",
         "model": "deepseek-chat", "private_ok": True},
    ]))
    import hive_mind_proxy as g
    with caplog.at_level(logging.WARNING, logger="hive-proxy"):
        importlib.reload(g)

    assert g.LLM_BACKENDS == ["http://local:5000"]
    assert "https://api.deepseek.com/v1" not in g.LLM_BACKENDS
    # The pool log names the backend that was excluded and why.
    assert any("api.deepseek.com" in r.getMessage()
               and "DEEPSEEK_API_KEY" in r.getMessage()
               and "excluding this backend" in r.getMessage()
               for r in caplog.records)
    # The reader's own line named the FILE — the thing the operator must fix.
    err = capsys.readouterr().err
    assert str(keyfile) in err
    assert "\\x0d" in err
    assert "printf '%s'" in err
    # No secret content anywhere, in either surface.
    assert "sk-test" not in err
    assert not any("sk-test" in r.getMessage() for r in caplog.records)


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


class _HealthProbeResp:
    status = 200


class _HealthProbeCm:
    async def __aenter__(self):
        return _HealthProbeResp()

    async def __aexit__(self, *a):
        return False


class _HealthProbeSession:
    """No real network — every probe (/health, /v1/models) just reports 200."""
    def get(self, url, timeout=None):
        return _HealthProbeCm()


def _health_request(headers):
    """Minimal stand-in for aiohttp.web.Request as handle_health uses it —
    mirrors the MagicMock pattern in test_backup_quiesce.py's
    test_health_surfaces_backup_in_progress."""
    class _Req:
        pass
    req = _Req()
    req.headers = headers
    req.app = {"proxy": None}  # patched in below
    return req


def test_health_config_hides_credential_fields_from_unauthenticated_caller(monkeypatch):
    """/health itself stays unauthenticated (liveness must work without a token) —
    but a caller presenting no valid bearer token, ON AN AUTH-CONFIGURED
    INSTALL (SEC-A5-03, PR A5 fix round: slimming applies only when
    AUTH_CONFIGURED_AT_STARTUP is true), gets the S-10 anonymous-slim shape
    (status/version/api_version only), so the backend roster — and any
    credential attached to it — never reaches them at all, not just the two
    fields that used to be hidden inside it."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-should-never-leak")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY", "model": "deepseek-chat"},
    ]))
    monkeypatch.setenv("AGENT_TOKENS", "claude:tok_health_hide_test")
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)  # see test_auth.load_coordinator's docstring
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    assert g.AUTH_CONFIGURED_AT_STARTUP is True

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    req = _health_request({})                  # no Authorization header
    req.app = {"proxy": proxy}

    body = json.loads(asyncio.run(g.handle_health(req)).body.decode())
    assert set(body.keys()) == {"status", "version", "api_version"}
    assert "config" not in body
    assert "sk-should-never-leak" not in json.dumps(body)


def test_health_config_shows_bool_and_model_for_authenticated_caller_never_the_token(monkeypatch):
    """A caller WITH a valid gateway bearer token may see has_credential (bool)
    and the model override -- but never the raw token value, under any caller."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-should-never-leak")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY", "model": "deepseek-chat"},
        {"url": "http://localhost:5000"},
    ]))
    import hive_mind_proxy as g
    importlib.reload(g)
    import coordinator
    coordinator._AGENT_TOKENS.clear()
    # PR A2: the registry is digest-keyed — store the SHA-256 digest of the
    # presented token, never the raw value (coordinator._token_digest()).
    coordinator._AGENT_TOKENS[hashlib.sha256(b"tok_valid_test").hexdigest()] = "claude"
    try:
        proxy = g.AsyncHiveMindProxy()
        proxy.session = _HealthProbeSession()
        req = _health_request({"Authorization": "Bearer tok_valid_test"})
        req.app = {"proxy": proxy}

        body = json.loads(asyncio.run(g.handle_health(req)).body.decode())
        by_url = {e["url"]: e for e in body["config"]["llm_backends"]}
        assert by_url["https://api.deepseek.com/v1"]["has_credential"] is True
        assert by_url["https://api.deepseek.com/v1"]["model"] == "deepseek-chat"
        assert by_url["http://localhost:5000"]["has_credential"] is False
        assert by_url["http://localhost:5000"]["model"] is None
        # The raw secret must never appear anywhere in the response, authenticated or not.
        assert "sk-should-never-leak" not in json.dumps(body)
    finally:
        coordinator._AGENT_TOKENS.clear()


class _FailSession:
    """Raises a given exception on every .request() -- models what a real
    unreachable/erroring backend looks like, so error-path RESPONSES (what a
    valid, full-access agent token actually sees back) can be inspected for a
    leaked secret, not just the outbound call. This is the realistic threat:
    a legitimate coder's agent, possibly reaching the gateway remotely over an
    SSH tunnel (a supported topology, see SKILL.md 10a), not an anonymous
    attacker -- so every client-visible surface, including error bodies and
    response headers, must never carry the raw token, only its own gateway
    identity is ever meant to be visible to it."""
    closed = False

    def __init__(self, exc):
        self._exc = exc

    def request(self, *a, **kw):
        raise self._exc


def test_token_never_leaks_into_client_visible_error_response(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-must-never-appear-to-any-client")
    # private_ok: true (M-5) — see test_backend_token_env_injected_as_
    # authorization's comment above; this test needs role-less traffic to
    # actually reach dispatch (and then fail) to exercise the leak check.
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY",
         "model": "deepseek-chat", "private_ok": True},
    ]))
    import hive_mind_proxy as g
    importlib.reload(g)

    for exc in (
        g.ClientError("connection refused"),
        asyncio.TimeoutError(),
        RuntimeError("some unexpected failure mentioning the request object"),
    ):
        proxy = g.AsyncHiveMindProxy()
        proxy.session = _FailSession(exc)
        resp = asyncio.run(proxy.handle_proxy(_Req()))
        assert resp.status in (503, 504, 500)
        assert "sk-must-never-appear-to-any-client" not in resp.body.decode()
        assert all("sk-must-never-appear-to-any-client" not in str(v)
                   for v in resp.headers.values())


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
