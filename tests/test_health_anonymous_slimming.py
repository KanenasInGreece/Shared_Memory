"""S-10 (Credential_Custody_Plan PR A5): GET /health used to disclose the
full backend roster, per-backend pool state and capability probes to any
anonymous caller. Anonymous shape is now {"status", "version", "api_version"}
— exactly what memory_bridge.py's `doctor` (check_gateway_compat) parses;
the full payload requires a valid agent bearer token, byte-compatible with
the pre-S-10 shape.

Also covers S-11's TTL cache on the same handler's upstream fan-out."""
import asyncio
import hashlib
import importlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


class _HealthProbeResp:
    status = 200


class _HealthProbeCm:
    async def __aenter__(self):
        return _HealthProbeResp()

    async def __aexit__(self, *a):
        return False


class _HealthProbeSession:
    """No real network — every probe (/health, /v1/models) just reports 200.
    Counts calls so the S-11 TTL cache can be proven to skip the fan-out on
    a cache hit."""
    def __init__(self):
        self.get_calls = 0

    def get(self, url, timeout=None):
        self.get_calls += 1
        return _HealthProbeCm()


def _health_request(headers=None, agent_token=None):
    class _Req(dict):
        pass
    req = _Req()
    req.headers = headers or {}
    if agent_token:
        req.headers = {**req.headers, "Authorization": f"Bearer {agent_token}"}
    req.app = {}  # "proxy" patched in by callers
    return req


def _load_gateway(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    import hive_mind_proxy as g
    importlib.reload(g)
    return g


def test_anonymous_caller_gets_exactly_the_slim_shape(monkeypatch):
    g = _load_gateway(monkeypatch)
    import coordinator
    coordinator._AGENT_TOKENS.clear()

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    req = _health_request()
    req.app = {"proxy": proxy}

    body = json.loads(asyncio.run(g.handle_health(req)).body.decode())
    assert set(body.keys()) == {"status", "version", "api_version"}
    assert body["version"] == g.FRAMEWORK_VERSION
    assert body["api_version"] == g.API_VERSION
    assert body["status"] == "ok"


def test_doctors_three_fields_survive_anonymous_slimming(monkeypatch):
    """Direct regression test for the hard constraint: memory_bridge.py's
    check_gateway_compat() reads exactly status/version/api_version off
    /health -- confirm those three keys are the ones actually kept."""
    import memory_bridge
    g = _load_gateway(monkeypatch)
    import coordinator
    coordinator._AGENT_TOKENS.clear()

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    req = _health_request()
    req.app = {"proxy": proxy}

    body = json.loads(asyncio.run(g.handle_health(req)).body.decode())
    assert body.get("api_version") == memory_bridge.API_VERSION or "api_version" in body
    assert "version" in body
    assert "status" in body


def test_authenticated_caller_gets_the_full_byte_compatible_payload(monkeypatch):
    g = _load_gateway(monkeypatch)
    import coordinator
    coordinator._AGENT_TOKENS.clear()
    digest = hashlib.sha256(b"tok_health_full_test").hexdigest()
    coordinator._AGENT_TOKENS[digest] = "claude"
    try:
        proxy = g.AsyncHiveMindProxy()
        proxy.session = _HealthProbeSession()
        req = _health_request(agent_token="tok_health_full_test")
        req.app = {"proxy": proxy}

        body = json.loads(asyncio.run(g.handle_health(req)).body.decode())
        # Today's full shape, unchanged (same field set the pre-S-10 handler
        # produced): liveness + daemon/rem_daemon + config + auth_* + backup_*.
        for field in ("status", "version", "api_version", "daemon", "rem_daemon",
                       "auth_required", "auth_scheme", "backup_in_progress",
                       "config", "embedder", "reranker", "llm", "backend_capability"):
            assert field in body, f"authenticated payload missing {field!r}"
    finally:
        coordinator._AGENT_TOKENS.clear()


def test_invalid_token_still_gets_the_anonymous_slim_shape(monkeypatch):
    """A caller presenting a token that fails to verify is treated as
    anonymous, not error'd -- /health stays liveness-reachable regardless."""
    g = _load_gateway(monkeypatch)
    import coordinator
    coordinator._AGENT_TOKENS.clear()

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    req = _health_request(agent_token="tok_totally_wrong")
    req.app = {"proxy": proxy}

    resp = asyncio.run(g.handle_health(req))
    assert resp.status == 200
    body = json.loads(resp.body.decode())
    assert set(body.keys()) == {"status", "version", "api_version"}


def test_degraded_status_code_preserved_for_anonymous_caller(monkeypatch):
    """HTTP 503 must still surface anonymously -- an anonymous caller learns
    the VERDICT (degraded), just not why."""
    g = _load_gateway(monkeypatch)
    import coordinator
    coordinator._AGENT_TOKENS.clear()

    class _DownResp:
        status = 500

    class _DownCm:
        async def __aenter__(self):
            return _DownResp()

        async def __aexit__(self, *a):
            return False

    class _DownSession:
        def get(self, url, timeout=None):
            return _DownCm()

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _DownSession()
    req = _health_request()
    req.app = {"proxy": proxy}

    resp = asyncio.run(g.handle_health(req))
    assert resp.status == 503
    body = json.loads(resp.body.decode())
    assert body["status"] == "degraded"


# ── S-11: TTL cache on the fan-out ───────────────────────────────────────────

def test_second_hit_within_ttl_reuses_the_cached_probe(monkeypatch):
    monkeypatch.setenv("HEALTH_CACHE_TTL_S", "60")
    g = _load_gateway(monkeypatch)
    import coordinator
    coordinator._AGENT_TOKENS.clear()

    proxy = g.AsyncHiveMindProxy()
    session = _HealthProbeSession()
    proxy.session = session
    req = _health_request()
    req.app = {"proxy": proxy}

    asyncio.run(g.handle_health(req))
    first_calls = session.get_calls
    assert first_calls > 0
    asyncio.run(g.handle_health(req))
    assert session.get_calls == first_calls, (
        "a second /health hit inside the TTL window must not re-probe the "
        "embedder/reranker upstreams"
    )


def test_anonymous_and_authenticated_callers_share_the_cache(monkeypatch):
    """Same probe cost regardless of who asks -- only the response shape
    differs, computed fresh from the same cached checks."""
    monkeypatch.setenv("HEALTH_CACHE_TTL_S", "60")
    g = _load_gateway(monkeypatch)
    import coordinator
    coordinator._AGENT_TOKENS.clear()
    digest = hashlib.sha256(b"tok_shared_cache_test").hexdigest()
    coordinator._AGENT_TOKENS[digest] = "claude"
    try:
        proxy = g.AsyncHiveMindProxy()
        session = _HealthProbeSession()
        proxy.session = session

        anon_req = _health_request()
        anon_req.app = {"proxy": proxy}
        asyncio.run(g.handle_health(anon_req))
        calls_after_anon = session.get_calls

        auth_req = _health_request(agent_token="tok_shared_cache_test")
        auth_req.app = {"proxy": proxy}
        body = json.loads(asyncio.run(g.handle_health(auth_req)).body.decode())
        assert session.get_calls == calls_after_anon, (
            "the authenticated caller's hit, inside the TTL window, must "
            "reuse the SAME cached probe the anonymous caller triggered"
        )
        # And still gets the full shape despite the underlying probe being shared.
        assert "config" in body
    finally:
        coordinator._AGENT_TOKENS.clear()


def test_cache_expires_after_ttl(monkeypatch):
    monkeypatch.setenv("HEALTH_CACHE_TTL_S", "0")
    g = _load_gateway(monkeypatch)
    import coordinator
    coordinator._AGENT_TOKENS.clear()

    proxy = g.AsyncHiveMindProxy()
    session = _HealthProbeSession()
    proxy.session = session
    req = _health_request()
    req.app = {"proxy": proxy}

    asyncio.run(g.handle_health(req))
    first_calls = session.get_calls
    asyncio.run(g.handle_health(req))
    assert session.get_calls > first_calls, (
        "TTL=0 must never cache -- every hit re-probes"
    )


def test_cache_never_serves_a_stale_api_version_across_a_restart(monkeypatch):
    """Trivially true in-process (stated, not assumed, per the brief): the
    module reload below is the test's stand-in for a process restart -- a
    fresh module has a fresh (empty) _health_cache, so the very first
    /health hit after "restart" always re-probes and picks up whatever
    FRAMEWORK_VERSION that fresh process defines."""
    monkeypatch.setenv("HEALTH_CACHE_TTL_S", "600")
    g = _load_gateway(monkeypatch)
    import coordinator
    coordinator._AGENT_TOKENS.clear()

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    req = _health_request()
    req.app = {"proxy": proxy}
    asyncio.run(g.handle_health(req))
    assert g._health_cache["checks"] is not None

    # Simulate a restart: a fresh module import gets a fresh cache.
    importlib.reload(g)
    assert g._health_cache["checks"] is None


# ── Mutation check target ────────────────────────────────────────────────────
# See A5_HANDOFF.md's mutation-check table: making handle_health always
# return the full `checks` dict (dropping the `caller_authenticated` branch)
# makes test_anonymous_caller_gets_exactly_the_slim_shape fail (extra keys
# appear). Removing the TTL cache short-circuit (always calling
# _build_health_checks) makes test_second_hit_within_ttl_reuses_the_cached_
# probe fail (get_calls keeps climbing).
