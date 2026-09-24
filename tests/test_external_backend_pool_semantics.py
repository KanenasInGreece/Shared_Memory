"""Isolation tests for the external-backend pool semantics.

⚠ These exist under decision:2671 (E2E is the preferred mechanism; an isolation
test is allowed only where an adversarial pass has already NAMED the failure it
kills, and each test must die when that failure is re-introduced). The E2E that
proves the same fixes through real sockets is
tests/test_external_backend_e2e.py; every test here covers a boundary that E2E
can only sample, or a condition E2E cannot reach without wall-clock waiting.

Each test names the failure it kills and the mutation that must break it.
"""
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared-memory" / "scripts"))
import hive_mind_proxy as P  # noqa: E402


# ── F1: the reporting cap ────────────────────────────────────────────────────
# NAMED FAILURE: the undeclared case silently changes behaviour, so every local
# backend that never declared max_inflight starts reporting spare capacity it
# does not have. MUTATION: `cap or 1` -> `cap or 2` must break test_undeclared_*.

def test_undeclared_max_inflight_reports_exactly_one_slot(monkeypatch):
    """A backend that declares nothing keeps the old inflight == 0 meaning."""
    monkeypatch.setitem(P.LLM_BACKEND_MAX_INFLIGHT, "http://local:5000", None)
    assert P._reported_max_inflight("http://local:5000") == 1


def test_backend_absent_from_the_map_entirely_reports_one_slot():
    """A backend with no entry at all is the same as declaring nothing."""
    assert P._reported_max_inflight("http://never-configured:1") == 1


def test_declared_max_inflight_is_reported_verbatim(monkeypatch):
    monkeypatch.setitem(P.LLM_BACKEND_MAX_INFLIGHT, "https://api.example.com/v1", 32)
    assert P._reported_max_inflight("https://api.example.com/v1") == 32


# ── F2: which upstream statuses are a verdict on the BACKEND ─────────────────
# NAMED FAILURE: a 400 or a 401 cools a backend down. Those are verdicts on the
# request, not the backend, and they do not heal on a timer, so a cooldown would
# hide the cause and cost availability for nothing.
# MUTATION: `status == 429 or status >= 500` -> `status >= 400` must break this.

@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_backend_faulting_statuses_count_toward_the_cooldown(status):
    assert P._http_status_faults_backend(status) is True


@pytest.mark.parametrize("status", [200, 201, 400, 401, 403, 404, 409, 422])
def test_request_level_statuses_never_cool_a_backend_down(status):
    assert P._http_status_faults_backend(status) is False


# ── F2 shape: consecutive failures, not a rate ───────────────────────────────
# NAMED FAILURE (adversarial, Gemini Pro 3.1 "F-AMPLIFY"): an absolute count in
# a window penalises a healthy high-throughput provider. It does not, because a
# success clears the streak — but nothing pinned that, so a future edit to
# _llm_mark_ok could silently turn this into the rate limiter it was accused of
# being. E2E cannot reach this without hundreds of requests per second.
# MUTATION: remove the reset in _llm_mark_ok must break test_interleaved_*.

def _reset(backend):
    P._llm_unhealthy_until[backend] = 0.0
    P._llm_fail_times[backend] = []


def test_interleaved_successes_keep_a_busy_backend_out_of_cooldown():
    """A busy provider at 95% success must never cool down: the streak resets.

    ⚠ The fault count here (20) is deliberately well ABOVE
    LLM_HTTP_FAIL_THRESHOLD, and that is what gives the test teeth. An earlier
    version fired only 4 faults against a threshold of 5, so it passed whether
    or not the reset existed and the mutation survived — the test proved
    nothing (fact:1067: a surviving mutation is first a question about the
    harness). With 20 interleaved faults, removing the reset in _llm_mark_ok
    cools the backend at the 5th and this assertion fails.
    """
    b = "https://api.example.com/v1"
    _reset(b)
    faults = 0
    for i in range(400):
        if i % 20 == 19:
            P._llm_mark_fail(b, threshold=P.LLM_HTTP_FAIL_THRESHOLD)
            faults += 1
        else:
            P._llm_mark_ok(b)
    assert faults > P.LLM_HTTP_FAIL_THRESHOLD, (
        "the test must fire more faults than the threshold or it cannot fail")
    assert P._llm_unhealthy_until[b] == 0.0
    _reset(b)


def test_consecutive_http_faults_do_cool_the_backend_down():
    b = "https://api.example.com/v1"
    _reset(b)
    for _ in range(P.LLM_HTTP_FAIL_THRESHOLD):
        P._llm_mark_fail(b, threshold=P.LLM_HTTP_FAIL_THRESHOLD)
    assert P._llm_unhealthy_until[b] > 0.0
    _reset(b)


def test_the_transport_threshold_is_not_moved_by_the_http_one():
    """A transport failure still trips at LLM_FAIL_THRESHOLD, unchanged."""
    b = "http://local:5000"
    _reset(b)
    for _ in range(P.LLM_FAIL_THRESHOLD):
        P._llm_mark_fail(b)
    assert P._llm_unhealthy_until[b] > 0.0
    _reset(b)


# ── F4: the probe verdict ────────────────────────────────────────────────────
# NAMED FAILURE (fact:1794's defect, inverted): a rejected key reads as healthy.
# MUTATION: make 401 return "ok" must break test_a_rejected_key_*.

def test_a_rate_limited_probe_means_the_backend_is_alive():
    """429 is the one 4xx that proves the backend is up and will serve."""
    assert P._classify_probe_status(429) == "ok"


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_is_never_reported_as_healthy(status):
    assert P._classify_probe_status(status) == f"http_{status}"


@pytest.mark.parametrize("status", [500, 502, 503])
def test_a_failing_backend_is_not_healthy(status):
    assert P._classify_probe_status(status) == f"http_{status}"


@pytest.mark.parametrize("status", [200, 204])
def test_a_served_probe_is_healthy(status):
    assert P._classify_probe_status(status) == "ok"


# ── F4 + F2 composition: the blackhole ───────────────────────────────────────
# NAMED FAILURE (adversarial, Gemini Pro 3.1 "F-BLACKHOLE", and a regression a
# first cut of this change actually shipped into the branch): a mistyped backend
# URL 404s every call. If the probe calls that alive AND 404 is exempt from the
# fail streak, the backend reads healthy, never cools down, and absorbs traffic
# indefinitely with nothing anywhere showing it.
# This is the composition of two rules that are each defensible alone, which is
# exactly the class a per-function test cannot see.
# MUTATION: make _classify_probe_status(404) return "ok" must break this.

def test_a_backend_that_404s_everything_cannot_become_a_silent_blackhole():
    reads_healthy = P._classify_probe_status(404) == "ok"
    cools_down = P._http_status_faults_backend(404)
    assert not (reads_healthy and not cools_down), (
        "a backend 404ing every call would read healthy AND never cool down")


@pytest.fixture
def declared_fleet(monkeypatch):
    """A pool whose CONFIGURATION is complete, so the only thing left that can
    move `llm_pool` is the probe verdict.

    Without this the dependency also folds in config facts — a test process
    that declared no LLM_BACKENDS_JSON reads `degraded` for
    LLM_POOL_CONFIG_EMPTY no matter what the probe said, which would have let
    these tests pass or fail for a reason that has nothing to do with the
    probe.
    """
    url = "https://api.example.com/v1"
    monkeypatch.setattr(P, "LLM_POOL_CONFIG_EMPTY", False)
    monkeypatch.setattr(P, "LLM_POOL_FALLBACK_REASON", None)
    monkeypatch.setattr(P, "LLM_POOL", [url])
    monkeypatch.setitem(P.LLM_BACKEND_ROLES, url, None)
    monkeypatch.setitem(P.LLM_BACKEND_PRIVATE_OK, url, True)
    return url


def test_a_404_backend_is_surfaced_as_down_on_the_pool_dependency(declared_fleet):
    dep = P._llm_pool_dependency({declared_fleet: P._classify_probe_status(404)})
    assert dep["state"] == "down"


def test_a_rate_limited_backend_is_not_reported_as_a_dead_pool(declared_fleet):
    """The defect F4 exists for: a healthy provider metering its probe."""
    dep = P._llm_pool_dependency({declared_fleet: P._classify_probe_status(429)})
    assert dep["state"] == "ok"


# ── F4 cadence: a credentialed backend is probed less often ──────────────────
# NAMED FAILURE: the credentialed interval silently collapses to the plain one
# and a hosted provider is probed ~28,800 times a day again.
# MUTATION: make _probe_interval_for always return LLM_PROBE_INTERVAL_S.

def test_a_credentialed_backend_gets_the_longer_probe_interval(monkeypatch):
    monkeypatch.setitem(P.LLM_BACKEND_TOKENS, "https://api.example.com/v1", "tok")
    assert (P._probe_interval_for("https://api.example.com/v1")
            == P.LLM_PROBE_INTERVAL_CREDENTIALED_S)


def test_an_uncredentialed_backend_keeps_the_plain_probe_interval(monkeypatch):
    monkeypatch.setitem(P.LLM_BACKEND_TOKENS, "http://local:5000", None)
    assert P._probe_interval_for("http://local:5000") == P.LLM_PROBE_INTERVAL_S


def test_the_credentialed_interval_can_never_be_shorter_than_the_plain_one():
    """Config cannot invert the two, which would defeat the whole point."""
    assert P.LLM_PROBE_INTERVAL_CREDENTIALED_S >= P.LLM_PROBE_INTERVAL_S


def test_the_credentialed_interval_is_clamped_up_not_silently_accepted(monkeypatch):
    """An operator setting a shorter credentialed interval gets the plain one."""
    monkeypatch.setenv("LLM_PROBE_INTERVAL_S", "10")
    monkeypatch.setenv("LLM_PROBE_INTERVAL_CREDENTIALED_S", "1")
    reloaded = importlib.reload(P)
    try:
        assert reloaded.LLM_PROBE_INTERVAL_CREDENTIALED_S == 10.0
    finally:
        monkeypatch.undo()
        importlib.reload(reloaded)


# ── F-LINGER: the probe map cannot outlive the pool ──────────────────────────
# NAMED FAILURE (adversarial, Gemini Pro 3.1): the probe map was seeded from the
# previous cache, so a key that left LLM_BACKENDS was never updated and never
# removed — it kept its last verdict forever. Not reachable in production today
# (LLM_BACKENDS is bound once at import and never mutated), which is exactly why
# it needs pinning here: nothing else would notice if that stopped being true.
# E2E cannot reach it without mutating module state mid-daemon.
# MUTATION: `{b: _llm_status_cache.get(b, "unknown") for b in LLM_BACKENDS}`
# back to `dict(_llm_status_cache)` must break this.

def test_the_probe_map_is_keyed_off_the_pool_not_the_previous_cache(monkeypatch):
    monkeypatch.setattr(P, "_llm_status_cache", {"http://departed:1": "ok"})
    monkeypatch.setattr(P, "LLM_BACKENDS", ["http://present:2"])
    fresh = P._fresh_probe_map()
    assert "http://departed:1" not in fresh
    assert fresh == {"http://present:2": "unknown"}


def test_a_backend_inside_its_interval_carries_its_verdict_forward(monkeypatch):
    """Skipping a probe must not read as 'never probed'."""
    monkeypatch.setattr(P, "_llm_status_cache", {"http://present:2": "ok"})
    monkeypatch.setattr(P, "LLM_BACKENDS", ["http://present:2"])
    fresh = P._fresh_probe_map()
    assert fresh["http://present:2"] == "ok"
