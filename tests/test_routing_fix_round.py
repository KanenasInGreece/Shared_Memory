"""Security-review fix round for the model-attributes routing cycle
(decision:1357 dispositions over the QA + security review facts).

Covers: C-1 (free_slots counts full-dream-roles backends; loud startup
warning when no backend can ever gate the dream daemons on), R-1 (503
backend_at_capacity counter + last-ts), R-2 (max_inflight must be int >= 1,
never bool), R-3 (empty roles list refuses startup), R-4 (concurrent
capacity-waiter cap; doomed non-allowlisted route denied before the wait),
R-5 (fit-422 carries the estimate that failed), R-6 (operator-declared
extra steer-permitted agent names), and the folded Optionals (n_ctx
validation, poll floor, stream:true usage-capture skip, unknown-role
warning, routed_role counted at dispatch).

Harness idioms mirror tests/test_model_attributes_routing.py."""
import asyncio
import importlib
import json
import os
import sys

import pytest
from yarl import URL

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def _fresh(monkeypatch):
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
    closed = False

    def request(self, *a, **kw):
        raise AssertionError("upstream must never be called on a refusal path")


def _req(headers: dict, body: bytes, method: str = "POST",
         path: str = "/v1/chat/completions"):
    class _Req(dict):
        pass
    r = _Req()
    r.method = method
    # T-1 (HYG round): a REAL yarl.URL — the credentialed-route gates read
    # rel_url.raw_path / .query_string, the values actually forwarded.
    # `path` is the URL's DECODED .path, exactly the split production has,
    # so it never contains '?'.
    _rel = URL(path, encoded=True)
    r.path = _rel.path
    r.rel_url = _rel
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


# ── C-1: free_slots must count a full-dream-roles backend ───────────────────

def test_c1_full_roles_backend_counts_free_slot(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["extract", "judge"]},
    ]))
    g = _fresh(monkeypatch)
    assert g._counts_free_slot("http://a:5000") is True


def test_c1_partial_roles_backend_does_not_count(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["extract"]},
    ]))
    g = _fresh(monkeypatch)
    assert g._counts_free_slot("http://a:5000") is False


def test_c1_serves_all_still_counts_and_private_false_roleless_does_not(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "private_ok": True},
        {"url": "http://b:4000", "private_ok": False},
    ]))
    g = _fresh(monkeypatch)
    assert g._counts_free_slot("http://a:5000") is True
    assert g._counts_free_slot("http://b:4000") is False


def test_c1_pool_status_free_slots_nonzero_for_all_declared_fleet(monkeypatch):
    """The exact probe from the security review: one idle backend declaring
    every dream role must yield free_slots == 1, not 0."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["extract", "judge"]},
    ]))
    g = _fresh(monkeypatch)
    resp = asyncio.run(g.handle_pool_status(_req({}, b"")))
    body = json.loads(resp.body.decode())
    assert body["free_slots"] == 1
    entry = body["backends"]["http://a:5000"]
    assert entry["counts_free_slot"] is True
    assert entry["serves_all"] is False   # additive display unchanged in meaning


def test_c1_startup_warning_when_no_dream_slot_possible(monkeypatch, caplog):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["extract"]},
    ]))
    g = _fresh(monkeypatch)
    import logging
    with caplog.at_level(logging.WARNING):
        g.warn_if_dream_slots_impossible()
    assert any("free_slots" in r.message and "NEVER run" in r.message
               for r in caplog.records)


def test_c1_no_warning_when_a_dream_slot_exists(monkeypatch, caplog):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["extract"]},
        {"url": "http://b:4000", "private_ok": True},
    ]))
    g = _fresh(monkeypatch)
    import logging
    with caplog.at_level(logging.WARNING):
        g.warn_if_dream_slots_impossible()
    assert not any("NEVER run" in r.message for r in caplog.records)


# ── R-1: 503 backend_at_capacity gets its own counter ───────────────────────

def test_r1_capacity_503_increments_counter_and_last_ts(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "max_inflight": 1, "private_ok": True},
    ]))
    monkeypatch.setenv("LLM_MAX_INFLIGHT_WAIT_S", "0.05")
    g = _fresh(monkeypatch)
    g._llm_inflight["http://a:5000"] = 1   # at cap
    before = g._routing_backend_at_capacity_count
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _RaisingSession()
    resp = asyncio.run(proxy.handle_proxy(_req({}, _body())))
    assert resp.status == 503
    assert json.loads(resp.body.decode())["error"] == "backend_at_capacity"
    assert g._routing_backend_at_capacity_count == before + 1
    assert g._routing_backend_at_capacity_last_ts is not None


def test_r1_counter_surfaced_on_health(monkeypatch):
    """The llm_routing health section must carry the capacity pair (I-7
    shape: counter + paired last-ts). Pinned against the source because a
    full _build_health_checks needs a live session; the handle_proxy test
    above proves the counter itself moves."""
    g = _fresh(monkeypatch)
    src = open(g.__file__).read()
    assert '"routing_backend_at_capacity": _routing_backend_at_capacity_count' in src
    assert '"routing_backend_at_capacity_last_ts": _routing_backend_at_capacity_last_ts' in src


# ── R-2 + Optional: int-field validation ────────────────────────────────────

@pytest.mark.parametrize("field,value", [
    ("max_inflight", 0), ("max_inflight", -1), ("max_inflight", True),
    ("n_ctx", 0), ("n_ctx", -5), ("n_ctx", True),
])
def test_r2_invalid_int_descriptor_excludes_backend(monkeypatch, field, value):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://bad:5000", field: value},
        {"url": "http://good:4000"},
    ]))
    g = _fresh(monkeypatch)
    assert "http://bad:5000" not in g.LLM_BACKENDS
    assert g.LLM_BACKENDS == ["http://good:4000"]


# ── R-3: empty roles list refuses startup ───────────────────────────────────

def test_r3_empty_roles_list_refuses_startup(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": []},
    ]))
    g = _fresh(monkeypatch)
    with pytest.raises(SystemExit) as e:
        g.require_valid_llm_routing_config()
    assert "EMPTY" in str(e.value)


def test_r3_empty_roles_does_not_sidestep_m5(monkeypatch):
    """The security probe: a credentialed backend with roles: [] must still
    refuse startup (previously it slipped past M-5's `is None` test and the
    backend silently went eligible-for-nothing)."""
    monkeypatch.setenv("SM_TEST_PROVIDER_KEY", "k")
    monkeypatch.setenv("AGENT_TOKENS", "tester:tok")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.example.com/v1", "token_env": "SM_TEST_PROVIDER_KEY",
         "roles": []},
    ]))
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    with pytest.raises(SystemExit):
        g.require_valid_llm_routing_config()


# ── R-4: waiter cap + doomed-route pre-wait denial ──────────────────────────

def test_r4_waiter_cap_immediate_503_beyond_cap(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "max_inflight": 1, "private_ok": True},
    ]))
    monkeypatch.setenv("LLM_MAX_INFLIGHT_WAIT_S", "5")
    g = _fresh(monkeypatch)
    g._llm_inflight["http://a:5000"] = 1          # at cap
    g._capacity_waiters = g.LLM_MAX_CAPACITY_WAITERS   # queue full

    async def _boom_sleep(*a, **kw):
        raise AssertionError("beyond the waiter cap the request must NOT wait")
    monkeypatch.setattr(g.asyncio, "sleep", _boom_sleep)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _RaisingSession()
    resp = asyncio.run(proxy.handle_proxy(_req({}, _body())))
    assert resp.status == 503
    assert json.loads(resp.body.decode())["error"] == "backend_at_capacity"
    assert g._capacity_waiters == g.LLM_MAX_CAPACITY_WAITERS   # untouched


def test_r4_waiter_count_released_after_wait(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "max_inflight": 1, "private_ok": True},
    ]))
    monkeypatch.setenv("LLM_MAX_INFLIGHT_WAIT_S", "0.05")
    g = _fresh(monkeypatch)
    g._llm_inflight["http://a:5000"] = 1
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _RaisingSession()
    resp = asyncio.run(proxy.handle_proxy(_req({}, _body())))
    assert resp.status == 503
    assert g._capacity_waiters == 0   # increment released on exit (I-8b discipline)


def test_r4_doomed_route_denied_before_any_wait(monkeypatch):
    """A GET to a non-allowlisted path whose ONLY eligible backends are
    credentialed must be 403'd immediately — it may never hold a
    capacity-wait slot for the full window."""
    monkeypatch.setenv("SM_TEST_PROVIDER_KEY", "k")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.example.com/v1", "token_env": "SM_TEST_PROVIDER_KEY",
         "private_ok": True, "max_inflight": 1},
    ]))
    monkeypatch.setenv("LLM_MAX_INFLIGHT_WAIT_S", "60")
    g = _fresh(monkeypatch)
    g._llm_inflight["https://api.example.com/v1"] = 1   # at cap: old code would wait

    async def _boom_sleep(*a, **kw):
        raise AssertionError("doomed route must be denied BEFORE waiting")
    monkeypatch.setattr(g.asyncio, "sleep", _boom_sleep)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _RaisingSession()
    resp = asyncio.run(proxy.handle_proxy(
        _req({}, _body(), method="GET", path="/v1/models")))
    assert resp.status == 403


def test_r4_mixed_fleet_not_doomed_falls_through(monkeypatch):
    """With an uncredentialed eligible backend present, the same
    non-allowlisted route proceeds (selection picks the local one)."""
    monkeypatch.setenv("SM_TEST_PROVIDER_KEY", "k")
    # local FIRST: at equal inflight the least-inflight tie-break keeps pool
    # order, so selection deterministically picks the uncredentialed backend
    # (if it picked the credentialed one, the post-selection S-04 check would
    # 403 — pre-existing behavior, not the doomed-denial under test here).
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://local:5000", "private_ok": True},
        {"url": "https://api.example.com/v1", "token_env": "SM_TEST_PROVIDER_KEY",
         "private_ok": True},
    ]))
    g = _fresh(monkeypatch)

    class _S:
        closed = False
        captured_url = None

        def request(self, method, url, **kw):
            _S.captured_url = url
            raise RuntimeError("capture only")
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _S()
    resp = asyncio.run(proxy.handle_proxy(
        _req({}, _body(), method="GET", path="/v1/models")))
    # not the 403 doomed-denial; the attempt reached upstream selection
    assert resp.status != 403
    assert _S.captured_url is not None and _S.captured_url.startswith("http://local:5000")


# ── R-5: fit-422 carries the estimate ───────────────────────────────────────

def test_r5_fit_422_names_the_estimate(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "n_ctx": 100, "private_ok": True},
    ]))
    g = _fresh(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _RaisingSession()
    huge = _body(**{"messages": [{"role": "user", "content": "x" * 5000}],
                    "max_tokens": 64})
    resp = asyncio.run(proxy.handle_proxy(_req({}, huge)))
    assert resp.status == 422
    body = json.loads(resp.body.decode())
    assert body["constraint"] == "fit"
    # concrete VALUES, not mere presence: est = body_chars / 1.2, and the
    # caller's own max_tokens
    assert body["effective_max_tokens"] == 64
    assert body["est_prompt_tokens"] == int(len(huge) / g.CHARS_PER_TOKEN_RATIO)
    assert body["est_prompt_tokens"] > 100   # sanity: bigger than n_ctx


def test_r5_role_422_body_gains_only_the_additive_declaration_key(monkeypatch):
    """W4 (§5, Ruling C(α)/E(α2), was
    test_r5_role_422_body_shape_unchanged): the three ORIGINAL keys are
    still exactly unchanged; the body now ALSO carries the additive
    `declaration` key for this undeclared, roles-only fleet."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["extract"]},
    ]))
    g = _fresh(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _RaisingSession()
    resp = asyncio.run(proxy.handle_proxy(_req({"X-SM-LLM-Role": "judge"}, _body())))
    body = json.loads(resp.body.decode())
    assert body == {"error": "no_eligible_backend", "constraint": "role", "role": "judge",
                     "declaration": "no_role_less_opt_in"}


# ── R-6: operator-declared extra steer-permitted names ──────────────────────

def test_r6_extra_steer_agent_name_unioned(monkeypatch):
    monkeypatch.setenv("LLM_STEER_EXTRA_AGENT_NAMES", "night_sweep, night_tool")
    g = _fresh(monkeypatch)
    assert "night_sweep" in g.DAEMON_AGENT_NAMES
    assert "night_tool" in g.DAEMON_AGENT_NAMES
    assert "consolidation" in g.DAEMON_AGENT_NAMES
    assert "rem_daemon" in g.DAEMON_AGENT_NAMES


def test_r6_unset_env_keeps_exactly_the_two_daemons(monkeypatch):
    monkeypatch.delenv("LLM_STEER_EXTRA_AGENT_NAMES", raising=False)
    g = _fresh(monkeypatch)
    assert g.DAEMON_AGENT_NAMES == frozenset({"consolidation", "rem_daemon"})


# ── Folded Optionals ────────────────────────────────────────────────────────

def test_opt_poll_interval_floor(monkeypatch):
    monkeypatch.setenv("LLM_MAX_INFLIGHT_POLL_S", "0")
    g = _fresh(monkeypatch)
    assert g.LLM_MAX_INFLIGHT_POLL_S == 0.05


def test_opt_unknown_role_warned_once_per_value(monkeypatch, caplog):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000"},
    ]))
    g = _fresh(monkeypatch)
    import logging
    with caplog.at_level(logging.WARNING):
        g._warn_unknown_role_once("summarise")
        g._warn_unknown_role_once("summarise")
        g._warn_unknown_role_once("chat")
    msgs = [r.message for r in caplog.records if "not a known routing role" in r.message]
    assert len(msgs) == 2   # one per DISTINCT value, not per call


def test_opt_routed_role_counted_at_dispatch_not_selection(monkeypatch):
    """A request refused at S-04 AFTER selection must not bump the per-role
    routed counter — the counter now lives beside the inflight increment."""
    monkeypatch.setenv("SM_TEST_PROVIDER_KEY", "k")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.example.com/v1", "token_env": "SM_TEST_PROVIDER_KEY",
         "roles": ["extract"]},
    ]))
    g = _fresh(monkeypatch)
    before = g._llm_routed_by_role["extract"]
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _RaisingSession()
    # extract-role traffic on a non-allowlisted route: eligible (role listed),
    # doomed (only credentialed backends) → 403 pre-dispatch
    resp = asyncio.run(proxy.handle_proxy(
        _req({"X-SM-LLM-Role": "extract"}, _body(), method="GET", path="/v1/models")))
    assert resp.status == 403
    assert g._llm_routed_by_role["extract"] == before
