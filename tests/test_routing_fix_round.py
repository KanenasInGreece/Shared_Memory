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
pytestmark = pytest.mark.usefixtures("chat_caller_is_consolidation")
from yarl import URL

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
import request_sizing


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


# ── C-1: free_slots counts every available declared backend ─────────────────

def test_c1_partial_roles_backend_still_counts(monkeypatch):
    """A leftover partial roles list does not remove the backend from free_slots."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["extract"], "n_ctx": 8192, "max_output_tokens": 2048},
    ]))
    g = _fresh(monkeypatch)
    resp = asyncio.run(g.handle_pool_status(_req({}, b"")))
    body = json.loads(resp.body.decode())
    assert body["free_slots"] == 1
    assert body["backends"]["http://a:5000"]["available"] is True


def test_c1_private_ok_false_still_counts(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "private_ok": True, "n_ctx": 8192, "max_output_tokens": 2048},
        {"url": "http://b:4000", "private_ok": False, "n_ctx": 8192, "max_output_tokens": 2048},
    ]))
    g = _fresh(monkeypatch)
    resp = asyncio.run(g.handle_pool_status(_req({}, b"")))
    body = json.loads(resp.body.decode())
    assert body["free_slots"] == 2
    assert body["backends"]["http://b:4000"]["available"] is True


def test_c1_pool_status_free_slots_nonzero_for_all_declared_fleet(monkeypatch):
    """One idle declared backend yields free_slots == 1. Role display keys are gone."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["extract", "judge"], "n_ctx": 8192, "max_output_tokens": 2048},
    ]))
    g = _fresh(monkeypatch)
    resp = asyncio.run(g.handle_pool_status(_req({}, b"")))
    body = json.loads(resp.body.decode())
    assert body["free_slots"] == 1
    entry = body["backends"]["http://a:5000"]
    assert entry["available"] is True
    assert "counts_free_slot" not in entry
    assert "serves_all" not in entry


# ── R-1: 503 backend_at_capacity gets its own counter ───────────────────────

def test_r1_capacity_503_increments_counter_and_last_ts(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "max_inflight": 1, "private_ok": True, "n_ctx": 8192, "max_output_tokens": 2048},
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
def test_r2_invalid_int_descriptor_is_a_declaration_fatal(monkeypatch, field, value):
    """An invalid int is a declaration FATAL for the whole file. The sibling entry does not load."""
    bad = {"url": "http://bad:5000", "n_ctx": 8192, "max_output_tokens": 2048}
    bad[field] = value
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        bad,
        {"url": "http://good:4000", "n_ctx": 8192, "max_output_tokens": 2048},
    ]))
    g = _fresh(monkeypatch)
    assert g.LLM_BACKENDS == []
    with pytest.raises(SystemExit) as exc:
        g.require_valid_llm_routing_config()
    assert exc.value.code == 78
    assert f"field {field}" in str(exc.value)
    assert "http://bad:5000" in str(exc.value)


# ── R-3: an empty roles list is a leftover, not a refusal ───────────────────

def test_r3_empty_roles_list_loads(monkeypatch, caplog):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": [], "n_ctx": 8192, "max_output_tokens": 2048},
    ]))
    import logging
    with caplog.at_level(logging.WARNING, logger="hive-proxy"):
        g = _fresh(monkeypatch)
        g.require_valid_llm_routing_config()
    assert g.LLM_BACKENDS == ["http://a:5000"]
    assert any("ignores leftover key roles" in r.message for r in caplog.records)


def test_r3_empty_roles_on_a_credentialed_backend_loads(monkeypatch):
    """roles: [] does not refuse a credentialed backend. The provider key stays out of the refusal text because there is no refusal."""
    monkeypatch.setenv("SM_TEST_PROVIDER_KEY", "k")
    monkeypatch.setenv("AGENT_TOKENS", "tester:tok")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.example.com/v1", "token_env": "SM_TEST_PROVIDER_KEY",
         "roles": [], "n_ctx": 8192, "max_output_tokens": 2048},
    ]))
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    g.require_valid_llm_routing_config()
    assert g.LLM_BACKENDS == ["https://api.example.com/v1"]
    assert "k" not in json.dumps(g.LLM_DECLARATION_ERRORS)


# ── R-4: waiter cap + doomed-route pre-wait denial ──────────────────────────

def test_r4_waiter_cap_immediate_503_beyond_cap(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "max_inflight": 1, "private_ok": True, "n_ctx": 8192, "max_output_tokens": 2048},
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
        {"url": "http://a:5000", "max_inflight": 1, "private_ok": True, "n_ctx": 8192, "max_output_tokens": 2048},
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


def test_r4_mixed_fleet_still_refuses_a_non_chat_path(monkeypatch):
    """A non-chat path is refused before selection, including when a local backend is declared."""
    monkeypatch.setenv("SM_TEST_PROVIDER_KEY", "k")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://local:5000", "n_ctx": 8192, "max_output_tokens": 2048},
        {"url": "https://api.example.com/v1", "token_env": "SM_TEST_PROVIDER_KEY",
         "n_ctx": 8192, "max_output_tokens": 2048},
    ]))
    g = _fresh(monkeypatch)

    class _S:
        closed = False
        called = False

        def request(self, method, url, **kw):
            _S.called = True
            raise AssertionError("a non-chat path must not reach upstream")
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _S()
    resp = asyncio.run(proxy.handle_proxy(
        _req({}, _body(), method="GET", path="/v1/models")))
    assert resp.status == 403
    assert "framework endpoints" in resp.body.decode()
    assert _S.called is False


# ── R-5: fit-422 carries the estimate ───────────────────────────────────────

def test_r5_fit_422_names_the_estimate(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "n_ctx": 100, "private_ok": True, "max_output_tokens": 2048},
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
    assert body["est_prompt_tokens"] == int(request_sizing.estimate_prompt_tokens(len(huge)))
    assert body["est_prompt_tokens"] > 100   # sanity: bigger than n_ctx


def test_r5_a_role_header_does_not_refuse(monkeypatch):
    """A leftover role header is not a routing constraint. The call reaches the declared backend."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["extract"], "n_ctx": 8192, "max_output_tokens": 2048},
    ]))
    g = _fresh(monkeypatch)

    class _S:
        closed = False
        url = None

        def request(self, method, url, **kw):
            _S.url = url
            raise RuntimeError("capture only")
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _S()
    resp = asyncio.run(proxy.handle_proxy(_req({"X-SM-LLM-Role": "judge"}, _body())))
    body = json.loads(resp.body.decode())
    assert resp.status != 422
    assert body.get("constraint") != "role"
    assert "declaration" not in body
    assert "role" not in body
    assert _S.url is not None and _S.url.startswith("http://a:5000")


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



