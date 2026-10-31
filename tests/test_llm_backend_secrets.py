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

import pytest
pytestmark = pytest.mark.usefixtures("chat_caller_is_consolidation")
from yarl import URL

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


class _ReqBodyStream:
    """handle_encoder reads request.content via StreamReader.read(n)."""

    def __init__(self, body: bytes):
        self._buf = body

    async def read(self, n: int) -> bytes:
        if n < 0:
            out, self._buf = self._buf, b""
            return out
        out = self._buf[:n]
        self._buf = self._buf[n:]
        return out


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
    rel_url = URL("/v1/chat/completions", encoded=True)
    headers = {"Authorization": "Bearer client-gateway-token"}
    can_read_body = True

    def __init__(self, model="local-model"):
        # S5 (v0.9.97): a credentialed backend serves only its declared model.
        # Tests that must REACH the upstream pass the backend's declared model
        # so the refuse-not-rewrite check does not fire.
        self._model = model

    async def read(self):
        return json.dumps({"messages": [], "model": self._model}).encode()


def test_client_authorization_never_forwarded_to_llm_backend(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([{"url": "http://a:5000", "private_ok": True, "n_ctx": 8192, "max_output_tokens": 2048}]))
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
         "model": "deepseek-chat", "private_ok": True, "n_ctx": 8192, "max_output_tokens": 2048},
    ]))
    import hive_mind_proxy as g
    importlib.reload(g)
    assert g.LLM_BACKENDS == ["https://api.deepseek.com/v1"]
    assert g.LLM_BACKEND_TOKENS["https://api.deepseek.com/v1"] == "sk-test-123"

    proxy = g.AsyncHiveMindProxy()
    session = _HeaderCaptureSession()
    proxy.session = session
    asyncio.run(proxy.handle_proxy(_Req(model="deepseek-chat")))

    assert session.captured_headers["Authorization"] == "Bearer sk-test-123"


def test_missing_token_env_excludes_backend_not_silently_unauthenticated(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://local:5000", "n_ctx": 8192, "max_output_tokens": 2048},
        {"url": "https://api.x.ai/v1", "token_env": "XAI_API_KEY", "n_ctx": 8192, "max_output_tokens": 2048},
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
        {"url": "http://local:5000", "n_ctx": 8192, "max_output_tokens": 2048},
        {"url": "https://api.deepseek.com/v1", "token": "sk-oops-a-literal-secret", "n_ctx": 8192, "max_output_tokens": 2048},
        {"url": "https://api.x.ai/v1", "api_key": "xai-oops-another-literal-secret", "n_ctx": 8192, "max_output_tokens": 2048},
    ]))
    import hive_mind_proxy as g
    importlib.reload(g)

    assert g.LLM_BACKENDS == []
    assert "sk-oops-a-literal-secret" not in "".join(str(v) for v in g.LLM_BACKEND_TOKENS.values())
    assert "xai-oops-another-literal-secret" not in "".join(str(v) for v in g.LLM_BACKEND_TOKENS.values())
    declared = " ".join(f"{entry} {field}" for entry, field in g.LLM_DECLARATION_ERRORS)
    assert "sk-oops-a-literal-secret" not in declared
    assert "xai-oops-another-literal-secret" not in declared
    assert ("https://api.deepseek.com/v1", "token") in g.LLM_DECLARATION_ERRORS
    assert ("https://api.x.ai/v1", "api_key") in g.LLM_DECLARATION_ERRORS
    with pytest.raises(SystemExit) as exc:
        g.require_valid_llm_routing_config()
    assert exc.value.code == 78
    assert "sk-oops-a-literal-secret" not in str(exc.value)
    assert "xai-oops-another-literal-secret" not in str(exc.value)


def test_legacy_llm_backends_unaffected_when_json_unset(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", "[{\"url\": \"http://a:5000\", \"n_ctx\": 8192, \"max_output_tokens\": 2048, \"weight\": 2}, {\"url\": \"http://b:4000\", \"n_ctx\": 8192, \"max_output_tokens\": 2048}]")
    import hive_mind_proxy as g
    importlib.reload(g)

    assert set(g.LLM_BACKENDS) == {"http://a:5000", "http://b:4000"}
    assert g.LLM_WEIGHTS["http://a:5000"] == 2.0
    assert g.LLM_BACKEND_TOKENS["http://a:5000"] is None
    assert g.LLM_BACKEND_MODELS["http://a:5000"] is None


def test_backend_model_override_rewrites_request_body(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "model": "custom-model-id", "private_ok": True, "n_ctx": 8192, "max_output_tokens": 2048},
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
        {"url": "http://local:5000", "n_ctx": 8192, "max_output_tokens": 2048},
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY",
         "model": "deepseek-chat", "private_ok": True, "n_ctx": 8192, "max_output_tokens": 2048},
    ]))
    import hive_mind_proxy as g
    with caplog.at_level(logging.WARNING, logger="hive-proxy"):
        importlib.reload(g)

    assert g.LLM_BACKENDS == ["http://local:5000"]
    assert "https://api.deepseek.com/v1" not in g.LLM_BACKENDS
    # The pool log names the backend that was excluded and why. The wording
    # must cover THIS cause too: the variable is not "not set" here — the key
    # file exists and was read, and secure_env refused what was in it. An
    # operator told only "not set" goes looking for a missing export instead
    # of at the [secure_env] line that names the file they must rewrite.
    _excl = [r.getMessage() for r in caplog.records
             if "api.deepseek.com" in r.getMessage()
             and "DEEPSEEK_API_KEY" in r.getMessage()
             and "token_refused" in r.getMessage()]
    assert _excl
    assert any("Fill the file named by DEEPSEEK_API_KEY_FILE" in m for m in _excl)
    assert not any("is not set in the gateway's own environment" in m
                   for m in _excl)
    # The reader's own line named the FILE — the thing the operator must fix.
    err = capsys.readouterr().err
    assert str(keyfile) in err
    assert "\\x0d" in err
    assert "printf '%s'" in err
    # No secret content anywhere, in either surface.
    assert "sk-test" not in err
    assert not any("sk-test" in r.getMessage() for r in caplog.records)


def test_all_json_backends_excluded_leaves_an_empty_pool_and_health_says_so(monkeypatch):
    """Every entry's token is missing. The pool stays empty. There is no fallback target. The dependency is degraded and names token_missing."""
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.x.ai/v1", "token_env": "XAI_API_KEY", "n_ctx": 8192, "max_output_tokens": 2048},
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY", "n_ctx": 8192, "max_output_tokens": 2048},
    ]))
    import hive_mind_proxy as g
    importlib.reload(g)

    assert g.LLM_BACKENDS == []
    assert g._select_llm_backend(None) is None
    assert not hasattr(g, "LLM_POOL_FALLBACK_REASON")
    dep = g._llm_pool_dependency({})
    assert dep["state"] == "degraded"
    assert "token_missing" in dep["reason"]
    assert "fallback" not in dep["reason"]
    assert "api.x.ai" in dep["reason"]
    assert "api.deepseek.com" in dep["reason"]


def test_json_entry_with_openai_base_url_key_is_excluded_loudly(monkeypatch, caplog):
    """OpenAI SDK calls the field base_url. This schema's key is url.
    A hand-written LLM_BACKENDS_JSON that uses the SDK name used to be
    skipped with no log (empty url → continue), the pool fell back to
    localhost:5000, and the operator saw a 'blocked' gateway with no
    pointer at the typo. Exclude as before — but name the wrong key."""
    import logging
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"base_url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY",
         "private_ok": True},
    ]))
    import hive_mind_proxy as g
    with caplog.at_level(logging.DEBUG, logger="hive-proxy"):
        importlib.reload(g)

    assert "https://api.deepseek.com/v1" not in g.LLM_BACKENDS
    assert g.LLM_BACKENDS == []
    with pytest.raises(SystemExit) as exc:
        g.require_valid_llm_routing_config()
    assert exc.value.code == 78
    assert "field url" in str(exc.value)
    # Do not echo the value: a mistaken key can still carry a credentialed URL.
    assert "api.deepseek.com" not in str(exc.value)
    assert not any("api.deepseek.com" in r.getMessage() for r in caplog.records)


def test_json_entry_missing_url_entirely_is_excluded_loudly(monkeypatch, caplog):
    import logging
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"token_env": "DEEPSEEK_API_KEY", "private_ok": True},
    ]))
    import hive_mind_proxy as g
    with caplog.at_level(logging.DEBUG, logger="hive-proxy"):
        importlib.reload(g)

    assert g.LLM_BACKENDS == []
    with pytest.raises(SystemExit) as exc:
        g.require_valid_llm_routing_config()
    assert exc.value.code == 78
    assert "field url" in str(exc.value)
    assert "DEEPSEEK_API_KEY" not in str(exc.value)


class _HealthProbeResp:
    status = 200


class _HealthProbeCm:
    async def __aenter__(self):
        return _HealthProbeResp()

    async def __aexit__(self, *a):
        return False


class _HealthProbeSession:
    """No real network — every probe (/health, /v1/models) just reports 200."""
    def get(self, url, timeout=None, headers=None, **_kw):
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
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY", "model": "deepseek-chat", "n_ctx": 8192, "max_output_tokens": 2048},
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
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY", "model": "deepseek-chat", "n_ctx": 8192, "max_output_tokens": 2048},
        {"url": "http://localhost:5000", "n_ctx": 8192, "max_output_tokens": 2048},
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
        # ⛔ THE ASSERTION MOVES WITH THE KEY, IT IS NEVER DROPPED. The
        # configured roster is served from `/memory/telemetry` → `config`, and
        # `telemetry_extras()` is what builds it — so that is where the
        # has_credential/model contract is pinned now.
        cfg = g.telemetry_extras()["config"]
        by_url = {e["url"]: e for e in cfg["llm_backends"]}
        assert by_url["https://api.deepseek.com/v1"]["has_credential"] is True
        assert by_url["https://api.deepseek.com/v1"]["model"] == "deepseek-chat"
        assert by_url["http://localhost:5000"]["has_credential"] is False
        assert by_url["http://localhost:5000"]["model"] is None
        # The raw secret must never appear in either payload, authenticated or not.
        assert "sk-should-never-leak" not in json.dumps(body)
        assert "sk-should-never-leak" not in json.dumps(cfg)
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
         "model": "deepseek-chat", "private_ok": True, "n_ctx": 8192, "max_output_tokens": 2048},
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
        resp = asyncio.run(proxy.handle_proxy(_Req(model="deepseek-chat")))
        assert resp.status in (503, 504, 500)
        assert "sk-must-never-appear-to-any-client" not in resp.body.decode()
        assert all("sk-must-never-appear-to-any-client" not in str(v)
                   for v in resp.headers.values())


def test_embedder_target_never_gets_authorization_either(monkeypatch):
    """R-A (HYG round): driven through handle_ENCODER, not handle_proxy.

    The prefix loop that used to route /v1/embeddings inside handle_proxy is
    GONE — the path is its own registered route now. Left on handle_proxy this
    test would still pass, but only because an embed request would fall into
    the reasoning-LLM pool and land on an UNCREDENTIALED local backend, which
    attaches no Authorization for reasons that have nothing to do with the
    embedder. That is a false green: it would assert the property while
    exercising the wrong path."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", "[{\"url\": \"http://a:5000\", \"n_ctx\": 8192, \"max_output_tokens\": 2048}]")
    import hive_mind_proxy as g
    importlib.reload(g)

    class _EmbedReq:
        method = "POST"
        path = "/v1/embeddings"           # its own registered route -> handle_encoder
        rel_url = URL("/v1/embeddings", encoded=True)
        headers = {"Authorization": "Bearer client-gateway-token"}
        can_read_body = True
        content_length = 40

        def __init__(self):
            self._raw = b'{"input":"hello","model":"bge-m3"}'
            self.content = _ReqBodyStream(self._raw)

        async def read(self):
            return self._raw

    proxy = g.AsyncHiveMindProxy()
    session = _HeaderCaptureSession()
    proxy.session = session
    asyncio.run(proxy.handle_encoder(_EmbedReq()))

    assert "Authorization" not in (session.captured_headers or {})


def test_a_credentialed_backend_over_plaintext_to_a_remote_host_is_excluded(monkeypatch, caplog):
    """Operator security ruling 2026-08-28: the gateway never sends a provider
    key in the clear — not from the /health probe, not from a real call. A
    credentialed backend whose URL is http to a non-loopback host is excluded
    at load, with one ERROR line naming the URL (scrubbed) and the token_env;
    a loopback http backend and an https backend are accepted."""
    import logging
    monkeypatch.setenv("REMOTE_KEY", "sk-remote")
    monkeypatch.setenv("LOCAL_KEY", "sk-local")
    monkeypatch.setenv("CLOUD_KEY", "sk-cloud")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://8.8.8.8:8000/v1", "token_env": "REMOTE_KEY", "private_ok": True, "n_ctx": 8192, "max_output_tokens": 2048},   # PUBLIC, plaintext
        {"url": "http://127.0.0.1:5001/v1", "token_env": "LOCAL_KEY", "private_ok": True, "n_ctx": 8192, "max_output_tokens": 2048},
        {"url": "https://api.example.com/v1", "token_env": "CLOUD_KEY", "private_ok": True, "n_ctx": 8192, "max_output_tokens": 2048},
        {"url": "http://192.168.1.9:5000", "n_ctx": 8192, "max_output_tokens": 2048},   # uncredentialed plaintext is fine
        {"url": "http://llama-box:8000/v1", "token_env": "LOCAL_KEY", "private_ok": True, "n_ctx": 8192, "max_output_tokens": 2048},        # LAN name
        {"url": "http://100.101.102.103:8000/v1", "token_env": "LOCAL_KEY", "private_ok": True, "n_ctx": 8192, "max_output_tokens": 2048},  # tailnet
        {"url": "http://1.1.1.1:8000/v1", "token_env": "LOCAL_KEY", "private_ok": True, "plaintext_ok": True, "n_ctx": 8192, "max_output_tokens": 2048},  # operator-asserted
    ]))
    import hive_mind_proxy as g
    with caplog.at_level(logging.WARNING, logger="hive-proxy"):
        importlib.reload(g)
    assert "http://8.8.8.8:8000/v1" not in g.LLM_BACKENDS
    assert "http://llama-box:8000/v1" in g.LLM_BACKENDS
    assert "http://100.101.102.103:8000/v1" in g.LLM_BACKENDS
    assert "http://1.1.1.1:8000/v1" in g.LLM_BACKENDS
    assert "http://127.0.0.1:5001/v1" in g.LLM_BACKENDS
    assert "https://api.example.com/v1" in g.LLM_BACKENDS
    assert "http://192.168.1.9:5000" in g.LLM_BACKENDS
    line = [r.getMessage() for r in caplog.records if "plaintext_public_host" in r.getMessage()]
    assert line and any("8.8.8.8" in m for m in line)
    assert all("sk-remote" not in m and "sk-local" not in m and "sk-cloud" not in m for m in line)
    assert all("sk-remote" not in r.getMessage() for r in caplog.records)


def test_bearer_transport_rule_is_strict_about_odd_urls(monkeypatch):
    """The rule reads the parsed hostname, never the netloc: userinfo, ports,
    uppercase schemes and look-alike hosts cannot smuggle a bearer onto
    plaintext; an unparsable URL is refused."""
    import hive_mind_proxy as g
    ok = g._bearer_transport_ok
    assert ok("https://api.deepseek.com/v1")
    assert ok("HTTPS://API.DEEPSEEK.COM")
    assert ok("http://localhost:5000")
    assert ok("http://127.0.0.1:5000/v1")
    assert ok("http://[::1]:5000")
    assert ok("http://10.0.0.7:8000")                    # RFC1918 — the LAN
    assert ok("http://100.101.102.103:8000")            # Tailscale CGNAT
    assert ok("http://llama-box:8000")                      # unqualified LAN name
    assert ok("http://llama-box.lan:8000") and ok("http://box.tailnet.ts.net:8000")
    assert not ok("http://8.8.8.8:8000")             # public IP
    assert ok("http://8.8.8.8:8000", plaintext_ok=True)
    assert not ok("http://localhost.evil.com:80")
    assert not ok("http://localhost@8.8.8.8:8000")       # userinfo is not the host
    assert not ok("ftp://localhost")
    assert not ok("http://")
    assert not ok("")


def test_bearer_transport_rule_refuses_numeric_literal_hosts_the_resolver_accepts(monkeypatch):
    """SEC E (S3): a DOTLESS host can still be a numeric IPv4 literal in a
    form the RESOLVER accepts (inet_aton) even though ipaddress.ip_address
    already refused it as non-dotted-quad — decimal dword, 0x hex, and
    leading-zero octal all resolve to a real (here PUBLIC) address."""
    import hive_mind_proxy as g
    ok = g._bearer_transport_ok
    assert not ok("http://16909060:8000")        # decimal dword for 1.2.3.4
    assert not ok("http://0x7f000001:8000")      # hex for 127.0.0.1
    assert not ok("http://00100403004:8000")     # leading-zero octal -> 1.2.6.4, PUBLIC (measured)
    # Deliberate: bare alphabetic hostnames still pass — inet_aton refuses
    # both (no digit-only/0x/octal alphabet), including a hex-looking name
    # with no "0x" prefix.
    assert ok("http://myhost:8000")
    assert ok("http://beef:8000")


def test_the_gateway_cold_imports_with_a_credentialed_backend(tmp_path):
    """v0.9.75 review (Opus): every test here imports hive_mind_proxy through
    importlib.reload, which re-executes in a namespace where every name is
    ALREADY bound — so a helper defined below the module-level loader that
    calls it raised NameError on a real cold start and never in the suite.
    This is the only test that starts the interpreter the way systemd does.
    Fake key value only; nothing is contacted (import only)."""
    import subprocess, sys, os
    env = dict(os.environ)
    env.update({
        "DS_TEST_KEY": "sk-test-cold-import",
        "LLM_BACKENDS_JSON": json.dumps([
            {"url": "https://api.example.com/v1", "token_env": "DS_TEST_KEY",
             "model": "m", "private_ok": True, "n_ctx": 8192, "max_output_tokens": 2048},
            {"url": "http://llama-box:8000/v1", "token_env": "DS_TEST_KEY", "private_ok": True, "n_ctx": 8192, "max_output_tokens": 2048},
        ]),
        "PYTHONPATH": os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")),
    })
    r = subprocess.run(
        [sys.executable, "-c",
         "import hive_mind_proxy as g; print(sorted(g.LLM_BACKENDS))"],
        env=env, capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr[-1500:]
    assert "https://api.example.com/v1" in r.stdout and "http://llama-box:8000/v1" in r.stdout
    assert "sk-test-cold-import" not in r.stdout + r.stderr


# ── SEC A (R-1/R-2, ADV1-4/ADV2-6): backend URL credential hygiene at load ──
# Prove-failing-first evidence (run against unmodified hive_mind_proxy.py, at
# the commit just before this SEC-A fix): NONE of these behaviours existed.
# - A JSON entry with userinfo flowed straight into LLM_BACKENDS with its
#   credential intact; there was no _backend_url_credential_error(),
#   _LLM_BACKEND_URL_CREDENTIAL_ERRORS, or require_no_backend_url_credentials
#   at all (AttributeError on `g._LLM_BACKEND_URL_CREDENTIAL_ERRORS`).
# - The legacy CSV path had NO per-entry validation of any kind: an
#   entry.partition("@") (FIRST "@") mis-split "http://u:p@h:8000" into
#   url="http://u:p" (weight defaulted to 1.0, the real host "h:8000" and the
#   credential were both silently discarded) — no refusal, no exclusion, no
#   trace the entry was ever malformed.
# - "http://user@10" partitioned the same way old code would (bare
#   `partition`) into url="http://user", weight=10 — the embedded credential
#   "user" vanished into a discarded weight field.
# Confirmed by running exactly the assertions below against that code via
# `git stash` of only shared-memory/scripts/hive_mind_proxy.py.

_URL_CRED_SECRET = "sm-test-secret-9f3a"


def _declared(g) -> str:
    return " ".join(f"{entry} {field}" for entry, field in g.LLM_DECLARATION_ERRORS)


def test_json_entry_with_userinfo_excluded_and_flagged_for_fatal_refusal(monkeypatch):
    """A JSON backend URL with userinfo never reaches the pool. Import stays quiet. The startup guard refuses."""
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": f"https://leakuser:{_URL_CRED_SECRET}@backend.example.test/v1",
         "n_ctx": 8192, "max_output_tokens": 2048},
    ]))
    import hive_mind_proxy as g
    importlib.reload(g)   # must not raise
    assert not any("leakuser" in b or _URL_CRED_SECRET in b for b in g.LLM_BACKENDS)
    assert any(field == "url" for _entry, field in g.LLM_DECLARATION_ERRORS)
    assert _URL_CRED_SECRET not in _declared(g)
    assert "leakuser" not in _declared(g)
    with pytest.raises(SystemExit) as exc_info:
        g.require_no_backend_url_credentials()
    assert exc_info.value.code == 78
    assert _URL_CRED_SECRET not in str(exc_info.value)
    assert "leakuser" not in str(exc_info.value)
    assert "backend.example.test" in str(exc_info.value)


def test_csv_entry_with_userinfo_is_ignored_and_never_loaded(monkeypatch, caplog):
    """The CSV pool is not a declaration. Its userinfo never enters the pool or a log line."""
    import logging
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", f"http://leakuser:{_URL_CRED_SECRET}@backend.example.test:8000")
    import hive_mind_proxy as g
    with caplog.at_level(logging.DEBUG, logger="hive-proxy"):
        importlib.reload(g)
    assert g.LLM_BACKENDS == []
    assert _URL_CRED_SECRET not in " ".join(g.LLM_BACKENDS)
    assert all(_URL_CRED_SECRET not in r.getMessage() for r in caplog.records)
    g.require_no_backend_url_credentials()


def test_query_string_backend_url_loads_r2(monkeypatch):
    """A query string is not userinfo. The backend loads and startup does not refuse."""
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    azure_url = "https://my-res.openai.azure.com/openai?api-version=2024-02-01"
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": azure_url, "n_ctx": 8192, "max_output_tokens": 2048},
    ]))
    import hive_mind_proxy as g
    importlib.reload(g)
    assert azure_url in g.LLM_BACKENDS
    assert not any(field == "url" for _entry, field in g.LLM_DECLARATION_ERRORS)
    g.require_no_backend_url_credentials()


def test_csv_weight_forms_are_ignored(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "url@2,url2@1.5")
    import hive_mind_proxy as g
    importlib.reload(g)
    assert g.LLM_BACKENDS == []
    assert "url" not in g.LLM_WEIGHTS
    assert "url2" not in g.LLM_WEIGHTS


def test_json_weight_field_still_parses(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", "[{\"url\": \"http://a:5000\", \"n_ctx\": 8192, \"max_output_tokens\": 2048, \"weight\": 2}, {\"url\": \"http://b:4000\", \"n_ctx\": 8192, \"max_output_tokens\": 2048}]")
    import hive_mind_proxy as g
    importlib.reload(g)
    assert set(g.LLM_BACKENDS) == {"http://a:5000", "http://b:4000"}
    assert g.LLM_WEIGHTS["http://a:5000"] == 2.0
    assert not any(field == "url" for _entry, field in g.LLM_DECLARATION_ERRORS)


def test_csv_nan_weight_is_not_loaded(monkeypatch):
    """A CSV weight tail is not parsed. It is not admitted as a backend."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000@nan")
    import hive_mind_proxy as g
    importlib.reload(g)
    assert g.LLM_BACKENDS == []
    assert "http://a:5000" not in g.LLM_BACKENDS
    g.require_no_backend_url_credentials()


def test_ambiguous_user_at_numeric_host_not_silently_treated_as_weight(monkeypatch):
    """http://user@10 is userinfo. It must not load as url http://user with the name discarded."""
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://user@10", "n_ctx": 8192, "max_output_tokens": 2048},
    ]))
    import hive_mind_proxy as g
    importlib.reload(g)
    assert "http://user" not in g.LLM_BACKENDS
    assert g.LLM_BACKENDS == []
    assert "user@10" not in _declared(g)
    assert "user@" not in _declared(g)
    with pytest.raises(SystemExit) as exc:
        g.require_no_backend_url_credentials()
    assert exc.value.code == 78
    assert "user@10" not in str(exc.value)
    assert "user@" not in str(exc.value)


def test_json_weight_on_a_path_url_loads(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", "[{\"url\": \"https://api.example.com/v1\", \"n_ctx\": 8192, \"max_output_tokens\": 2048, \"weight\": 2}]")
    import hive_mind_proxy as g
    importlib.reload(g)
    assert "https://api.example.com/v1" in g.LLM_BACKENDS
    assert g.LLM_WEIGHTS["https://api.example.com/v1"] == 2.0
    assert not any(field == "url" for _entry, field in g.LLM_DECLARATION_ERRORS)


def test_csv_host_at_weight_is_not_loaded(monkeypatch):
    """http://myhost@2 is not a JSON declaration. The CSV parser that used to split it is gone."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://myhost@2")
    import hive_mind_proxy as g
    importlib.reload(g)
    assert g.LLM_BACKENDS == []
    assert "http://myhost" not in g.LLM_BACKENDS
    assert "myhost@2" not in " ".join(g.LLM_BACKENDS)


def test_userinfo_refusal_names_the_field_and_not_the_credential(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://user@10", "n_ctx": 8192, "max_output_tokens": 2048},
    ]))
    import hive_mind_proxy as g
    importlib.reload(g)
    with pytest.raises(SystemExit) as exc:
        g.require_no_backend_url_credentials()
    text = str(exc.value)
    assert "field url" in text
    assert "user@10" not in text
    assert "user@" not in text


def test_plain_credentialed_url_refuses_without_the_password(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://u:p@host:8000", "n_ctx": 8192, "max_output_tokens": 2048},
    ]))
    import hive_mind_proxy as g
    importlib.reload(g)
    assert g.LLM_BACKENDS == []
    assert "u:p@" not in _declared(g)
    with pytest.raises(SystemExit) as exc:
        g.require_no_backend_url_credentials()
    assert exc.value.code == 78
    assert "u:p@" not in str(exc.value)
    assert "host:8000" in str(exc.value)
    assert "field url" in str(exc.value)


def test_unparseable_backend_url_refuses_rather_than_silently_admitted(monkeypatch):
    """An unparseable credentialed URL is a declaration fatal. The credential is not admitted and not echoed."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://u:p@[::1", "n_ctx": 8192, "max_output_tokens": 2048},
    ]))
    import hive_mind_proxy as g
    importlib.reload(g)
    assert g.LLM_BACKENDS == []
    assert "u:p" not in _declared(g)
    with pytest.raises(SystemExit) as exc:
        g.require_no_backend_url_credentials()
    assert exc.value.code == 78
    assert "u:p" not in str(exc.value)
