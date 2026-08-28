"""Model-attributes routing (Model_Attributes_Routing_Plan_2026-08-18, REVISED
DESIGN) — Unit 1 (gateway). Descriptor schema (roles/n_ctx/private_ok/
max_inflight), startup refusals (unknown role, M-5, P-5), the eligibility
hard pre-filter (I-1a enumerated "never" paths), the 422 no_eligible_backend
refusal (I-2a), the fit check (I-3), backward compat for descriptor-less
fleets (I-5a), P-6 steering-header hygiene, the max_inflight concurrency cap
(I-8/I-8b), and the H-1/H-3 health-display fixes.

Uses the request/session patterns proven in tests/test_llm_backend_secrets.py,
tests/test_llm_affinity.py and tests/test_llm_steering_headers.py."""
import asyncio
import importlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def _fresh(monkeypatch):
    """Auth-off reload (mirrors test_llm_affinity.py's _fresh) — most tests
    here care about eligibility/selection, not identity."""
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    assert g.AUTH_CONFIGURED_AT_STARTUP is False
    return g


class _RaisingSession:
    """Any .request() call is a test failure — used to prove a 422/503
    refusal never reaches the upstream at all (I-2a: never falls back)."""
    closed = False

    def request(self, *a, **kw):
        raise AssertionError("upstream must never be called on a refusal path")


class _CaptureSession:
    closed = False

    def __init__(self):
        self.captured_headers = None
        self.captured_data = None
        self.calls = 0

    def request(self, *a, **kw):
        self.calls += 1
        self.captured_headers = kw.get("headers")
        self.captured_data = kw.get("data")
        raise RuntimeError("capture-only session — no real upstream call")


def _req(headers: dict, body: bytes):
    class _Req(dict):
        pass
    r = _Req()
    r.method = "POST"
    r.path = "/v1/chat/completions"
    r.rel_url = "/v1/chat/completions"
    r.headers = headers
    r.can_read_body = True

    async def read():
        return body
    r.read = read
    return r


def _body(**fields) -> bytes:
    payload = {"messages": [{"role": "user", "content": "hi"}], "model": "local-model"}
    payload.update(fields)
    return json.dumps(payload).encode()


# ── Descriptor parsing ───────────────────────────────────────────────────────

def test_roles_ncontext_private_ok_max_inflight_parsed(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["extract", "judge"], "n_ctx": 8192,
         "max_inflight": 2, "price_per_mtok_in": 0.14, "price_per_mtok_out": 0.28},
        {"url": "http://b:4000"},
    ]))
    g = _fresh(monkeypatch)
    assert g.LLM_BACKEND_ROLES["http://a:5000"] == frozenset({"extract", "judge"})
    assert g.LLM_BACKEND_NCTX["http://a:5000"] == 8192
    assert g.LLM_BACKEND_MAX_INFLIGHT["http://a:5000"] == 2
    assert g.LLM_BACKEND_PRICE_IN["http://a:5000"] == 0.14
    assert g.LLM_BACKEND_PRICE_OUT["http://a:5000"] == 0.28
    # absent fields: serves-all degenerate case
    assert g.LLM_BACKEND_ROLES["http://b:4000"] is None
    assert g.LLM_BACKEND_NCTX["http://b:4000"] is None
    assert g.LLM_BACKEND_MAX_INFLIGHT["http://b:4000"] is None


def test_private_ok_default_from_token_env_presence(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://local:5000"},
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY", "roles": ["extract"]},
    ]))
    g = _fresh(monkeypatch)
    assert g.LLM_BACKEND_PRIVATE_OK["http://local:5000"] is True   # no token -> default True
    assert g.LLM_BACKEND_PRIVATE_OK["https://api.deepseek.com/v1"] is False  # has token -> default False
    assert g.LLM_BACKEND_PRIVATE_OK_EXPLICIT["http://local:5000"] is False
    assert g.LLM_BACKEND_PRIVATE_OK_EXPLICIT["https://api.deepseek.com/v1"] is False


def test_explicit_private_ok_always_wins(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY", "private_ok": True},
        {"url": "http://local:5000", "private_ok": False, "roles": ["extract"]},
    ]))
    g = _fresh(monkeypatch)
    assert g.LLM_BACKEND_PRIVATE_OK["https://api.deepseek.com/v1"] is True
    assert g.LLM_BACKEND_PRIVATE_OK_EXPLICIT["https://api.deepseek.com/v1"] is True
    assert g.LLM_BACKEND_PRIVATE_OK["http://local:5000"] is False
    assert g.LLM_BACKEND_PRIVATE_OK_EXPLICIT["http://local:5000"] is True


def test_legacy_comma_form_is_serves_all_and_private_ok_true(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000,http://b:4000")
    g = _fresh(monkeypatch)
    for b in g.LLM_BACKENDS:
        assert g.LLM_BACKEND_ROLES[b] is None
        assert g.LLM_BACKEND_PRIVATE_OK[b] is True
        assert g.LLM_BACKEND_PRIVATE_OK_EXPLICIT[b] is False
        assert g.LLM_BACKEND_MAX_INFLIGHT[b] is None
    assert g._LLM_BACKEND_ROLE_CONFIG_ERRORS == []


def test_unknown_role_recorded_as_config_error_not_raised_at_parse_time(monkeypatch):
    """Module import must never raise (every test in this repo imports this
    module freely) — the problem is collected, not thrown, until main() asks."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["extract", "bogus_role"]},
    ]))
    g = _fresh(monkeypatch)
    assert any("bogus_role" in e for e in g._LLM_BACKEND_ROLE_CONFIG_ERRORS)
    # the invalid entries are dropped, valid ones kept
    assert g.LLM_BACKEND_ROLES["http://a:5000"] == frozenset({"extract"})


def test_summarize_role_flagged_reserved_not_generic_unknown(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["summarize"]},
    ]))
    g = _fresh(monkeypatch)
    assert any("RESERVED" in e for e in g._LLM_BACKEND_ROLE_CONFIG_ERRORS)


def test_price_metadata_never_read_by_selection(monkeypatch):
    """M-4-adjacent: price fields are stored + surfaced, never consulted by
    routing math — construct two otherwise-identical eligible backends where
    the "cheaper" one has MORE inflight, and confirm least-in-flight (not
    price) still decides."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://cheap:5000", "price_per_mtok_in": 0.01},
        {"url": "http://pricey:4000", "price_per_mtok_in": 100.0},
    ]))
    g = _fresh(monkeypatch)
    g._llm_inflight["http://cheap:5000"] = 5
    g._llm_inflight["http://pricey:4000"] = 0
    chosen = g._select_llm_backend("", None)
    assert chosen == "http://pricey:4000"   # least-in-flight, not cheapest


# ── Startup refusals ─────────────────────────────────────────────────────────

def test_unknown_role_refuses_startup(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["not_a_real_role"]},
    ]))
    g = _fresh(monkeypatch)
    try:
        g.require_valid_llm_routing_config()
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "not_a_real_role" in str(e)


def test_m5_credentialed_backend_with_no_choice_refuses_startup(monkeypatch):
    """MUTATION TARGET (M-5 Critical): a credentialed backend with neither
    roles nor an explicit private_ok would silently go dark under the plain
    default — must refuse rather than brick a cloud-only install on upgrade.

    Auth is configured ON here so P-5 cannot ALSO independently refuse this
    exact scenario (private_ok defaults False for a credentialed backend,
    which P-5 would catch on an auth-off install regardless of M-5) —
    isolating M-5 as the SOLE possible cause of the refusal is what makes
    this test actually prove M-5 works, not just "some refusal happened"."""
    monkeypatch.setenv("AGENT_TOKENS", "claude:tok_m5_isolated_test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY"},
    ]))
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    assert g.AUTH_CONFIGURED_AT_STARTUP is True
    try:
        g.require_valid_llm_routing_config()
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "api.deepseek.com" in str(e)


def test_m5_satisfied_by_explicit_private_ok_true(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY", "private_ok": True},
    ]))
    g = _fresh(monkeypatch)
    g.require_valid_llm_routing_config()   # must not raise


def test_m5_satisfied_by_roles(monkeypatch):
    """M-5 is satisfied by `roles` alone -- but a credentialed backend's
    private_ok still DEFAULTS to False (no explicit override here), so P-5
    also needs auth configured for this to start cleanly; auth-on isolates
    the M-5 check from the P-5 check this scenario would otherwise also
    trip."""
    monkeypatch.setenv("AGENT_TOKENS", "claude:tok_m5_roles_test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY", "roles": ["extract"]},
    ]))
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    assert g.AUTH_CONFIGURED_AT_STARTUP is True
    g.require_valid_llm_routing_config()   # must not raise


def test_p5_auth_off_plus_private_ok_false_refuses_startup(monkeypatch):
    """MUTATION TARGET (P-5): without identities the privacy/steering
    invariants cannot hold — auth-off install with a private_ok=false
    backend must refuse."""
    monkeypatch.delenv("ALLOW_UNAUTHENTICATED_PROVIDER_KEYS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "private_ok": False, "roles": ["extract"]},
    ]))
    g = _fresh(monkeypatch)
    assert g.AUTH_CONFIGURED_AT_STARTUP is False
    try:
        g.require_valid_llm_routing_config()
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "private_ok=false" in str(e) or "AGENT_TOKENS" in str(e)


def test_p5_override_env_warns_instead_of_refusing(monkeypatch):
    monkeypatch.setenv("ALLOW_UNAUTHENTICATED_PROVIDER_KEYS", "1")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "private_ok": False, "roles": ["extract"]},
    ]))
    g = _fresh(monkeypatch)
    g.require_valid_llm_routing_config()   # must not raise


def test_auth_on_plus_private_ok_false_is_fine(monkeypatch):
    monkeypatch.setenv("AGENT_TOKENS", "claude:tok_routing_test")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "private_ok": False, "roles": ["extract"]},
    ]))
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    assert g.AUTH_CONFIGURED_AT_STARTUP is True
    g.require_valid_llm_routing_config()   # must not raise


# ── Eligibility (I-1) ─────────────────────────────────────────────────────────

def test_explicit_roles_is_the_privacy_opt_in_even_when_private_ok_false(monkeypatch):
    """I-1: a private_ok=false backend that EXPLICITLY lists a role IS
    eligible for that role's traffic — the explicit assignment IS the
    opt-in."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "private_ok": False, "roles": ["extract"]},
    ]))
    g = _fresh(monkeypatch)
    assert g._role_eligible("http://a:5000", "extract") is True
    assert g._role_eligible("http://a:5000", "judge") is False
    assert g._role_eligible("http://a:5000", "") is False   # role-less: roles list ignored, private_ok gates


def test_role_scoped_private_ok_backend_still_serves_role_less_traffic(monkeypatch):
    """R-3: role-less traffic ignores a backend's roles list ENTIRELY and is
    gated purely on private_ok — a local card pinned to "extract" must not
    refuse an ad-hoc authenticated chat, so a private_ok=True, roles=
    ["extract"] backend IS eligible for role-less traffic."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["extract"]},
    ]))
    g = _fresh(monkeypatch)
    assert g._role_eligible("http://a:5000", "") is True


def test_role_scoped_private_ok_false_backend_never_serves_role_less_traffic(monkeypatch):
    """The other half of R-3: private_ok=false is what actually excludes a
    backend from role-less traffic, regardless of its roles list."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "private_ok": False, "roles": ["extract"]},
    ]))
    g = _fresh(monkeypatch)
    assert g._role_eligible("http://a:5000", "") is False


def test_serves_all_degenerate_case_eligible_for_role_less_and_every_role(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([{"url": "http://a:5000"}]))
    g = _fresh(monkeypatch)
    assert g._role_eligible("http://a:5000", "") is True
    assert g._role_eligible("http://a:5000", "extract") is True
    assert g._role_eligible("http://a:5000", "judge") is True


def test_i5a_descriptor_less_fleet_eligible_set_is_the_full_pool(monkeypatch):
    """I-5a: nothing new restricts a descriptor-less (legacy comma-form)
    fleet — the eligible set for ANY role/role-less traffic is the whole
    pool, exactly like pre-routing-cycle behavior."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000,http://b:4000")
    g = _fresh(monkeypatch)
    assert set(g._eligible_backends("")) == set(g.LLM_BACKENDS)
    assert set(g._eligible_backends("extract")) == set(g.LLM_BACKENDS)
    assert set(g._eligible_backends("judge")) == set(g.LLM_BACKENDS)


# ── I-1a: enumerated "never" paths, each its own mutation-checked test ──────

def test_i1a_affinity_hit_outside_eligible_set_is_a_miss(monkeypatch):
    """MUTATION TARGET (P-2): an affinity-cached backend that falls OUTSIDE
    the current eligible set must be treated as a MISS, not returned
    anyway."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["extract"]},
        {"url": "http://b:4000", "roles": ["judge"]},
    ]))
    g = _fresh(monkeypatch)
    key = "affine-key-1"
    first = g._select_llm_backend("extract", key)
    assert first == "http://a:5000"
    g._llm_inflight[first] = 0
    # Same affinity key, but now the traffic needs "judge" -- "a" (the
    # cached affinity target) is NOT eligible for judge at all.
    second = g._select_llm_backend("judge", key)
    assert second == "http://b:4000"
    assert second != first


def test_i1a_cold_fallback_never_widens_past_eligible(monkeypatch):
    """MUTATION TARGET (P-1 Critical): every fallback tier — including the
    final cooldown-ignoring last resort — must bottom out at the ELIGIBLE
    set, never the full LLM_POOL. Construct the sole eligible backend as
    unhealthy AND reserved (everything "bad" at once); selection must still
    return it rather than leaking to an ineligible backend that happens to
    be healthy."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["extract"]},
        {"url": "http://b:4000", "roles": ["judge"]},
    ]))
    g = _fresh(monkeypatch)
    import time
    g._llm_unhealthy_until["http://a:5000"] = time.monotonic() + 300
    g._llm_reserved.add("http://a:5000")
    try:
        chosen = g._select_llm_backend("extract", None)
        assert chosen == "http://a:5000"   # sole eligible backend, still returned
    finally:
        g._llm_reserved.discard("http://a:5000")


def test_i1a_cooldown_ignoring_last_resort_stays_within_eligible(monkeypatch):
    """A single-backend eligible pool in cooldown must still serve (the
    long-standing "one card always serves" behavior) rather than 422 or
    return None purely from cooldown."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([{"url": "http://a:5000"}]))
    g = _fresh(monkeypatch)
    import time
    g._llm_unhealthy_until["http://a:5000"] = time.monotonic() + 300
    assert g._select_llm_backend("", None) == "http://a:5000"


def test_i1a_reserved_backend_deprioritised_but_last_resort_within_eligible(monkeypatch):
    """_llm_reserved interaction: a reserved backend is skipped in favor of
    an available eligible peer, but a SOLE eligible+reserved backend is
    still the last-resort pick (never an ineligible peer)."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000"}, {"url": "http://b:4000"},
    ]))
    g = _fresh(monkeypatch)
    g._llm_reserved.add("http://a:5000")
    try:
        assert g._select_llm_backend("", None) == "http://b:4000"
    finally:
        g._llm_reserved.discard("http://a:5000")


# ── I-2a: the 422 refusal ────────────────────────────────────────────────────

def test_422_role_constraint_when_nothing_serves_the_function(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["extract"]},
    ]))
    g = _fresh(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _RaisingSession()
    req = _req({"X-SM-LLM-Role": "judge"}, _body())
    resp = asyncio.run(proxy.handle_proxy(req))
    assert resp.status == 422
    body = json.loads(resp.body.decode())
    assert body == {"error": "no_eligible_backend", "constraint": "role", "role": "judge"}
    assert resp.headers["X-SM-Fault-Origin"] == "gateway"


def test_422_privacy_constraint_when_role_less_and_all_backends_private_false(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "private_ok": False, "roles": ["extract"]},
    ]))
    g = _fresh(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _RaisingSession()
    req = _req({}, _body())   # role-less traffic
    resp = asyncio.run(proxy.handle_proxy(req))
    assert resp.status == 422
    body = json.loads(resp.body.decode())
    assert body["constraint"] == "privacy"
    assert body["role"] is None


def test_422_fit_constraint_when_oversized(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "n_ctx": 100},
    ]))
    g = _fresh(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _RaisingSession()
    huge_body = _body(**{"messages": [{"role": "user", "content": "x" * 5000}]})
    req = _req({}, huge_body)
    resp = asyncio.run(proxy.handle_proxy(req))
    assert resp.status == 422
    body = json.loads(resp.body.decode())
    assert body["constraint"] == "fit"


def test_422_increments_counter_and_last_ts(monkeypatch):
    g = _fresh(monkeypatch)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["extract"]},
    ]))
    g = _fresh(monkeypatch)
    before = g._routing_no_eligible_backend_count
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _RaisingSession()
    asyncio.run(proxy.handle_proxy(_req({"X-SM-LLM-Role": "judge"}, _body())))
    assert g._routing_no_eligible_backend_count == before + 1
    assert g._routing_no_eligible_backend_last_ts is not None


def test_422_never_touches_inflight_accounting(monkeypatch):
    """I-8b: the refusal is PRE-DISPATCH — no backend's inflight counter may
    move because of it."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["extract"]},
    ]))
    g = _fresh(monkeypatch)
    before = dict(g._llm_inflight)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _RaisingSession()
    asyncio.run(proxy.handle_proxy(_req({"X-SM-LLM-Role": "judge"}, _body())))
    assert g._llm_inflight == before


def test_422_never_waits(monkeypatch):
    """I-2a: "never queues" -- a pure ineligibility refusal must return
    without ever sleeping/polling (that behavior is reserved for the
    max_inflight capacity-wait case only)."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["extract"]},
    ]))
    g = _fresh(monkeypatch)

    async def _boom_sleep(*a, **kw):
        raise AssertionError("must not sleep/poll on a pure ineligibility refusal")
    monkeypatch.setattr(g.asyncio, "sleep", _boom_sleep)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _RaisingSession()
    resp = asyncio.run(proxy.handle_proxy(_req({"X-SM-LLM-Role": "judge"}, _body())))
    assert resp.status == 422


# ── I-3: fit is a ceiling, never a body mutation ────────────────────────────

def test_fits_backend_without_nctx_always_fits(monkeypatch):
    g = _fresh(monkeypatch)
    assert g._fits("http://anything", 10_000_000, 10_000_000) is True


def test_fits_boundary_math(monkeypatch):
    g = _fresh(monkeypatch)
    monkeypatch.setenv("FIT_MARGIN", "0.10")
    importlib.reload(g)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([{"url": "http://a:5000", "n_ctx": 1000}]))
    importlib.reload(g)
    # 1000 * (1 - 0.10) = 900 usable tokens
    assert g._fits("http://a:5000", 800, 99) is True     # 899 <= 900
    assert g._fits("http://a:5000", 800, 101) is False    # 901 > 900


def test_fit_never_rewrites_max_tokens_in_the_forwarded_body(monkeypatch):
    """I-3: the fit check may only EXCLUDE a backend; it must never modify
    the request. A backend declaring n_ctx must forward the caller's body
    byte-for-byte (no max_tokens injected) when the caller sent none."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([{"url": "http://a:5000", "n_ctx": 100000}]))
    g = _fresh(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    session = _CaptureSession()
    proxy.session = session
    req = _req({}, _body())   # no max_tokens in the caller's body
    asyncio.run(proxy.handle_proxy(req))
    forwarded = json.loads(session.captured_data)
    assert "max_tokens" not in forwarded


# ── H-1/H-3: health display + probe URL ─────────────────────────────────────

def test_v1_models_probe_url_avoids_doubling(monkeypatch):
    g = _fresh(monkeypatch)
    assert g._v1_models_probe_url("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1/models"
    assert g._v1_models_probe_url("http://localhost:5000") == "http://localhost:5000/v1/models"
    assert g._v1_models_probe_url("https://api.deepseek.com/v1/") == "https://api.deepseek.com/v1/models"


class _StatusResp:
    def __init__(self, status):
        self.status = status


class _StatusCm:
    def __init__(self, status):
        self._status = status

    async def __aenter__(self):
        return _StatusResp(self._status)

    async def __aexit__(self, *a):
        return False


class _FixedStatusSession:
    def __init__(self, status):
        self._status = status
        self.probe_headers: dict = {}   # url -> headers the probe SENT (v0.9.75)

    def get(self, url, timeout=None, headers=None):
        self.probe_headers[url] = dict(headers or {})
        return _StatusCm(self._status)


def test_a_credentialed_backend_that_401s_is_reported_unusable(monkeypatch):
    """RE-RULED at v0.9.74 (was: test_h1_credentialed_backend_401_reads_ok).

    H-1 argued that a bare-probe 401/403 from a CREDENTIALED backend should
    display `ok`, because the server ANSWERED and refusing an unauthenticated
    probe is correct auth behaviour. That is a true statement about the SERVER
    and the wrong answer to the question /health asks, which is about the
    DEPENDENCY: a backend this gateway cannot get a completion out of is not
    usable, however correct its refusal is. The practical cost was that a wrong
    or expired provider key — the one LLM failure an operator can immediately
    fix — was the one failure /health reported green.

    Enumerated as a meaning change in telemetry_contract.MEANING_CHANGES
    (fact:1626), because the KEY did not change: only what it says."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY", "private_ok": True},
    ]))
    g = _fresh(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _FixedStatusSession(401)

    async def _run():
        return await g._build_health_checks(proxy, None)
    checks = asyncio.run(_run())
    # The status code is passed through, so the payload says WHICH kind of
    # unusable — not merely that it is.
    assert checks["llm_backends"]["https://api.deepseek.com/v1"] == "http_401"
    assert checks["llm"] == "down"
    assert checks["dependencies"]["llm_pool"]["state"] == "down"


def test_h1_uncredentialed_backend_401_stays_down(monkeypatch):
    """The H-1 exception is scoped to credentialed backends only -- an
    uncredentialed (local) backend answering 401 is a real problem, not
    "correctly rejected an unauthenticated probe"."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    g = _fresh(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _FixedStatusSession(401)

    async def _run():
        return await g._build_health_checks(proxy, None)
    checks = asyncio.run(_run())
    assert checks["llm"] == "down"


def test_h1_credentialed_backend_5xx_stays_down(monkeypatch):
    """Genuinely down (5xx) is unaffected by the H-1 exception."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY", "private_ok": True},
    ]))
    g = _fresh(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _FixedStatusSession(500)

    async def _run():
        return await g._build_health_checks(proxy, None)
    checks = asyncio.run(_run())
    assert checks["llm"] == "down"


# ── P-6: steering headers stripped before upstream forward, for EVERYONE ───

def test_p6_daemon_role_header_still_stripped_before_upstream_forward(monkeypatch):
    """MUTATION TARGET (P-6): unlike the pre-routing-cycle behavior, even a
    DAEMON identity's X-SM-LLM-Role header must never reach the upstream
    backend -- the role is read for the gateway's OWN routing decision, then
    stripped before the forward. Deliberately supersedes the old assertion
    in tests/test_llm_steering_headers.py (see that file's updated docstring)."""
    monkeypatch.setenv("AGENT_TOKENS", "consolidation:tok_daemon_test")
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    assert g.AUTH_CONFIGURED_AT_STARTUP is True

    proxy = g.AsyncHiveMindProxy()
    session = _CaptureSession()
    proxy.session = session
    req = _req({"X-SM-LLM-Role": "judge"}, _body())
    req["authenticated_agent"] = "consolidation"
    asyncio.run(proxy.handle_proxy(req))
    assert session.captured_headers is not None
    assert "X-SM-LLM-Role" not in session.captured_headers
    assert "x-sm-llm-role" not in {k.lower() for k in session.captured_headers}


def test_p6_role_still_drives_routing_even_though_stripped_on_forward(monkeypatch):
    """The point of P-6 is "read then strip", not "ignore" -- the role must
    still influence WHICH backend gets the request."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["extract"]},
        {"url": "http://b:4000", "roles": ["judge"]},
    ]))
    g = _fresh(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    session = _CaptureSession()
    proxy.session = session
    req = _req({"X-SM-LLM-Role": "judge"}, _body())
    asyncio.run(proxy.handle_proxy(req))
    assert req.get("backend") == "http://b:4000"


# ── I-8 / I-8b: max_inflight concurrency cap ────────────────────────────────

def test_i8_capped_backend_excluded_when_an_alternative_eligible_backend_exists(monkeypatch):
    """MUTATION TARGET (I-8): a backend AT its cap must not be selected when
    another eligible backend has room."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "max_inflight": 1},
        {"url": "http://b:4000"},
    ]))
    g = _fresh(monkeypatch)
    g._llm_inflight["http://a:5000"] = 1   # AT cap
    chosen = g._select_llm_backend("", None)
    assert chosen == "http://b:4000"


def test_i8_cap_never_widens_eligibility(monkeypatch):
    """The cap is a CAPACITY concern layered strictly inside eligibility --
    an ineligible backend with plenty of spare capacity must never be picked
    just because the eligible one is capped; the request must 422, not
    divert."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["extract"], "max_inflight": 1},
        {"url": "http://b:4000", "roles": ["judge"]},   # NOT eligible for extract, has capacity
    ]))
    g = _fresh(monkeypatch)
    g._llm_inflight["http://a:5000"] = 1   # AT cap, the only eligible backend for extract
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _RaisingSession()
    monkeypatch.setenv("LLM_MAX_INFLIGHT_WAIT_S", "0")
    g.LLM_MAX_INFLIGHT_WAIT_S = 0.0   # don't actually wait in this test
    resp = asyncio.run(proxy.handle_proxy(_req({"X-SM-LLM-Role": "extract"}, _body())))
    assert resp.status == 503
    body = json.loads(resp.body.decode())
    assert body["error"] == "backend_at_capacity"


def test_i8_sole_eligible_backend_at_cap_frees_up_during_wait(monkeypatch):
    """When the sole eligible backend is momentarily at its cap, the request
    WAITS rather than refusing immediately -- if capacity frees up inside
    the wait window, it is selected."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "max_inflight": 1},
    ]))
    g = _fresh(monkeypatch)
    g._llm_inflight["http://a:5000"] = 1
    g.LLM_MAX_INFLIGHT_WAIT_S = 5.0
    g.LLM_MAX_INFLIGHT_POLL_S = 0.01

    async def _free_it_shortly():
        await asyncio.sleep(0.03)
        g._llm_inflight["http://a:5000"] = 0

    async def _run():
        free_task = asyncio.create_task(_free_it_shortly())
        backend = await g._select_backend_waiting_on_capacity("", None, 0.0, 0.0)
        await free_task
        return backend
    assert asyncio.run(_run()) == "http://a:5000"


def test_i8b_capacity_wait_exhausted_never_touches_inflight(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "max_inflight": 1},
    ]))
    g = _fresh(monkeypatch)
    g._llm_inflight["http://a:5000"] = 1
    g.LLM_MAX_INFLIGHT_WAIT_S = 0.02
    g.LLM_MAX_INFLIGHT_POLL_S = 0.01
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _RaisingSession()
    before = dict(g._llm_inflight)
    resp = asyncio.run(proxy.handle_proxy(_req({}, _body())))
    assert resp.status == 503
    assert g._llm_inflight == before   # unchanged: never dispatched, never accounted


# ── I-7: routing telemetry is additive ──────────────────────────────────────

def test_i7_llm_routing_and_token_usage_keys_present_for_authenticated_caller(monkeypatch):
    monkeypatch.setenv("AGENT_TOKENS", "claude:tok_routing_health_test")
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _FixedStatusSession(200)

    async def _run():
        return await g._build_health_checks(proxy, None)
    checks = asyncio.run(_run())
    assert "llm_routing" in checks
    for key in ("routed_role_extract", "routed_role_extract_last_ts",
                "routed_role_judge", "routed_role_judge_last_ts",
                "routing_no_eligible_backend", "routing_no_eligible_backend_last_ts",
                "routing_fit_rejected", "routing_fit_rejected_last_ts"):
        assert key in checks["llm_routing"]
    assert "llm_token_usage" in checks
    assert "http://a:5000" in checks["llm_token_usage"]


def test_i7_role_routed_counter_increments_on_successful_dispatch(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([{"url": "http://a:5000", "roles": ["extract"]}]))
    g = _fresh(monkeypatch)
    before = g._llm_routed_by_role["extract"]
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _CaptureSession()
    asyncio.run(proxy.handle_proxy(_req({"X-SM-LLM-Role": "extract"}, _body())))
    assert g._llm_routed_by_role["extract"] == before + 1
    assert g._llm_routed_by_role_last_ts["extract"] is not None


# ── F-3: /pool/status free_slots counts serves-all-eligible only ───────────

def test_f3_free_slots_excludes_role_scoped_backend(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["extract"]},   # NOT serves-all
        {"url": "http://b:4000"},                          # serves-all
    ]))
    g = _fresh(monkeypatch)
    resp = asyncio.run(g.handle_pool_status(None))
    d = json.loads(resp.body)
    assert d["free_slots"] == 1   # only b
    assert d["backends"]["http://a:5000"]["serves_all"] is False
    assert d["backends"]["http://b:4000"]["serves_all"] is True


# ── I-4: the gateway performs no NEW retry on truncation or 4xx/5xx ─────────

class _FixedFaultBodySession:
    """A single 429 response, once — a second call in this test would prove
    a retry happened."""
    closed = False

    def __init__(self):
        self.calls = 0

    def request(self, *a, **kw):
        self.calls += 1

        class _Resp:
            status = 429
            headers = {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            class content:
                @staticmethod
                async def iter_any():
                    if False:
                        yield b""

            async def prepare(self, request):
                return None

        return _Resp()


def test_i4_no_gateway_retry_on_a_4xx_5xx_status(monkeypatch):
    """I-4: a plain upstream fault status (not the stale-connection-reset
    class the pre-existing retry exists for) must reach the client on the
    FIRST attempt — the model-attributes routing cycle adds no new retry
    behavior; the capacity WAIT loop never issues an upstream call at all
    (see test_i8b_capacity_wait_exhausted_never_touches_inflight), and the
    eligibility/fit pre-filter runs entirely before any upstream call too."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([{"url": "http://a:5000"}]))
    g = _fresh(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    session = _FixedFaultBodySession()
    proxy.session = session
    asyncio.run(proxy.handle_proxy(_req({}, _body())))
    assert session.calls == 1


# ── N-4: dream_telemetry prompt_chars ────────────────────────────────────────

def test_n4_record_llm_call_prompt_chars_additive(monkeypatch):
    import dream_telemetry as dt
    importlib.reload(dt)
    rec = dt.record_llm_call("REM", {"model": "m"}, prompt_chars=1234)
    assert rec["prompt_chars"] == 1234
    rec2 = dt.record_llm_call("REM", {"model": "m"})
    assert rec2["prompt_chars"] is None


def test_the_backend_probe_carries_the_backends_own_bearer(monkeypatch):
    """v0.9.75 (fact:1794). v0.9.74 started counting a credentialed 401 as down
    while the probe was still a BARE GET — and DeepSeek 401s every
    unauthenticated request on every path, so a correct key read `http_401`
    and the pool `degraded` on the first live reading. The probe must send
    exactly what a real call sends: the backend's bearer, from the token map
    (never os.environ, never logged). Then a 401 means a rejected key."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY", "private_ok": True},
        {"url": "http://localhost:5000"},
    ]))
    g = _fresh(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _FixedStatusSession(200)

    async def _run():
        return await g._build_health_checks(proxy, None)
    checks = asyncio.run(_run())
    sent = proxy.session.probe_headers
    assert sent["https://api.deepseek.com/v1/models"] == {"Authorization": "Bearer sk-test"}
    assert sent["http://localhost:5000/v1/models"] == {}          # uncredentialed: nothing
    assert checks["llm_backends"]["https://api.deepseek.com/v1"] == "ok"


def test_a_401_with_the_bearer_attached_is_a_rejected_key(monkeypatch):
    """The 0.9.74 reading is only TRUE once the probe authenticates: a 401 to a
    request that carried the bearer is the key being refused, and that IS
    down. (Mutation: drop headers= from the probe → the first test dies; map a
    401 back to ok → this one dies.)"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY", "private_ok": True},
    ]))
    g = _fresh(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _FixedStatusSession(401)

    async def _run():
        return await g._build_health_checks(proxy, None)
    checks = asyncio.run(_run())
    assert proxy.session.probe_headers["https://api.deepseek.com/v1/models"]["Authorization"] == "Bearer sk-test"
    assert checks["llm_backends"]["https://api.deepseek.com/v1"] == "http_401"
    assert checks["dependencies"]["llm_pool"]["state"] == "down"
