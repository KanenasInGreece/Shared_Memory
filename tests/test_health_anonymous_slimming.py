"""S-10 (Credential_Custody_Plan PR A5): GET /health used to disclose the
full backend roster, per-backend pool state and capability probes to any
anonymous caller. Anonymous shape is now {"status", "version", "api_version"}
— exactly what memory_bridge.py's `doctor` (check_gateway_compat) parses;
the full payload requires a valid agent bearer token, byte-compatible with
the pre-S-10 shape.

SEC-A5-03 (PR A5 fix round): slimming applies ONLY when
AUTH_CONFIGURED_AT_STARTUP is true — an auth-off install keeps the full
payload for EVERY caller (there is no token that could ever restore it
otherwise, since resolve_identity() can never match anything against an
empty registry). Every test below reloads `coordinator` with a real
AGENT_TOKENS env var (or explicitly unset) BEFORE reloading `hive_mind_
proxy`, mirroring tests/test_llm_fault_origin.py's proven pattern, and
asserts `g.AUTH_CONFIGURED_AT_STARTUP` explicitly so the condition under
test is never implicit.

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

    def get(self, url, timeout=None, headers=None, **_kw):
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


def _load_gateway(monkeypatch, agent_tokens: str = ""):
    """Reloads coordinator FIRST (test_llm_fault_origin.py's proven order)
    so AUTH_CONFIGURED_AT_STARTUP reflects `agent_tokens` exactly — the
    bare `_AGENT_TOKENS.clear()` this file used before the fix round left
    AUTH_CONFIGURED_AT_STARTUP at whatever a PRIOR test in the session
    happened to leave it, which is exactly the untested gap SEC-A5-03's
    review found (zero references to AUTH_CONFIGURED_AT_STARTUP here)."""
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([{"url": "http://a:5000", "private_ok": True}]))
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)  # see test_auth.load_coordinator's docstring
    if agent_tokens:
        monkeypatch.setenv("AGENT_TOKENS", agent_tokens)
    else:
        monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    return g


# ── Auth-configured install: slimming applies ────────────────────────────────

def test_anonymous_caller_gets_exactly_the_slim_shape(monkeypatch):
    g = _load_gateway(monkeypatch, agent_tokens="claude:tok_health_slim_test")
    assert g.AUTH_CONFIGURED_AT_STARTUP is True

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    # RE-RULED at v0.9.74: `status` is now DERIVED from `dependencies`, and the
    # two daemons are dependencies. In this process no daemon is spawned, so
    # both PID flags are False and the honest verdict would be `down` — which is
    # correct behaviour and nothing to do with what this test is about (the slim
    # SHAPE an anonymous caller receives). Declare them healthy so the assertion
    # below still pins `ok` rather than being weakened to "some enum value".
    g._daemon_healthy = True
    g._rem_healthy = True
    req = _health_request()  # no Authorization header at all
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
    g = _load_gateway(monkeypatch, agent_tokens="claude:tok_health_slim_test")
    assert g.AUTH_CONFIGURED_AT_STARTUP is True

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    req = _health_request()
    req.app = {"proxy": proxy}

    body = json.loads(asyncio.run(g.handle_health(req)).body.decode())
    assert body.get("api_version") == memory_bridge.API_VERSION or "api_version" in body
    assert "version" in body
    assert "status" in body


def test_authenticated_caller_gets_the_full_byte_compatible_payload(monkeypatch):
    g = _load_gateway(monkeypatch, agent_tokens="claude:tok_health_full_test")
    assert g.AUTH_CONFIGURED_AT_STARTUP is True

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


def test_invalid_token_still_gets_the_anonymous_slim_shape(monkeypatch):
    """A caller presenting a token that fails to verify is treated as
    anonymous, not error'd -- /health stays liveness-reachable regardless."""
    g = _load_gateway(monkeypatch, agent_tokens="claude:tok_health_slim_test")
    assert g.AUTH_CONFIGURED_AT_STARTUP is True

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
    g = _load_gateway(monkeypatch, agent_tokens="claude:tok_health_slim_test")
    assert g.AUTH_CONFIGURED_AT_STARTUP is True

    class _DownResp:
        status = 500

    class _DownCm:
        async def __aenter__(self):
            return _DownResp()

        async def __aexit__(self, *a):
            return False

    class _DownSession:
        def get(self, url, timeout=None, headers=None, **_kw):
            return _DownCm()

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _DownSession()
    req = _health_request()
    req.app = {"proxy": proxy}

    resp = asyncio.run(g.handle_health(req))
    # ⛔ THE 503 IS THE POINT AND IT IS UNCHANGED. v0.9.74 widened what the ENUM
    # can say (a third value, `down`) without widening what the CODE means: 503
    # still fires if and only if an encoder is down, which is the save mandate.
    assert resp.status == 503
    body = json.loads(resp.body.decode())
    # RE-RULED at v0.9.74: an encoder that does not answer is `down`, not
    # `degraded`. `degraded` now means "usable but not right" — a slow encoder,
    # a failing outbox row, a raised warning — and reporting a dead critical
    # backend with the same word as a slow one was the imprecision that made the
    # enum unable to carry the new dependency states at all.
    assert body["status"] == "down"


# ── SEC-A5-03: auth-OFF install keeps the full payload for EVERYONE ─────────

def test_auth_off_install_gets_full_payload_with_no_token_presented(monkeypatch):
    """MUTATION TARGET (SEC-A5-03): the defect this review finding
    describes exactly -- an auth-unset install has no token registry at
    all, so gating on bare resolve_identity() locked such an install out
    of its own /health permanently. AUTH_CONFIGURED_AT_STARTUP False must
    mean the full payload, unconditionally."""
    g = _load_gateway(monkeypatch, agent_tokens="")
    assert g.AUTH_CONFIGURED_AT_STARTUP is False

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    req = _health_request()  # no Authorization header, no token registry either
    req.app = {"proxy": proxy}

    body = json.loads(asyncio.run(g.handle_health(req)).body.decode())
    assert "daemon" in body and "config" in body and "backup_in_progress" in body
    assert set(body.keys()) != {"status", "version", "api_version"}


def test_auth_off_install_full_payload_even_with_a_presented_token(monkeypatch):
    """A caller that HAPPENS to present a bearer token on an auth-off
    install still gets the full payload -- the token cannot match anything
    (empty registry) but that must not matter here."""
    g = _load_gateway(monkeypatch, agent_tokens="")
    assert g.AUTH_CONFIGURED_AT_STARTUP is False

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    req = _health_request(agent_token="tok_whatever")
    req.app = {"proxy": proxy}

    body = json.loads(asyncio.run(g.handle_health(req)).body.decode())
    assert "daemon" in body and "config" in body


# ── S-11: TTL cache on the fan-out ───────────────────────────────────────────

def test_second_hit_within_ttl_reuses_the_cached_probe(monkeypatch):
    monkeypatch.setenv("HEALTH_CACHE_TTL_S", "60")
    g = _load_gateway(monkeypatch, agent_tokens="claude:tok_health_slim_test")
    assert g.AUTH_CONFIGURED_AT_STARTUP is True

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
    g = _load_gateway(monkeypatch, agent_tokens="claude:tok_shared_cache_test")
    assert g.AUTH_CONFIGURED_AT_STARTUP is True

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


def test_cache_expires_after_ttl(monkeypatch):
    monkeypatch.setenv("HEALTH_CACHE_TTL_S", "0")
    g = _load_gateway(monkeypatch, agent_tokens="claude:tok_health_slim_test")

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


class _SlowHealthProbeCm:
    """Blocks in __aenter__ until an asyncio.Event is set -- lets a test
    hold N concurrent probes open simultaneously to prove they coalesce."""
    def __init__(self, gate):
        self._gate = gate

    async def __aenter__(self):
        await self._gate.wait()
        return _HealthProbeResp()

    async def __aexit__(self, *a):
        return False


class _SlowHealthProbeSession:
    """Every .get() counts itself, then blocks until released -- so several
    concurrent _health_probe_cached() callers can be proven to have either
    triggered their OWN fan-out (no coalescing) or shared exactly one
    (SEC-A5-05b)."""
    def __init__(self):
        self.get_calls = 0
        self.gate = asyncio.Event()

    def get(self, url, timeout=None, headers=None, **_kw):
        self.get_calls += 1
        return _SlowHealthProbeCm(self.gate)


def test_concurrent_misses_are_coalesced_into_one_probe(monkeypatch):
    """MUTATION TARGET (SEC-A5-05b): three concurrent cache-miss callers
    must trigger exactly ONE full upstream fan-out, not three. The
    reference count is measured from a real single fan-out (never
    hardcoded) so this stays correct if the probe's own call count ever
    changes for an unrelated reason."""
    monkeypatch.setenv("HEALTH_CACHE_TTL_S", "60")
    g = _load_gateway(monkeypatch, agent_tokens="claude:tok_health_slim_test")

    # Reference: how many session.get() calls does ONE full fan-out make?
    async def _reference():
        proxy = g.AsyncHiveMindProxy()
        proxy.session = _HealthProbeSession()
        await g._build_health_checks(proxy, None)
        return proxy.session.get_calls
    expected_single_fanout_calls = asyncio.run(_reference())
    assert expected_single_fanout_calls > 0

    async def _run():
        proxy = g.AsyncHiveMindProxy()
        session = _SlowHealthProbeSession()
        proxy.session = session

        tasks = [asyncio.create_task(g._health_probe_cached(proxy, None)) for _ in range(3)]
        # Let all three coroutines run up to their first await (the gate)
        # before releasing it -- this is what forces genuine concurrency
        # rather than three sequential completions.
        for _ in range(5):
            await asyncio.sleep(0)
        session.gate.set()
        results = await asyncio.gather(*tasks)
        return session, results

    session, results = asyncio.run(_run())
    assert session.get_calls == expected_single_fanout_calls, (
        f"expected exactly one fan-out's worth of probe calls "
        f"({expected_single_fanout_calls}), got {session.get_calls} -- "
        f"concurrent misses were not coalesced behind the lock"
    )
    assert all(r is results[0] for r in results), (
        "all coalesced callers must receive the identical cached dict"
    )


def test_cache_never_serves_a_stale_api_version_across_a_restart(monkeypatch):
    """Trivially true in-process (stated, not assumed, per the brief): the
    module reload below is the test's stand-in for a process restart -- a
    fresh module has a fresh (empty) _health_cache, so the very first
    /health hit after "restart" always re-probes and picks up whatever
    FRAMEWORK_VERSION that fresh process defines."""
    monkeypatch.setenv("HEALTH_CACHE_TTL_S", "600")
    g = _load_gateway(monkeypatch, agent_tokens="claude:tok_health_slim_test")

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
# return the full `checks` dict (dropping the caller_authenticated branch)
# makes test_anonymous_caller_gets_exactly_the_slim_shape fail (extra keys
# appear). Removing the TTL cache short-circuit (always calling
# _build_health_checks) makes test_second_hit_within_ttl_reuses_the_cached_
# probe fail (get_calls keeps climbing). Dropping the `AUTH_CONFIGURED_AT_
# STARTUP and` clause (SEC-A5-03) makes test_auth_off_install_gets_full_
# payload_with_no_token_presented fail (slim shape appears even with auth
# off). Removing the `async with _health_probe_lock:` coalescing (SEC-A5-
# 05b) makes test_concurrent_misses_are_coalesced_into_one_probe fail
# (get_calls triples instead of matching one fan-out).


# ── A-4: `agent` and `role` on the AUTHENTICATED payload only ────────────────
#
# A client holding a token could not learn either without attempting a write and
# reading the refusal — and a read-only token's 403 looks like a permissions bug
# to anyone who did not already know the token was confined. These keys let
# `doctor` say it plainly. Every test below also pins what must NOT change: the
# anonymous slim shape and the auth-off full shape are the same as they were.


def test_an_authenticated_write_token_is_told_who_it_is_and_what_it_may_do(monkeypatch):
    monkeypatch.delenv("AGENT_ROLES", raising=False)
    monkeypatch.delenv("SHARED_MEMORY_READ_ONLY_AGENTS", raising=False)
    g = _load_gateway(monkeypatch, agent_tokens="claude:tok_a4_write")
    assert g.AUTH_CONFIGURED_AT_STARTUP is True

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    req = _health_request(agent_token="tok_a4_write")
    req.app = {"proxy": proxy}

    body = json.loads(asyncio.run(g.handle_health(req)).body.decode())
    assert body["agent"] == "claude"
    assert body["role"] == "write"


def test_a_declared_read_only_token_is_told_it_is_read(monkeypatch):
    """The case the key exists for. Without it, the only way to discover a
    confined token is to attempt a write and read a 403 that reads like a bug."""
    monkeypatch.setenv("AGENT_ROLES", "dashboard:read")
    monkeypatch.delenv("SHARED_MEMORY_READ_ONLY_AGENTS", raising=False)
    g = _load_gateway(monkeypatch, agent_tokens="dashboard:tok_a4_read")
    assert g.AUTH_CONFIGURED_AT_STARTUP is True

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    req = _health_request(agent_token="tok_a4_read")
    req.app = {"proxy": proxy}

    body = json.loads(asyncio.run(g.handle_health(req)).body.decode())
    assert body["agent"] == "dashboard"
    assert body["role"] == "read"


def test_a_ROSTER_confined_identity_reads_read_even_with_no_declaration(monkeypatch):
    """⛔ THE REASON THIS GOES THROUGH `effective_role` AND NOT A BARE
    `_AGENT_ROLES` LOOKUP. `read_only_agents()` confines an identity REGARDLESS
    of what AGENT_ROLES says — `monitor` is on the built-in roster — so a raw
    map read reports `write` for a token the gateway 403s on every write route,
    which is the exact false reassurance this key exists to remove.

    MUTATION CHECK: replace `effective_role(...) == "read"` in
    `_health_role_for` with `_AGENT_ROLES.get(agent_name) == "read"` and this
    test dies while the declared-role test above still passes."""
    monkeypatch.delenv("AGENT_ROLES", raising=False)   # nothing declared at all
    monkeypatch.delenv("SHARED_MEMORY_READ_ONLY_AGENTS", raising=False)
    g = _load_gateway(monkeypatch, agent_tokens="monitor:tok_a4_roster")
    assert g.AUTH_CONFIGURED_AT_STARTUP is True

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    req = _health_request(agent_token="tok_a4_roster")
    req.app = {"proxy": proxy}

    body = json.loads(asyncio.run(g.handle_health(req)).body.decode())
    assert body["agent"] == "monitor"
    assert body["role"] == "read"


def test_an_anonymous_caller_is_told_NEITHER(monkeypatch):
    """Adding an identity to a response served to someone who proved no identity
    would be absurd. Named explicitly rather than left to the exact-set assertion
    above, so a future key added to the slim payload cannot quietly bring these
    two with it."""
    g = _load_gateway(monkeypatch, agent_tokens="claude:tok_a4_anon")
    assert g.AUTH_CONFIGURED_AT_STARTUP is True

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    req = _health_request()
    req.app = {"proxy": proxy}

    body = json.loads(asyncio.run(g.handle_health(req)).body.decode())
    assert "agent" not in body
    assert "role" not in body
    assert set(body.keys()) == {"status", "version", "api_version"}


def test_an_invalid_token_is_told_NEITHER(monkeypatch):
    """A presented-but-rejected token is anonymous here. It must not learn a
    name — least of all its own guess being confirmed or denied."""
    g = _load_gateway(monkeypatch, agent_tokens="claude:tok_a4_valid")
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    req = _health_request(agent_token="tok_a4_not_the_one")
    req.app = {"proxy": proxy}

    body = json.loads(asyncio.run(g.handle_health(req)).body.decode())
    assert "agent" not in body and "role" not in body


def test_an_auth_off_install_is_told_NEITHER_and_keeps_its_full_payload(monkeypatch):
    """⛔ ABSENT, NOT NULL, AND NOT A DEFAULT. There is no token registry on an
    auth-off install, so every caller is the same unnamed everyone; emitting
    `agent: null` / `role: "write"` would dress an absence up as an answer.
    Absent means "this install has no identities", which is true and useful —
    and the full payload it has always served is untouched."""
    monkeypatch.delenv("AGENT_ROLES", raising=False)
    g = _load_gateway(monkeypatch, agent_tokens="")
    assert g.AUTH_CONFIGURED_AT_STARTUP is False

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    req = _health_request()
    req.app = {"proxy": proxy}

    body = json.loads(asyncio.run(g.handle_health(req)).body.decode())
    assert "agent" not in body
    assert "role" not in body
    # Still the full payload for everyone, exactly as before.
    for field in ("status", "version", "api_version", "daemon", "config",
                  "embedder", "reranker", "llm"):
        assert field in body, f"auth-off payload lost {field!r}"


def test_the_identity_is_never_written_into_the_SHARED_health_cache(monkeypatch):
    """The per-caller projection must not touch the cache every caller shares.

    ⚠ THIS ASSERTS THE CACHE ITSELF, and the first version of this test did not
    — it asserted a follow-up ANONYMOUS response and passed happily against an
    in-place mutation, because the anonymous branch rebuilds its own three-key
    dict and is immune. A guard that cannot fail is not a guard.

    ⛔ AND THE HONEST REASON FOR THE COPY IS A CONTRACT, NOT A REPRODUCED LEAK.
    `_health_probe_cached` has exactly one consumer today and both authenticated
    callers overwrite the keys with their own values, so an in-place write is not
    currently observable from outside — I tried to make it observable and could
    not. What makes the copy right is that the cache is SHARED STATE whose own
    docstring says the per-caller projection "is applied fresh on every call,
    never cached itself", and a second consumer or a lazily-serialised response
    would turn an unobservable write into a cross-identity disclosure. This test
    exists so that stays true by construction rather than by that argument
    having to be re-derived.

    MUTATION CHECK: change the `{**checks, ...}` copy in `handle_health` to
    `checks["agent"] = ...` and this dies.
    """
    g = _load_gateway(monkeypatch, agent_tokens="claude:tok_a4_cache")
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()

    authed = _health_request(agent_token="tok_a4_cache")
    authed.app = {"proxy": proxy}
    body = json.loads(asyncio.run(g.handle_health(authed)).body.decode())
    assert body["agent"] == "claude"

    cached = g._health_cache["checks"]
    assert cached is not None, "the probe never populated the cache"
    assert "agent" not in cached, "the caller's identity was written into the shared cache"
    assert "role" not in cached
