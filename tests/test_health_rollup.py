"""Tests for health roll-up: a stalled NREM cycle stops turning the top /health status to degraded.

fact:2996 ruling 3: exactly the two NREM stall reasons (CONSOLIDATION_CYCLE_TYPES:
"insight", "fact_consolidation") stop turning the top /health status to degraded.
Everything else rolls up as today.

Covers:
  - Part A: real route handler cases (A0 control, A1 insight, A2 fact_consolidation,
    A3 both stalled, A4 auth-on anonymous slimming with stall, A5 stall with no
    dream slots, A6 stall plus warning).
  - Part B: overall_status isolation tests killing specific adversarial mutants
    (F0 lone exempt stall reads ok, F1 prefix match, F2 down not exempt, F3 name gate, F4 bare stalled,
    F5 folds_attempted_24h, F6 literal pins, F7 stall + warning, F8 stall + other dep, F9 non-string reason).
"""
import asyncio
import importlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


class _HealthProbeResp:
    status = 200


class _HealthProbeCm:
    async def __aenter__(self):
        return _HealthProbeResp()

    async def __aexit__(self, *a):
        return False


class _HealthProbeSession:
    """No real network — every probe (/health, /v1/models) just reports 200."""
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
    req.app = {}  # "proxy" and "coordinator" patched in by callers
    return req


def _load_gateway(monkeypatch, agent_tokens: str = "", backends_json: list | None = None):
    """Reloads coordinator FIRST then hive_mind_proxy with clean environment."""
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    if backends_json is None:
        backends_json = [{"url": "http://a:5000", "private_ok": True}]
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(backends_json))
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    if agent_tokens:
        monkeypatch.setenv("AGENT_TOKENS", agent_tokens)
    else:
        monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    return g


class _FakeCoordinator:
    """Fake coordinator supplying cached snapshot methods for _coordinator_health_keys
    and _build_health_checks without network or database calls."""
    def __init__(self, consolidation_dict=None, dep_snap=None):
        self._consolidation = consolidation_dict or {}
        self._dep_snap = dep_snap or {}
        self.pgvector_version = None
        self.hnsw_iterative_scan = False
        self._axis_registry_read_failures = 0
        self._registry_census_failures = 0

    def consolidation_health(self):
        return self._consolidation

    def dependency_snapshot(self):
        return self._dep_snap


def _record_transcript(case: str, status: str, http: int, nrem_state: str | None,
                       nrem_reason: str | None, tmp_path=None) -> None:
    entry = {
        "case": case,
        "status": status,
        "http": http,
        "nrem_state": nrem_state,
        "nrem_reason": nrem_reason,
    }
    env_path = os.environ.get("HEALTH_ROLLUP_TRANSCRIPT")
    if env_path:
        out_file = Path(env_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        data = []
        if out_file.exists():
            try:
                loaded = json.loads(out_file.read_text())
                if isinstance(loaded, list):
                    data = loaded
            except Exception:
                data = []
        for idx, e in enumerate(data):
            if isinstance(e, dict) and e.get("case") == case:
                data[idx] = entry
                break
        else:
            data.append(entry)
        out_file.write_text(json.dumps(data, indent=2))
    elif tmp_path is not None:
        out_file = tmp_path / "health_rollup_transcript.json"
        out_file.write_text(json.dumps([entry], indent=2))


# ══════════════════════════════════════════════════════════════════════════════
# Part A: Real Route Handler Cases (assert reason literal -> top status -> http)
# ══════════════════════════════════════════════════════════════════════════════

def test_handler_a0_control(monkeypatch, tmp_path):
    """A0 control: stalled: False -> nrem ok, top ok, 200. Asserted first."""
    g = _load_gateway(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    g._daemon_healthy = True
    g._rem_healthy = True
    g._health_cache["checks"] = None

    fake = _FakeCoordinator(consolidation_dict={"stalled": False, "fresh": True})
    req = _health_request()
    req.app = {"proxy": proxy, "coordinator": fake}

    resp = asyncio.run(g.handle_health(req))
    body = json.loads(resp.body.decode())

    assert body["dependencies"]["nrem_daemon"]["reason"] is None
    assert body["dependencies"]["nrem_daemon"]["state"] == "ok"
    assert body["status"] == "ok"
    assert resp.status == 200

    _record_transcript("A0", body["status"], resp.status,
                       body["dependencies"]["nrem_daemon"]["state"],
                       body["dependencies"]["nrem_daemon"]["reason"],
                       tmp_path=tmp_path)


def test_handler_a1_insight_stall(monkeypatch, tmp_path):
    """A1 ["insight"] -> reason stalled:insight, top ok, 200."""
    g = _load_gateway(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    g._daemon_healthy = True
    g._rem_healthy = True
    g._health_cache["checks"] = None

    fake = _FakeCoordinator(consolidation_dict={"stalled": True, "stalled_types": ["insight"], "fresh": True})
    req = _health_request()
    req.app = {"proxy": proxy, "coordinator": fake}

    resp = asyncio.run(g.handle_health(req))
    body = json.loads(resp.body.decode())

    assert body["dependencies"]["nrem_daemon"]["reason"] == "stalled:insight"
    assert body["dependencies"]["nrem_daemon"]["state"] == "degraded"
    assert body["status"] == "ok"
    assert resp.status == 200

    _record_transcript("A1", body["status"], resp.status,
                       body["dependencies"]["nrem_daemon"]["state"],
                       body["dependencies"]["nrem_daemon"]["reason"],
                       tmp_path=tmp_path)


def test_handler_a2_fact_consolidation_stall(monkeypatch, tmp_path):
    """A2 ["fact_consolidation"] -> reason stalled:fact_consolidation, top ok, 200."""
    g = _load_gateway(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    g._daemon_healthy = True
    g._rem_healthy = True
    g._health_cache["checks"] = None

    fake = _FakeCoordinator(consolidation_dict={"stalled": True, "stalled_types": ["fact_consolidation"], "fresh": True})
    req = _health_request()
    req.app = {"proxy": proxy, "coordinator": fake}

    resp = asyncio.run(g.handle_health(req))
    body = json.loads(resp.body.decode())

    assert body["dependencies"]["nrem_daemon"]["reason"] == "stalled:fact_consolidation"
    assert body["dependencies"]["nrem_daemon"]["state"] == "degraded"
    assert body["status"] == "ok"
    assert resp.status == 200

    _record_transcript("A2", body["status"], resp.status,
                       body["dependencies"]["nrem_daemon"]["state"],
                       body["dependencies"]["nrem_daemon"]["reason"],
                       tmp_path=tmp_path)


def test_handler_a3_both_stalled(monkeypatch, tmp_path):
    """A3 ["insight", "fact_consolidation"] -> reason stalled:insight,fact_consolidation, top ok, 200."""
    g = _load_gateway(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    g._daemon_healthy = True
    g._rem_healthy = True
    g._health_cache["checks"] = None

    fake = _FakeCoordinator(consolidation_dict={"stalled": True, "stalled_types": ["insight", "fact_consolidation"], "fresh": True})
    req = _health_request()
    req.app = {"proxy": proxy, "coordinator": fake}

    resp = asyncio.run(g.handle_health(req))
    body = json.loads(resp.body.decode())

    assert body["dependencies"]["nrem_daemon"]["reason"] == "stalled:insight,fact_consolidation"
    assert body["dependencies"]["nrem_daemon"]["state"] == "degraded"
    assert body["status"] == "ok"
    assert resp.status == 200

    _record_transcript("A3", body["status"], resp.status,
                       body["dependencies"]["nrem_daemon"]["state"],
                       body["dependencies"]["nrem_daemon"]["reason"],
                       tmp_path=tmp_path)


def test_handler_a4_auth_on_anonymous(monkeypatch, tmp_path):
    """A4 auth-on anonymous (_load_gateway(monkeypatch, agent_tokens="claude:tok_x"), no header)
    with A1's stall -> body keys exactly {status, version, api_version}, status == "ok", 200."""
    g = _load_gateway(monkeypatch, agent_tokens="claude:tok_x")
    assert g.AUTH_CONFIGURED_AT_STARTUP is True

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    g._daemon_healthy = True
    g._rem_healthy = True
    g._health_cache["checks"] = None

    fake = _FakeCoordinator(consolidation_dict={"stalled": True, "stalled_types": ["insight"], "fresh": True})
    req = _health_request()  # No Authorization header
    req.app = {"proxy": proxy, "coordinator": fake}

    resp = asyncio.run(g.handle_health(req))
    body = json.loads(resp.body.decode())

    assert set(body.keys()) == {"status", "version", "api_version"}
    assert body["status"] == "ok"
    assert resp.status == 200

    # Underlying nrem state & reason from cache
    cached_nrem = (g._health_cache["checks"].get("dependencies") or {}).get("nrem_daemon")
    assert cached_nrem["reason"] == "stalled:insight"
    assert cached_nrem["state"] == "degraded"

    _record_transcript("A4", body["status"], resp.status,
                       cached_nrem["state"], cached_nrem["reason"],
                       tmp_path=tmp_path)


def test_handler_a5_stall_with_no_dream_slots(monkeypatch, tmp_path):
    """A5 stall with no backend counting toward dream slots (LLM_BACKENDS_JSON entry
    without private_ok) -> top status is degraded by co-degradation (rem_daemon carries
    the slots reason too). F1 is the test that kills the prefix-match mutant."""
    g = _load_gateway(monkeypatch, backends_json=[{"url": "http://a:5000"}])
    assert g._dream_slots_impossible_reason() is not None

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    g._daemon_healthy = True
    g._rem_healthy = True
    g._health_cache["checks"] = None

    fake = _FakeCoordinator(consolidation_dict={"stalled": True, "stalled_types": ["insight"], "fresh": True})
    req = _health_request()
    req.app = {"proxy": proxy, "coordinator": fake}

    resp = asyncio.run(g.handle_health(req))
    body = json.loads(resp.body.decode())

    assert body["dependencies"]["nrem_daemon"]["reason"].startswith("stalled:insight; ")
    assert body["dependencies"]["nrem_daemon"]["state"] == "degraded"
    assert body["dependencies"]["rem_daemon"]["state"] == "degraded"
    assert body["status"] == "degraded"
    assert resp.status == 200

    _record_transcript("A5", body["status"], resp.status,
                       body["dependencies"]["nrem_daemon"]["state"],
                       body["dependencies"]["nrem_daemon"]["reason"],
                       tmp_path=tmp_path)


def test_handler_a6_stall_plus_warning(monkeypatch, tmp_path):
    """A6 A1's stall plus a warning -> reason stalled:insight, top degraded, 200."""
    g = _load_gateway(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    g._daemon_healthy = True
    g._rem_healthy = True
    g._health_cache["checks"] = None

    # Simplest warning without network stub: load-shed 503 counter
    monkeypatch.setattr(g, "_gateway_shed_rate", lambda: 1)

    fake = _FakeCoordinator(consolidation_dict={"stalled": True, "stalled_types": ["insight"], "fresh": True})
    req = _health_request()
    req.app = {"proxy": proxy, "coordinator": fake}

    resp = asyncio.run(g.handle_health(req))
    body = json.loads(resp.body.decode())

    assert body["dependencies"]["nrem_daemon"]["reason"] == "stalled:insight"
    assert body["dependencies"]["nrem_daemon"]["state"] == "degraded"
    assert len(body["warnings"]) > 0
    assert body["status"] == "degraded"
    assert resp.status == 200

    _record_transcript("A6", body["status"], resp.status,
                       body["dependencies"]["nrem_daemon"]["state"],
                       body["dependencies"]["nrem_daemon"]["reason"],
                       tmp_path=tmp_path)


# ══════════════════════════════════════════════════════════════════════════════
# Part B: overall_status Isolation Tests (adversarial mutant killers)
# ══════════════════════════════════════════════════════════════════════════════

def test_f0_lone_exempt_stall_reads_ok(monkeypatch):
    """Positive isolation case: for each of the three exempt reasons,
    overall_status({"nrem_daemon": {"state": "degraded", "reason": r}}, []) == 'ok'."""
    g = _load_gateway(monkeypatch)
    for r in (
        "stalled:insight",
        "stalled:fact_consolidation",
        "stalled:insight,fact_consolidation",
    ):
        assert g.overall_status({"nrem_daemon": {"state": "degraded", "reason": r}}, []) == "ok"


def test_f1_prefix_match_not_exempt(monkeypatch):
    """Kills mutant: prefix match in exempt check.
    nrem_daemon degraded with reason 'stalled:insight; no backend …' -> degraded."""
    g = _load_gateway(monkeypatch)
    deps = {
        "nrem_daemon": {"state": "degraded", "reason": "stalled:insight; no backend counts toward dream slots"},
        "embedder": {"state": "ok", "reason": None},
    }
    assert g.overall_status(deps, []) == "degraded"


def test_f2_down_not_exempt(monkeypatch):
    """Kills mutant: dropping state == _STATE_DEGRADED check.
    nrem_daemon down with reason 'stalled:insight' -> down."""
    g = _load_gateway(monkeypatch)
    deps = {
        "nrem_daemon": {"state": "down", "reason": "stalled:insight"},
        "embedder": {"state": "ok", "reason": None},
    }
    assert g.overall_status(deps, []) == "down"


def test_f3_name_gate(monkeypatch):
    """Kills mutant: dropping name == 'nrem_daemon' check.
    Guards the name check, not a reachable live state:
    rem_daemon degraded with reason 'stalled:insight' -> degraded."""
    g = _load_gateway(monkeypatch)
    deps = {
        "rem_daemon": {"state": "degraded", "reason": "stalled:insight"},
        "embedder": {"state": "ok", "reason": None},
    }
    assert g.overall_status(deps, []) == "degraded"


def test_f4_bare_stalled_not_exempt(monkeypatch):
    """Kills mutant: including the empty subset in exempt set.
    nrem_daemon degraded with bare reason 'stalled' -> degraded."""
    g = _load_gateway(monkeypatch)
    deps = {
        "nrem_daemon": {"state": "degraded", "reason": "stalled"},
        "embedder": {"state": "ok", "reason": None},
    }
    assert g.overall_status(deps, []) == "degraded"


def test_f5_folds_attempted_zero_succeeded_not_exempt(monkeypatch):
    """Kills mutant: exempting any degraded nrem_daemon.
    This branch is unreachable through the compact snapshot today; isolation only:
    folds_attempted_24h:9 succeeded:0 -> degraded."""
    g = _load_gateway(monkeypatch)
    deps = {
        "nrem_daemon": {"state": "degraded", "reason": "folds_attempted_24h:9 succeeded:0"},
        "embedder": {"state": "ok", "reason": None},
    }
    assert g.overall_status(deps, []) == "degraded"


def test_f6_literal_pins(monkeypatch):
    """fact:1309: pin helper literals and exact exempt set."""
    g = _load_gateway(monkeypatch)
    assert g._nrem_stall_reason([]) == "stalled"
    assert g._nrem_stall_reason(["insight"]) == "stalled:insight"
    assert g._nrem_stall_reason(["fact_consolidation"]) == "stalled:fact_consolidation"
    assert g._nrem_stall_reason(["insight", "fact_consolidation"]) == "stalled:insight,fact_consolidation"
    assert g._NREM_STALL_REASONS_EXEMPT_FROM_ROLLUP == {
        "stalled:insight",
        "stalled:fact_consolidation",
        "stalled:insight,fact_consolidation",
    }


def test_f7_exempt_stall_plus_warning_is_degraded(monkeypatch):
    """Kills mutant: ignoring warnings when an exempt stall is present.
    Exempt stall + one warning -> degraded."""
    g = _load_gateway(monkeypatch)
    deps = {
        "nrem_daemon": {"state": "degraded", "reason": "stalled:insight"},
        "embedder": {"state": "ok", "reason": None},
    }
    warnings = [{"key": "test_warn", "limit": 10, "observed": 20, "unit": "ms"}]
    assert g.overall_status(deps, warnings) == "degraded"


def test_f8_exempt_stall_with_other_degraded_or_down(monkeypatch):
    """Kills mutant: returning early on exempt stall.
    Exempt stall + another dependency degraded -> degraded;
    exempt stall + another dependency down -> down."""
    g = _load_gateway(monkeypatch)
    deps_degraded = {
        "nrem_daemon": {"state": "degraded", "reason": "stalled:insight"},
        "postgres": {"state": "degraded", "reason": "read_failures:1"},
    }
    assert g.overall_status(deps_degraded, []) == "degraded"

    deps_down = {
        "nrem_daemon": {"state": "degraded", "reason": "stalled:insight"},
        "postgres": {"state": "down", "reason": "connection refused"},
    }
    assert g.overall_status(deps_down, []) == "down"


def test_f9_non_string_reason_is_not_exempt_and_does_not_raise(monkeypatch):
    """Kills mutant: unhandled non-string reason crashing membership test in frozenset.
    nrem_daemon degraded with reason ['stalled:insight'] -> degraded without raising."""
    g = _load_gateway(monkeypatch)
    deps = {
        "nrem_daemon": {"state": "degraded", "reason": ["stalled:insight"]},
    }
    assert g.overall_status(deps, []) == "degraded"
