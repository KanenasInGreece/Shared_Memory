"""END-TO-END: the gateway in front of a real, misbehaving OpenAI-compatible
upstream.

⭐ This is the preferred mechanism under decision:2671 (E2E over unit tests,
each run ending in a verifiable repeatable artifact). Nothing here is spied or
stubbed INSIDE the gateway: a real aiohttp upstream listens on a real port, the
real AsyncHiveMindProxy holds a real ClientSession, the real routes are
registered in main()'s order, and a real client drives real sockets. The only
fiction is the upstream's behaviour, which is the whole point — you cannot ask
a hosted provider to return 429 on demand, which is exactly why this class of
defect survived until now.

WHY THE GATEWAY WAS WRONG ABOUT HOSTED BACKENDS. The pool was designed around a
local llama-server started with -np 1, where one in-flight request really does
mean the card is busy. A hosted provider serves many requests at once, reports
its faults as an HTTP status rather than a dropped connection, and rate-limits.
Each scenario below drives one of those three differences.

THE ARTIFACT. Every run writes a JSON transcript naming the fleet it
configured, every observation it made, and the upstream's own hit counters, to
$SM_E2E_ARTIFACT_DIR (default ~/.shared-memory/e2e). It is diffable between
runs and it outlives the process, because a pytest exit code is not an
artifact.

SCOPE, stated plainly: this exercises the proxy and pool path, not a whole
install — there is no Postgres, Neo4j or daemon here. postflight.sh remains the
install E2E.
"""
import asyncio
import importlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared-memory" / "scripts"))


# ── the misbehaving upstream ─────────────────────────────────────────────────

class StubProvider:
    """An OpenAI-compatible upstream whose behaviour is switchable per route.

    `chat_mode`: "ok" | "slow" | "http_500" | "http_429"
    `models_status`: the status GET /v1/models answers with.
    Counts every hit, so the probe cadence can be measured rather than asserted.
    """

    def __init__(self):
        self.chat_mode = "ok"
        self.models_status = 200
        self.hits = {"chat": 0, "models": 0}
        self.slow_seconds = 0.6
        self.app = web.Application()
        self.app.router.add_post("/v1/chat/completions", self._chat)
        self.app.router.add_get("/v1/models", self._models)

    async def _chat(self, request):
        self.hits["chat"] += 1
        await request.read()
        if self.chat_mode == "slow":
            await asyncio.sleep(self.slow_seconds)
        elif self.chat_mode == "http_500":
            return web.json_response({"error": {"type": "server_error"}}, status=500)
        elif self.chat_mode == "http_429":
            return web.json_response({"error": {"type": "rate_limit"}}, status=429)
        return web.json_response(
            {"id": "c1", "model": "stub-model",
             "choices": [{"message": {"role": "assistant", "content": "ok"}}],
             "usage": {"prompt_tokens": 3, "completion_tokens": 1}})

    async def _models(self, request):
        self.hits["models"] += 1
        if self.models_status != 200:
            return web.json_response({"error": "no"}, status=self.models_status)
        return web.json_response({"data": [{"id": "stub-model"}]})


def _artifact_dir() -> Path:
    """Where the run's transcript lands.

    SM_E2E_ARTIFACT_DIR is the durable, opt-in location an operator sets when
    they want the artifact to outlive the run. With it unset the transcript goes
    to a temporary directory instead of the real home: an ordinary `pytest
    tests/` by a stranger must not write into ~/.shared-memory, which is the
    same rule conftest applies to the credential-audit and capacity logs.
    """
    raw = os.environ.get("SM_E2E_ARTIFACT_DIR")
    d = (Path(os.path.expanduser(raw)) if raw
         else Path(tempfile.gettempdir()) / "sm-e2e-artifacts")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_artifact(name: str, record: dict) -> Path:
    record["recorded_at"] = datetime.now(timezone.utc).isoformat()
    path = _artifact_dir() / f"{name}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True))
    return path


@pytest.fixture(autouse=True)
def _restore_gateway_module_state():
    """Put coordinator and hive_mind_proxy back after every test in this file.

    ⚠ Both modules read their configuration at IMPORT, and every scenario here
    reloads them with a synthetic fleet, so without an explicit restore the
    next file inherits this one's backends, its probe intervals and its
    auth-off flag. Measured, not theorised: leaving this out failed four tests
    in test_pool_status*.py and test_pool_status_auth_gating.py that pass in
    isolation, while these scenarios in turn failed on state an earlier file
    had left behind (fact:1194 — a green unit proves nothing about
    composition).
    """
    yield
    for var in ("LLM_BACKENDS_JSON", "LLM_BACKENDS", "LLM_PROBE_INTERVAL_S",
                "LLM_PROBE_INTERVAL_CREDENTIALED_S", "E2E_PROVIDER_KEY"):
        os.environ.pop(var, None)
    import secure_env
    # E2E_PROVIDER_KEY is read through get_secret, which CACHES it, so clearing
    # the environment alone would leave this file's invented value readable by
    # every later test in the session.
    for cached in ("AGENT_TOKENS", "E2E_PROVIDER_KEY"):
        secure_env._secrets.pop(cached, None)
    import coordinator
    import hive_mind_proxy
    importlib.reload(coordinator)
    importlib.reload(hive_mind_proxy)


def _load_gateway(monkeypatch, backends):
    """Reload the gateway with this fleet, the way the module loads it at boot.

    AGENT_TOKENS is unset deliberately: that is the shipped default, and it is
    the install where routing is the only containment there is. Env goes
    through monkeypatch so it is restored even when a scenario raises.
    """
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(backends))
    # setdefault, not setenv: a scenario that needs a particular cadence sets it
    # before calling this, and clobbering that made the cadence test measure the
    # default instead of the gap it was configured to measure.
    monkeypatch.setenv("LLM_PROBE_INTERVAL_S",
                       os.environ.get("LLM_PROBE_INTERVAL_S") or "0.2")
    monkeypatch.setenv("LLM_PROBE_INTERVAL_CREDENTIALED_S",
                       os.environ.get("LLM_PROBE_INTERVAL_CREDENTIALED_S") or "1.0")
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    return coordinator, g


def _build_app(c, g, proxy):
    """main()'s registration window, in main()'s order."""
    from unittest.mock import AsyncMock
    app = web.Application(middlewares=[c.auth_middleware])
    app["proxy"] = proxy
    app["coordinator"] = AsyncMock()
    app.on_response_prepare.append(g._set_server_header)
    app.router.add_get("/pool/status", g.handle_pool_status)
    proxy.set_known_routes(app.router)
    app.router.add_route("*", "/{tail:.*}", proxy.handle_proxy)
    return app


async def _chat(client, **kw):
    return await client.post("/v1/chat/completions",
                             json={"model": "local-model",
                                   "messages": [{"role": "user", "content": "hi"}]},
                             **kw)


# ══════════════════════════════════════════════════════════════════════════
# S1 — a many-slot provider keeps serving while one request is in flight
# ══════════════════════════════════════════════════════════════════════════

def test_e2e_a_declared_multi_slot_backend_stays_available_while_busy(monkeypatch):
    """F1 through real sockets: the hosted backend declares 4 slots and the
    local one declares nothing. Hold one request open against each; the hosted
    one must still report spare capacity and keep free_slots above zero, while
    the undeclared one must behave exactly as it always did."""
    obs = {}

    async def scenario():
        # Two real upstreams: one standing in for a hosted provider that
        # declares 4 slots, one for a local card that declares nothing. They
        # must be separate servers — pointing the second at a sub-path of the
        # first makes it 404, release its slot at once, and measure nothing.
        stub, stub_local = StubProvider(), StubProvider()
        up, up_local = TestServer(stub.app), TestServer(stub_local.app)
        await up.start_server()
        await up_local.start_server()
        hosted = f"http://{up.host}:{up.port}"
        local = f"http://{up_local.host}:{up_local.port}"
        c, g = _load_gateway(monkeypatch, [
            {"url": hosted, "max_inflight": 4, "private_ok": True},
            {"url": local, "private_ok": True},
        ])
        obs["fleet"] = {"hosted": hosted, "local": local,
                        "hosted_max_inflight": 4, "local_max_inflight": None}
        proxy = g.AsyncHiveMindProxy()
        await proxy.start_session()
        app = _build_app(c, g, proxy)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            # Generous, because the snapshot must happen while BOTH requests are
            # still in flight and the pinning below spends ~0.4 s getting there;
            # a 0.6 s upstream left too little margin on a loaded machine.
            stub.slow_seconds = stub_local.slow_seconds = 5.0
            stub.chat_mode = stub_local.chat_mode = "slow"
            # Pin one request to EACH backend. Left to itself the router picks
            # least-in-flight and would send both to the hosted one, leaving
            # the undeclared backend idle and the comparison meaningless.
            async def _pinned(url):
                g.LLM_POOL[:] = [url]
                task = asyncio.create_task(_chat(client))
                await asyncio.sleep(0.15)   # let it reach dispatch on this pin
                return task
            t_hosted = await _pinned(hosted)
            t_local = await _pinned(local)
            g.LLM_POOL[:] = [hosted, local]
            inflight = [t_hosted, t_local]
            await asyncio.sleep(0.1)
            snap = await (await client.get("/pool/status")).json()
            obs["while_busy"] = snap
            obs["inflight_seen"] = {b: e["inflight"] for b, e in snap["backends"].items()}
            await asyncio.gather(*inflight)
            obs["after_drain"] = await (await client.get("/pool/status")).json()
        finally:
            await client.close()
            await proxy.cleanup()
            await up.close()
            await up_local.close()
        obs["upstream_hits"] = {"hosted": dict(stub.hits), "local": dict(stub_local.hits)}

    asyncio.run(scenario())
    path = _write_artifact("e2e_s1_multi_slot_availability", obs)

    busy = obs["while_busy"]
    # Identify the two backends by the fleet the run recorded, never by a
    # substring of the URL: both are plain host:port and a substring match
    # silently picked the same entry twice.
    hosted_entry = busy["backends"][obs["fleet"]["hosted"]]
    local_entry = busy["backends"][obs["fleet"]["local"]]
    assert local_entry["inflight"] >= 1, (
        f"the undeclared backend was never occupied, so this compares nothing; "
        f"artifact {path}")
    assert hosted_entry["inflight"] >= 1, f"nothing was in flight; artifact {path}"
    assert hosted_entry["available"] is True, (
        f"a backend declaring 4 slots must stay available at 1 in flight; artifact {path}")
    assert local_entry["available"] is False, (
        f"an UNDECLARED backend must still mean one slot; artifact {path}")
    assert busy["free_slots"] >= 1, f"the dream cycle would have stalled; artifact {path}"


# ══════════════════════════════════════════════════════════════════════════
# S2 — an HTTP-erroring backend leaves rotation instead of attracting traffic
# ══════════════════════════════════════════════════════════════════════════

def test_e2e_an_http_erroring_backend_is_cooled_down_and_traffic_moves_off_it(monkeypatch):
    """F2 and F3 through real sockets. The failing upstream answers 500
    instantly, so it always looks least-busy; without the fix it would be
    PREFERRED over a healthy backend. Drive it past the threshold, then assert
    the next request is served by the healthy backend instead."""
    obs = {}

    async def scenario():
        bad, good = StubProvider(), StubProvider()
        up_bad, up_good = TestServer(bad.app), TestServer(good.app)
        await up_bad.start_server()
        await up_good.start_server()
        bad_url = f"http://{up_bad.host}:{up_bad.port}"
        good_url = f"http://{up_good.host}:{up_good.port}"
        c, g = _load_gateway(monkeypatch, [{"url": bad_url, "private_ok": True},
                              {"url": good_url, "private_ok": True}])
        obs["fleet"] = {"failing": bad_url, "healthy": good_url,
                        "http_fail_threshold": g.LLM_HTTP_FAIL_THRESHOLD}
        proxy = g.AsyncHiveMindProxy()
        await proxy.start_session()
        app = _build_app(c, g, proxy)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            bad.chat_mode = "http_500"
            # Pin every request to the failing backend until it is cooled.
            g.LLM_POOL[:] = [bad_url]
            statuses = []
            for _ in range(g.LLM_HTTP_FAIL_THRESHOLD):
                r = await _chat(client)
                statuses.append(r.status)
            obs["statuses_from_failing_backend"] = statuses
            obs["cooldown_s"] = round(
                max(0.0, g._llm_unhealthy_until.get(bad_url, 0.0) - time.monotonic()), 1)
            # Now offer both: a cooled backend must not win least-in-flight.
            g.LLM_POOL[:] = [bad_url, good_url]
            r = await _chat(client)
            obs["status_after_cooldown"] = r.status
            obs["served_by"] = r.headers.get("X-SM-LLM-Backend")
            obs["upstream_hits"] = {"failing": dict(bad.hits), "healthy": dict(good.hits)}
        finally:
            await client.close()
            await proxy.cleanup()
            await up_bad.close()
            await up_good.close()

    asyncio.run(scenario())
    path = _write_artifact("e2e_s2_http_fault_cooldown", obs)

    assert all(s == 500 for s in obs["statuses_from_failing_backend"]), (
        f"the failing upstream did not actually fail; artifact {path}")
    assert obs["cooldown_s"] > 0, (
        f"an all-500 backend never entered cooldown, so HTTP faults are invisible "
        f"to routing; artifact {path}")
    assert obs["status_after_cooldown"] == 200, (
        f"traffic did not move to the healthy backend; artifact {path}")
    assert obs["upstream_hits"]["healthy"]["chat"] == 1, (
        f"the healthy backend served nothing — the cooled one was still preferred; "
        f"artifact {path}")


# ══════════════════════════════════════════════════════════════════════════
# S3 — a rate-limited liveness probe is not an outage
# ══════════════════════════════════════════════════════════════════════════

def test_e2e_a_rate_limited_probe_does_not_report_a_working_pool_as_down(monkeypatch):
    """F4 through the real probe daemon against a real upstream: /v1/models
    answers 429 while /v1/chat/completions keeps working. The pool must read
    healthy AND a real completion must still succeed in the same run."""
    obs = {}

    async def scenario():
        stub = StubProvider()
        up = TestServer(stub.app)
        await up.start_server()
        url = f"http://{up.host}:{up.port}"
        c, g = _load_gateway(monkeypatch, [{"url": url, "private_ok": True}])
        proxy = g.AsyncHiveMindProxy()
        await proxy.start_session()
        app = _build_app(c, g, proxy)
        client = TestClient(TestServer(app))
        await client.start_server()
        stop = asyncio.Event()
        try:
            stub.models_status = 429           # metering the probe, serving traffic
            probe = asyncio.create_task(g._llm_probe_daemon(proxy, stop))
            # LLM_PROBE_INTERVAL_S is floored at 0.5s, so wait past two of them.
            await asyncio.sleep(1.3)
            assert not probe.done(), f"probe daemon died: {probe.exception()!r}"
            obs["probe_status_map"] = dict(g._llm_status_cache)
            obs["llm_pool_dependency"] = g._llm_pool_dependency(dict(g._llm_status_cache))
            r = await _chat(client)
            obs["real_completion_status"] = r.status
            # And a genuinely dead path must still read down.
            stub.models_status = 503
            await asyncio.sleep(1.3)
            assert not probe.done(), f"probe daemon died: {probe.exception()!r}"
            obs["probe_status_map_when_failing"] = dict(g._llm_status_cache)
            obs["llm_pool_dependency_when_failing"] = g._llm_pool_dependency(
                dict(g._llm_status_cache))
            stop.set()
            await asyncio.wait_for(probe, timeout=5)
            obs["upstream_hits"] = dict(stub.hits)
        finally:
            stop.set()
            await client.close()
            await proxy.cleanup()
            await up.close()

    asyncio.run(scenario())
    path = _write_artifact("e2e_s3_rate_limited_probe", obs)

    assert set(obs["probe_status_map"].values()) == {"ok"}, (
        f"a 429 on the liveness probe was read as not-ok; artifact {path}")
    assert obs["llm_pool_dependency"]["state"] == "ok", (
        f"a rate-limited probe must leave the pool exactly ok — 'not down' would "
        f"also accept a regression to degraded; artifact {path}")
    assert obs["real_completion_status"] == 200, (
        f"the backend that was called down did not actually serve; artifact {path}")
    assert obs["llm_pool_dependency_when_failing"]["state"] == "down", (
        f"a genuinely failing backend must still read down; artifact {path}")


# ══════════════════════════════════════════════════════════════════════════
# S4 — a credentialed backend is not probed at the loopback cadence
# ══════════════════════════════════════════════════════════════════════════

def test_e2e_a_credentialed_backend_is_probed_far_less_often(monkeypatch):
    """F4's other half, counted rather than asserted. The two backends differ
    only in whether a provider key is attached; the credentialed one must
    receive materially fewer probes over the same window.

    This test is only possible BECAUSE the interval became env-overridable —
    the previous hardcoded 3.0 s could only have been tested by waiting."""
    obs = {}

    async def scenario():
        plain, keyed = StubProvider(), StubProvider()
        up_p, up_k = TestServer(plain.app), TestServer(keyed.app)
        await up_p.start_server()
        await up_k.start_server()
        plain_url = f"http://{up_p.host}:{up_p.port}"
        keyed_url = f"http://{up_k.host}:{up_k.port}"
        monkeypatch.setenv("E2E_PROVIDER_KEY", "k" * 20)
        # A wider gap between the two intervals, so the comparison below is a
        # ratio with room in it rather than an absolute count sitting on its
        # own boundary.
        monkeypatch.setenv("LLM_PROBE_INTERVAL_CREDENTIALED_S", "2.0")
        c, g = _load_gateway(monkeypatch, [
            {"url": plain_url, "private_ok": True},
            {"url": keyed_url, "private_ok": True, "token_env": "E2E_PROVIDER_KEY",
             "plaintext_ok": True},
        ])
        obs["intervals"] = {"plain_s": g.LLM_PROBE_INTERVAL_S,
                            "credentialed_s": g.LLM_PROBE_INTERVAL_CREDENTIALED_S}
        obs["keyed_backend_has_credential"] = g.LLM_BACKEND_TOKENS.get(keyed_url) is not None
        proxy = g.AsyncHiveMindProxy()
        await proxy.start_session()
        stop = asyncio.Event()
        try:
            probe = asyncio.create_task(g._llm_probe_daemon(proxy, stop))
            await asyncio.sleep(2.2)
            stop.set()
            await asyncio.wait_for(probe, timeout=5)
        finally:
            stop.set()
            await proxy.cleanup()
            await up_p.close()
            await up_k.close()
        obs["probe_hits"] = {"plain": plain.hits["models"], "credentialed": keyed.hits["models"]}

    asyncio.run(scenario())
    path = _write_artifact("e2e_s4_probe_cadence", obs)

    assert obs["keyed_backend_has_credential"] is True, (
        f"the credentialed backend was not credentialed, so this measured nothing; "
        f"artifact {path}")
    hits = obs["probe_hits"]
    assert hits["credentialed"] >= 1, (
        f"the credentialed backend was never probed at all, so this measured "
        f"nothing; artifact {path}")
    # A ratio, not an absolute count: the plain interval is a quarter of the
    # credentialed one, so anything less than twice as many plain probes means
    # the separation is not being honoured, and a loaded machine that runs
    # fewer cycles overall still satisfies it.
    assert hits["plain"] >= 2 * hits["credentialed"], (
        f"the credentialed backend was probed nearly as often as the plain one "
        f"({hits}); artifact {path}")
