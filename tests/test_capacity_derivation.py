"""R0-I (decision:1424): the capacity-derivation instrument in hive_mind_
proxy.py. Read-and-report ONLY -- no request is ever limited, queued,
rejected or resized by anything in this file's target module. Every test
below exercises the derivation math, the trigger logic, /health surfacing,
and fail-open behaviour with mocked/synthetic inputs -- no live gateway,
Neo4j, or Postgres required (matches the rest of this suite).

conftest.py's autouse `_capacity_log_path_never_touches_real_home` fixture
already points CAPACITY_LOG_PATH at a per-test tmp_path; tests that need a
KNOWN path still set it explicitly via monkeypatch + reload, matching
tests/test_health_anonymous_slimming.py's proven `_load_gateway` pattern.
"""
import asyncio
import importlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def _load_gateway(monkeypatch, capacity_log_path, agent_tokens: str = ""):
    """Same reload order as test_health_anonymous_slimming.py's
    _load_gateway: coordinator first (so AUTH_CONFIGURED_AT_STARTUP is
    correct), then hive_mind_proxy -- plus CAPACITY_LOG_PATH set BEFORE the
    hive_mind_proxy reload, since that module reads it at import time."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    monkeypatch.setenv("CAPACITY_LOG_PATH", str(capacity_log_path))
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


# The shape _probe_capability() actually returns (capability_snapshot()'s
# format) -- reused across tests as a realistic fixture rather than an
# invented shorthand.
def _capability(reranker_chars_s=1000.0, reranker_projected_s=10.0,
                embedder_chars_s=2000.0, embedder_projected_s=5.0):
    return {
        "status": "ok",
        "probed_at": "2026-08-21T00:00:00+00:00",
        "reranker": {
            "throughput_chars_s": reranker_chars_s,
            "projected_full_payload_s": reranker_projected_s,
        },
        "embedder": {
            "throughput_chars_s": embedder_chars_s,
            "projected_full_payload_s": embedder_projected_s,
        },
    }


# ── Derivation math: values, not just equalities (fact:1309) ────────────────

def test_client_ceiling_matches_memory_bridge_formula_and_a_concrete_value(monkeypatch, tmp_path):
    """Parity with the client formula AND a concrete expected number --
    reranker 10.0 + embedder 5.0 = 15.0 projected, * SAFETY_FACTOR 1.5 =
    22.5, + OVERHEAD_S 15 = 37.5, clamped into [FLOOR 30, MAX 300] -> 37.5."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    cap = _capability(reranker_projected_s=10.0, embedder_projected_s=5.0)

    ceiling = g._capacity_client_ceiling_s(cap)
    assert ceiling == 37.5

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "memory_bridge_parity",
        os.path.join(os.path.dirname(__file__), "..", "shared-memory-skill",
                     "shared-memory", "scripts", "memory_bridge.py"),
    )
    memory_bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(memory_bridge)
    assert ceiling == memory_bridge.search_ceiling(cap), (
        "server-side mirror must agree with the client's own search_ceiling() "
        "for the identical capability input -- this is the guard against the "
        "two copies drifting apart"
    )


def test_queue_bound_concrete_value(monkeypatch, tmp_path):
    """s_mean=10.0, client_ceiling=37.5 -> floor(37.5/10.0) - 1 = 3 - 1 = 2."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    assert g._capacity_queue_bound(10.0, 37.5) == 2


def test_queue_bound_floors_at_zero_not_negative(monkeypatch, tmp_path):
    """A slow backend where a single search already exceeds the ceiling must
    report 0 (no room), never a negative queue depth."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    assert g._capacity_queue_bound(100.0, 37.5) == 0


def test_recommended_mem_limit_concrete_value(monkeypatch, tmp_path):
    """16 GiB host, all-default allowances (Neo4j fallback 8G + PG 4G +
    embedder 2G + gateway 512M + OS 1G = 15.5G) -> 0.5 GiB left over,
    exactly 536870912 bytes."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    total = 16 * 1024 ** 3
    assert g._capacity_recommended_mem_limit_bytes(total) == 536870912


def test_recommended_mem_limit_respects_operator_neo4j_env(monkeypatch, tmp_path):
    """NEO4J_HEAP_MAX + NEO4J_PAGECACHE, when BOTH set, replace the 8G
    compose-cap fallback -- a tighter operator config frees more headroom
    for the reranker recommendation."""
    monkeypatch.setenv("NEO4J_HEAP_MAX", "1G")
    monkeypatch.setenv("NEO4J_PAGECACHE", "1G")
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    total = 16 * 1024 ** 3
    # 2G neo4j + 4G pg + 2G embedder + 512M gateway + 1G os = 9.5G subtracted
    expected = total - (2 * 1024**3 + 4 * 1024**3 + 2 * 1024**3 +
                        512 * 1024**2 + 1 * 1024**3)
    assert g._capacity_recommended_mem_limit_bytes(total) == expected


def test_recommended_mem_limit_floors_at_zero(monkeypatch, tmp_path):
    """A tiny host where allowances exceed MemTotal reports 0, never negative."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    assert g._capacity_recommended_mem_limit_bytes(1 * 1024 ** 3) == 0


def test_mem_size_parser_handles_compose_style_and_bare_bytes(monkeypatch, tmp_path):
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    assert g._parse_mem_size("8G") == 8 * 1024 ** 3
    assert g._parse_mem_size("512M") == 512 * 1024 ** 2
    assert g._parse_mem_size("2GiB") == 2 * 1024 ** 3
    assert g._parse_mem_size("1073741824") == 1073741824
    assert g._parse_mem_size(None) is None
    assert g._parse_mem_size("not-a-size") is None


# ── Trigger logic ────────────────────────────────────────────────────────────

def test_first_probe_with_no_prior_record_fires_fingerprint_mismatch(monkeypatch, tmp_path):
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    cap = _capability()

    asyncio.run(g._maybe_derive_capacity(cap))

    records = g._read_capacity_records_sync(g.CAPACITY_LOG_PATH)
    assert len(records) == 1
    assert records[0]["trigger"] == "gateway_start_fingerprint_mismatch"
    assert records[0]["derived"]["s_mean_s"] == 10.0


def test_mismatch_log_line_ends_with_postflight_recommendation(monkeypatch, tmp_path, caplog):
    # fact:1425 A2: the basis-changed warning must tell the operator to re-run
    # postflight, so every hardware-era change produces a fresh verification.
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    with caplog.at_level("WARNING"):
        asyncio.run(g._maybe_derive_capacity(_capability()))

    basis_lines = [r.getMessage() for r in caplog.records
                   if "capacity basis changed" in r.getMessage()]
    assert len(basis_lines) == 1
    assert basis_lines[0].endswith(
        "re-run postflight to verify and re-baseline on this hardware: "
        "bash shared-memory/scripts/postflight.sh"
    )


def test_identical_fingerprint_on_second_cycle_does_not_fire(monkeypatch, tmp_path):
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    cap = _capability()

    asyncio.run(g._maybe_derive_capacity(cap))
    first_count = len(g._read_capacity_records_sync(g.CAPACITY_LOG_PATH))
    asyncio.run(g._maybe_derive_capacity(cap))
    second_count = len(g._read_capacity_records_sync(g.CAPACITY_LOG_PATH))

    assert first_count == 1
    assert second_count == 1, "an unchanged fingerprint and in-band throughput must not re-derive"


def test_drift_outside_band_fires_probe_drift(monkeypatch, tmp_path):
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    baseline = _capability(reranker_chars_s=1000.0)
    asyncio.run(g._maybe_derive_capacity(baseline))

    drifted = _capability(reranker_chars_s=2001.0)  # ratio 2.001 -- just outside x2
    asyncio.run(g._maybe_derive_capacity(drifted))

    records = g._read_capacity_records_sync(g.CAPACITY_LOG_PATH)
    assert len(records) == 2
    assert records[-1]["trigger"] == "probe_drift"


def test_drift_at_exactly_the_band_factor_does_not_fire(monkeypatch, tmp_path):
    """Boundary test: exactly 2x the basis reading is INSIDE the band."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    baseline = _capability(reranker_chars_s=1000.0)
    asyncio.run(g._maybe_derive_capacity(baseline))

    at_band = _capability(reranker_chars_s=2000.0)  # ratio exactly 2.0
    asyncio.run(g._maybe_derive_capacity(at_band))

    records = g._read_capacity_records_sync(g.CAPACITY_LOG_PATH)
    assert len(records) == 1, "exactly 2x must stay inside the band -- no re-derivation"


def test_drift_just_inside_the_band_does_not_fire(monkeypatch, tmp_path):
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    baseline = _capability(reranker_chars_s=1000.0)
    asyncio.run(g._maybe_derive_capacity(baseline))

    just_inside = _capability(reranker_chars_s=1999.0)  # ratio 1.999
    asyncio.run(g._maybe_derive_capacity(just_inside))

    records = g._read_capacity_records_sync(g.CAPACITY_LOG_PATH)
    assert len(records) == 1


def test_drift_just_below_the_lower_band_edge_fires(monkeypatch, tmp_path):
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    baseline = _capability(reranker_chars_s=1000.0)
    asyncio.run(g._maybe_derive_capacity(baseline))

    just_below = _capability(reranker_chars_s=499.0)  # ratio 0.499 -- below 1/2
    asyncio.run(g._maybe_derive_capacity(just_below))

    records = g._read_capacity_records_sync(g.CAPACITY_LOG_PATH)
    assert len(records) == 2
    assert records[-1]["trigger"] == "probe_drift"


def test_config_change_fires_on_a_later_cycle(monkeypatch, tmp_path):
    """encoder_config differing from the LOG's last record on a non-first
    cycle must re-derive, even with an unchanged probe reading. Exercises
    the mechanism directly via a patched _encoder_config_fingerprint rather
    than an env var: RERANK_MAX_DOC_CHARS et al. are read once at
    dream_telemetry import time, so within one process they cannot actually
    change without a reload -- see _maybe_derive_capacity's own comment on
    why config_change still checks every cycle (a differently-configured
    process may have written the last record)."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    cap = _capability()
    asyncio.run(g._maybe_derive_capacity(cap))

    changed = dict(g._encoder_config_fingerprint(), rerank_max_doc_chars=999)
    monkeypatch.setattr(g, "_encoder_config_fingerprint", lambda: changed)
    asyncio.run(g._maybe_derive_capacity(cap))

    records = g._read_capacity_records_sync(g.CAPACITY_LOG_PATH)
    assert len(records) == 2
    assert records[-1]["trigger"] == "config_change"


def test_hardware_change_across_a_restart_fires_on_first_probe(monkeypatch, tmp_path):
    """Simulates a restart onto different hardware: a record already exists
    on disk (from a 'prior process'), but the in-process
    _capacity_first_probe_done flag is fresh -- this must compare against
    the STORED fingerprint, not just skip because a record exists."""
    log_path = tmp_path / "cap.jsonl"
    g = _load_gateway(monkeypatch, log_path)
    cap = _capability()
    asyncio.run(g._maybe_derive_capacity(cap))
    old_mem = g._read_capacity_records_sync(log_path)[0]["fingerprint"]["hardware"]["mem_total_bytes"]

    # Reload again (fresh process semantics: _capacity_first_probe_done reset),
    # but force a different hardware fingerprint by monkeypatching the probe.
    g2 = _load_gateway(monkeypatch, log_path)
    monkeypatch.setattr(g2, "_hardware_fingerprint",
                         lambda: {"nproc": 4, "mem_total_bytes": (old_mem or 0) + 1,
                                   "gpu_present": False})
    asyncio.run(g2._maybe_derive_capacity(cap))

    records = g2._read_capacity_records_sync(log_path)
    assert len(records) == 2
    assert records[-1]["trigger"] == "gateway_start_fingerprint_mismatch"


# ── /health surfacing ────────────────────────────────────────────────────────

class _HealthProbeResp:
    status = 200


class _HealthProbeCm:
    async def __aenter__(self):
        return _HealthProbeResp()

    async def __aexit__(self, *a):
        return False


class _HealthProbeSession:
    def get(self, url, timeout=None):
        return _HealthProbeCm()


def test_capacity_key_present_authenticated_absent_anonymous(monkeypatch, tmp_path):
    """Additive top-level key: present (possibly None) for an authenticated
    caller, absent from the anonymous slim shape -- the anonymous contract
    stays exactly {status, version, api_version} (fact:1314-style, S-10)."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl", agent_tokens="claude:tok_cap_test")
    assert g.AUTH_CONFIGURED_AT_STARTUP is True

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()

    class _Req(dict):
        pass

    anon_req = _Req()
    anon_req.headers = {}
    anon_req.app = {"proxy": proxy}
    anon_body = json.loads(asyncio.run(g.handle_health(anon_req)).body.decode())
    assert set(anon_body.keys()) == {"status", "version", "api_version"}
    assert "capacity" not in anon_body

    auth_req = _Req()
    auth_req.headers = {"Authorization": "Bearer tok_cap_test"}
    auth_req.app = {"proxy": proxy}
    auth_body = json.loads(asyncio.run(g.handle_health(auth_req)).body.decode())
    assert "capacity" in auth_body
    assert auth_body["capacity"] is None  # nothing derived yet in this process/log


def test_capacity_key_reflects_a_stored_record(monkeypatch, tmp_path):
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl", agent_tokens="claude:tok_cap_test2")
    cap = _capability()
    asyncio.run(g._maybe_derive_capacity(cap))

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()

    class _Req(dict):
        pass

    req = _Req()
    req.headers = {"Authorization": "Bearer tok_cap_test2"}
    req.app = {"proxy": proxy}
    body = json.loads(asyncio.run(g.handle_health(req)).body.decode())
    assert body["capacity"]["trigger"] == "gateway_start_fingerprint_mismatch"
    assert body["capacity"]["derived"]["s_mean_s"] == 10.0


# ── Fail-open ─────────────────────────────────────────────────────────────────

def test_missing_proc_meminfo_yields_null_not_an_exception(monkeypatch, tmp_path):
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    real_open = open

    def _boom_open(path, *a, **kw):
        if path == "/proc/meminfo":
            raise OSError("no such file")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", _boom_open)
    hw = g._hardware_fingerprint()
    assert hw["mem_total_bytes"] is None
    assert hw["nproc"] is not None  # unaffected by the meminfo failure


def test_probe_none_still_derives_a_record_with_nulls(monkeypatch, tmp_path):
    """capability=None (a probe that never landed, or failed entirely) must
    still produce a storable record -- s_mean/queue_bound/ceiling degrade to
    None/fallback rather than raising."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    asyncio.run(g._maybe_derive_capacity(None))

    records = g._read_capacity_records_sync(g.CAPACITY_LOG_PATH)
    assert len(records) == 1
    assert records[0]["derived"]["s_mean_s"] is None
    assert records[0]["derived"]["queue_bound"] is None
    assert records[0]["derived"]["client_ceiling_s"] == g.CAPACITY_SEARCH_TIMEOUT_FALLBACK_S


def test_gpu_probe_exception_never_propagates(monkeypatch, tmp_path):
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    import gpu_load

    def _boom():
        raise RuntimeError("nvtop exploded")

    monkeypatch.setattr(gpu_load, "gpu_probe_available", _boom)
    hw = g._hardware_fingerprint()
    assert hw["gpu_present"] is False


def test_corrupt_log_line_is_skipped_not_fatal(monkeypatch, tmp_path):
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    path = tmp_path / "cap.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"trigger": "manual", "fingerprint": {}, "derived": {}, "probe": {}}\n'
                     'not-json-at-all\n')
    records = g._read_capacity_records_sync(str(path))
    assert len(records) == 1
    assert records[0]["trigger"] == "manual"


def test_maybe_derive_capacity_never_raises_on_internal_failure(monkeypatch, tmp_path):
    """Even a fully broken fingerprint function must not take the probe
    daemon down with it -- _maybe_derive_capacity wraps its own body."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    def _boom():
        raise RuntimeError("fingerprint exploded")

    monkeypatch.setattr(g, "_capacity_fingerprint", _boom)
    asyncio.run(g._maybe_derive_capacity(_capability()))  # must not raise


# ── Pruning ──────────────────────────────────────────────────────────────────

def test_log_prunes_to_max_records(monkeypatch, tmp_path):
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    monkeypatch.setattr(g, "CAPACITY_LOG_MAX_RECORDS", 3)

    for i in range(5):
        rec = g._build_capacity_record(_capability(), g._capacity_fingerprint(), "manual")
        asyncio.run(g._append_capacity_record(rec))

    records = g._read_capacity_records_sync(g.CAPACITY_LOG_PATH)
    assert len(records) == 3


# ── Mutation-check target ────────────────────────────────────────────────────
# Inverting the comparison in _capacity_drift_outside_band -- changing
# `ratio > band_factor or ratio < (1.0 / band_factor)` to
# `ratio >= band_factor or ratio <= (1.0 / band_factor)` -- makes
# test_drift_at_exactly_the_band_factor_does_not_fire fail (a record is
# written where the test asserts none is). Confirmed by hand: with `>=`
# substituted, that test's `len(records) == 1` assertion fails with 2.
