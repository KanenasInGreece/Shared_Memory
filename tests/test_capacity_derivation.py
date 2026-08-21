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
import stat
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
                embedder_chars_s=2000.0, embedder_projected_s=5.0,
                reranker_status="ok", embedder_status="ok"):
    return {
        "status": "ok",
        "probed_at": "2026-08-21T00:00:00+00:00",
        "reranker": {
            "status": reranker_status,
            "throughput_chars_s": reranker_chars_s,
            "projected_full_payload_s": reranker_projected_s,
        },
        "embedder": {
            "status": embedder_status,
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
    # fact:1309 -- an equality between two expressions is HALF a guard: the
    # pair can drift together to a wrong value. Pin the VALUE one side must
    # produce: (10.0 + 5.0 projected) * 1.5 safety + 15 overhead = 37.5,
    # inside the [30, 300] clamp.
    assert ceiling == 37.5


def test_capacity_tolerable_wait_default_is_30s(monkeypatch, tmp_path):
    """H1: CAPACITY_TOLERABLE_WAIT_S is the value queue_bound is now
    measured against (not client_ceiling_s) -- pin the default separately
    from the queue_bound tests below (fact:1309: an equality between two
    expressions is half a guard; pin the VALUE on at least one side)."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    assert g.CAPACITY_TOLERABLE_WAIT_S == 30.0


def test_queue_bound_concrete_value_s_mean_2(monkeypatch, tmp_path):
    """H1: floor(CAPACITY_TOLERABLE_WAIT_S / s_mean) = floor(30.0/2.0) = 15."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    assert g._capacity_queue_bound(2.0, 30.0) == 15


def test_queue_bound_concrete_value_s_mean_10(monkeypatch, tmp_path):
    """floor(30.0/10.0) = 3."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    assert g._capacity_queue_bound(10.0, 30.0) == 3


def test_queue_bound_cpu_floor_reality_s_mean_70(monkeypatch, tmp_path):
    """The CPU-floor reality: floor(30.0/70.0) = 0. A single search already
    exceeds the tolerable wait -- 0 genuinely means "no room" here, never
    negative, and single_search_exceeds_wait (M10) makes that reading
    explicit rather than ambiguous with "not yet measured" (None)."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    assert g._capacity_queue_bound(70.0, 30.0) == 0


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


def test_mem_size_parser_handles_k8s_binary_notation(monkeypatch, tmp_path):
    """M5: k8s-style 'Gi'/'Mi'/'Ki' (no trailing B) must parse too, not just
    the docker-compose 'G'/'M' and hand-typed 'GiB' forms above."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    assert g._parse_mem_size("8Gi") == 8 * 1024 ** 3
    assert g._parse_mem_size("512Mi") == 512 * 1024 ** 2
    assert g._parse_mem_size("4Ki") == 4 * 1024


def test_neo4j_allowance_warns_when_exactly_one_var_fails_to_parse(monkeypatch, tmp_path, caplog):
    """N3 (fix round 2): NEO4J_HEAP_MAX set-but-unparsable + NEO4J_PAGECACHE
    set-and-valid must still fall back to CAPACITY_NEO4J_FALLBACK_BYTES
    (behavior unchanged -- partial config can't be combined), but now names
    the rejected variable in a warning instead of failing silently. The
    valid sibling variable must NOT be named -- only the one that actually
    failed to parse."""
    monkeypatch.setenv("NEO4J_HEAP_MAX", "not-a-size")
    monkeypatch.setenv("NEO4J_PAGECACHE", "1G")
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    with caplog.at_level("WARNING"):
        result = g._capacity_neo4j_allowance_bytes()
    assert result == g.CAPACITY_NEO4J_FALLBACK_BYTES
    messages = [r.getMessage() for r in caplog.records]
    assert any("NEO4J_HEAP_MAX" in m for m in messages)
    assert not any("NEO4J_PAGECACHE" in m for m in messages)


def test_recommended_mem_limit_returns_null_on_unparsable_allowance(monkeypatch, tmp_path, caplog):
    """M5: an unparsable CAPACITY_*_BYTES value used to silently coerce to 0
    via `x or 0`, UNDER-subtracting and INFLATING the recommendation in the
    dangerous direction. It must now come back None (unknown beats wrong-
    in-the-dangerous-direction) with one warning naming the offender."""
    monkeypatch.setenv("CAPACITY_PG_MEM_ALLOWANCE_BYTES", "not-a-size")
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    with caplog.at_level("WARNING"):
        result = g._capacity_recommended_mem_limit_bytes(16 * 1024 ** 3)
    assert result is None
    assert any("CAPACITY_PG_MEM_ALLOWANCE_BYTES" in r.getMessage() for r in caplog.records)


# ── Trigger logic ────────────────────────────────────────────────────────────

def test_first_probe_with_no_prior_record_fires_first_derivation(monkeypatch, tmp_path):
    """M7: no prior record anywhere is a fresh baseline, not a mismatch --
    there is nothing to have mismatched AGAINST."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    cap = _capability()

    asyncio.run(g._maybe_derive_capacity(cap))

    records = g._read_capacity_records_sync(g.CAPACITY_LOG_PATH)
    assert len(records) == 1
    assert records[0]["trigger"] == "first_derivation"
    assert records[0]["derived"]["s_mean_s"] == 10.0


def test_first_derivation_logs_info_baseline_established_not_a_warning(monkeypatch, tmp_path, caplog):
    """M7: the very first record this log has ever held is informational --
    INFO level, "capacity baseline established", no re-run-postflight tail.
    Nothing has actually CHANGED yet (nothing prior to compare against), so
    this must not read as an alarm."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    with caplog.at_level("INFO"):
        asyncio.run(g._maybe_derive_capacity(_capability()))

    warn_lines = [r for r in caplog.records
                  if r.levelname == "WARNING" and "capacity basis changed" in r.getMessage()]
    assert warn_lines == [], "the first-ever derivation must not fire the WARNING path"

    info_lines = [r.getMessage() for r in caplog.records
                  if r.levelname == "INFO" and "capacity baseline established" in r.getMessage()]
    assert len(info_lines) == 1
    assert "re-run postflight" not in info_lines[0]


def test_mismatch_log_line_ends_with_postflight_recommendation(monkeypatch, tmp_path, caplog):
    # fact:1425 A2: the basis-changed warning must tell the operator to
    # re-run postflight, so every GENUINE hardware-era mismatch (not the
    # first-ever baseline, which is informational -- see the
    # first_derivation test above) produces a fresh verification.
    log_path = tmp_path / "cap.jsonl"
    g = _load_gateway(monkeypatch, log_path)
    cap = _capability()
    asyncio.run(g._maybe_derive_capacity(cap))  # first_derivation -- establishes a basis
    old_mem = g._read_capacity_records_sync(log_path)[0]["fingerprint"]["hardware"]["mem_total_bytes"]

    g2 = _load_gateway(monkeypatch, log_path)
    monkeypatch.setattr(g2, "_hardware_fingerprint",
                         lambda: {"nproc": 4, "mem_total_bytes": (old_mem or 0) + 1,
                                   "gpu_present": False})

    with caplog.at_level("WARNING"):
        asyncio.run(g2._maybe_derive_capacity(cap))

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


def test_drift_at_exactly_the_lower_band_edge_does_not_fire(monkeypatch, tmp_path):
    """L14: boundary test, lower edge -- exactly 0.5x (1/factor) the basis
    reading is INSIDE the band, mirroring the upper-edge test above."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    baseline = _capability(reranker_chars_s=1000.0)
    asyncio.run(g._maybe_derive_capacity(baseline))

    at_lower_band = _capability(reranker_chars_s=500.0)  # ratio exactly 0.5
    asyncio.run(g._maybe_derive_capacity(at_lower_band))

    records = g._read_capacity_records_sync(g.CAPACITY_LOG_PATH)
    assert len(records) == 1, "exactly 0.5x must stay inside the band -- no re-derivation"


def test_failed_status_probe_never_fires_drift_or_becomes_basis(monkeypatch, tmp_path):
    """H3: a not-ok reranker probe (fast HTTP error, fantasy throughput)
    must never fire probe_drift and must never become the new stored basis
    -- guarding only the CURRENT reading is not enough if a poisoned basis
    was already on disk; this exercises the "never fires" half directly."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    baseline = _capability(reranker_chars_s=1000.0)
    asyncio.run(g._maybe_derive_capacity(baseline))

    # Absurd throughput (would be WAY outside the x2 band) but status
    # "failing" -- must not fire, must not append, must not become basis.
    fantasy = _capability(reranker_chars_s=999999.0, reranker_status="failing")
    asyncio.run(g._maybe_derive_capacity(fantasy))

    records = g._read_capacity_records_sync(g.CAPACITY_LOG_PATH)
    assert len(records) == 1, "a not-ok probe must not fire drift or append a new basis record"
    assert records[0]["probe"]["reranker_chars_per_s"] == 1000.0, "the ok basis must be untouched"


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


# ── N1 (fix round 2): a bad first probe must not freeze the instrument ──────

def _write_raw_capacity_record(g, log_path, record: dict) -> None:
    """Write a hand-built record directly to the log, bypassing
    _maybe_derive_capacity/_build_capacity_record's own trigger decisions --
    simulates a basis that is already stored on disk (e.g. one written
    before this fix landed, or one N1(a) alone cannot retroactively repair)."""
    g._write_capacity_records_sync(str(log_path), [record])


def test_first_probe_not_ok_defers_then_second_healthy_cycle_derives(monkeypatch, tmp_path):
    """N1(a), test (i): a not-ok first-ever probe must write NOTHING --
    deferring the baseline rather than establishing an unusable one. Once a
    healthy probe lands on the very next cycle, exactly one record is
    written, trigger first_derivation -- the deferred cycle leaves nothing
    behind to distinguish it from a genuinely first cycle."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    asyncio.run(g._maybe_derive_capacity(_capability(reranker_status="failing")))
    assert g._read_capacity_records_sync(g.CAPACITY_LOG_PATH) == [], (
        "a not-ok first probe must not establish a not-ok baseline")

    asyncio.run(g._maybe_derive_capacity(_capability(reranker_status="ok")))
    records = g._read_capacity_records_sync(g.CAPACITY_LOG_PATH)
    assert len(records) == 1
    assert records[0]["trigger"] == "first_derivation"


def test_stored_not_ok_basis_recovers_on_next_healthy_probe(monkeypatch, tmp_path):
    """N1(b), test (ii): a stored basis with reranker_status != "ok" plus a
    now-healthy current probe must derive a NEW record via basis_recovery --
    the remedy for a basis that was already poisoned before this fix landed
    (N1(a) alone only stops NEW poisoning, it cannot repair old data)."""
    log_path = tmp_path / "cap.jsonl"
    g = _load_gateway(monkeypatch, log_path)
    stale = g._build_capacity_record(
        _capability(reranker_status="failing"), g._capacity_fingerprint(), "manual")
    _write_raw_capacity_record(g, log_path, stale)

    asyncio.run(g._maybe_derive_capacity(_capability(reranker_status="ok")))

    records = g._read_capacity_records_sync(log_path)
    assert len(records) == 2
    assert records[-1]["trigger"] == "basis_recovery"


def test_stored_basis_lacking_status_field_recovers_too(monkeypatch, tmp_path):
    """N1(b), test (iii): a legacy record predating the reranker_status
    field entirely (absent, not merely not "ok") must recover exactly the
    same way -- "not ok" and "unknown" are the same problem here: neither
    can be trusted as a drift-comparison basis."""
    log_path = tmp_path / "cap.jsonl"
    g = _load_gateway(monkeypatch, log_path)
    legacy = g._build_capacity_record(
        _capability(reranker_status="ok"), g._capacity_fingerprint(), "manual")
    del legacy["probe"]["reranker_status"]  # hand-build the pre-fix shape
    _write_raw_capacity_record(g, log_path, legacy)

    asyncio.run(g._maybe_derive_capacity(_capability(reranker_status="ok")))

    records = g._read_capacity_records_sync(log_path)
    assert len(records) == 2
    assert records[-1]["trigger"] == "basis_recovery"


def test_stored_not_ok_basis_stays_stuck_while_current_probe_also_not_ok(monkeypatch, tmp_path):
    """N1, test (iv): a stored not-ok basis and a STILL not-ok current probe
    fires nothing and writes nothing -- there is genuinely no healthy
    reading yet to recover from, so the instrument correctly stays quiet
    rather than writing another unusable record."""
    log_path = tmp_path / "cap.jsonl"
    g = _load_gateway(monkeypatch, log_path)
    stale = g._build_capacity_record(
        _capability(reranker_status="failing"), g._capacity_fingerprint(), "manual")
    _write_raw_capacity_record(g, log_path, stale)

    asyncio.run(g._maybe_derive_capacity(_capability(reranker_status="failing")))

    records = g._read_capacity_records_sync(log_path)
    assert len(records) == 1, "no healthy probe yet -- must not fire or write"


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
    assert body["capacity"]["trigger"] == "first_derivation"
    assert body["capacity"]["derived"]["s_mean_s"] == 10.0


def test_single_search_exceeds_wait_flag(monkeypatch, tmp_path):
    """M10: makes explicit what queue_bound == 0 means -- a fast backend
    reports False, a backend slower than the tolerable wait reports True,
    and an unmeasured s_mean reports None rather than either boolean.

    N2 (fix round 2, record-shape extension): every derived record also
    carries the tolerance it was computed against -- pinned here at the
    30.0s default (fact:1309: pin the VALUE, not just an equality) -- so a
    reader (postflight, /health) never has to know CAPACITY_TOLERABLE_WAIT_S
    separately to make sense of a stored queue_bound or exceeds-flag."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    fast = g._build_capacity_record(
        _capability(reranker_projected_s=5.0), g._capacity_fingerprint(), "manual")
    assert fast["derived"]["single_search_exceeds_wait"] is False
    assert fast["derived"]["tolerable_wait_s"] == 30.0

    slow = g._build_capacity_record(
        _capability(reranker_projected_s=70.0), g._capacity_fingerprint(), "manual")
    assert slow["derived"]["single_search_exceeds_wait"] is True
    assert slow["derived"]["queue_bound"] == 0
    assert slow["derived"]["tolerable_wait_s"] == 30.0

    unknown = g._build_capacity_record(None, g._capacity_fingerprint(), "manual")
    assert unknown["derived"]["single_search_exceeds_wait"] is None
    assert unknown["derived"]["tolerable_wait_s"] == 30.0


def test_encoder_config_fingerprint_redacts_url_userinfo(monkeypatch, tmp_path):
    """L17: userinfo (user:pass@) must be stripped from the embedder/
    reranker URLs before they persist to the on-disk JSONL log."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    monkeypatch.setattr(g, "EMBEDDER_URL", "http://user:secret@localhost:8070")
    monkeypatch.setattr(g, "RERANKER_URL", "http://localhost:8071")

    fp = g._encoder_config_fingerprint()

    assert "secret" not in fp["embedder_url"]
    assert "user" not in fp["embedder_url"]
    assert fp["embedder_url"] == "http://localhost:8070"
    assert fp["reranker_url"] == "http://localhost:8071"


def test_capacity_log_file_and_dir_are_secured(monkeypatch, tmp_path):
    """L15: the JSONL log lands 0600 and its (possibly freshly-created)
    parent directory lands 0700."""
    log_path = tmp_path / "capdir" / "derivations.jsonl"
    g = _load_gateway(monkeypatch, log_path)

    asyncio.run(g._maybe_derive_capacity(_capability()))

    mode_file = stat.S_IMODE(os.stat(log_path).st_mode)
    mode_dir = stat.S_IMODE(os.stat(log_path.parent).st_mode)
    assert mode_file == 0o600
    assert mode_dir == 0o700


# ── Fail-open ─────────────────────────────────────────────────────────────────


def test_capacity_env_number_fails_open_on_bad_float(monkeypatch, tmp_path, caplog):
    """H2: a malformed CAPACITY_* numeric env value must not crash the
    gateway on import -- it logs one warning naming the variable and its
    fallback, and the module-level constant lands on the documented
    default."""
    monkeypatch.setenv("CAPACITY_DRIFT_BAND_FACTOR", "not-a-number")

    with caplog.at_level("WARNING"):
        g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    assert g.CAPACITY_DRIFT_BAND_FACTOR == 2.0
    assert any("CAPACITY_DRIFT_BAND_FACTOR" in r.getMessage() for r in caplog.records)


def test_capacity_env_number_fails_open_on_bad_int(monkeypatch, tmp_path):
    """H2, int-cast path: same fail-open guarantee for CAPACITY_LOG_MAX_RECORDS."""
    monkeypatch.setenv("CAPACITY_LOG_MAX_RECORDS", "not-an-int")
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    assert g.CAPACITY_LOG_MAX_RECORDS == 20


def test_capacity_env_number_helper_never_raises_directly(monkeypatch, tmp_path):
    """H2: exercise the module-level helper function itself, not just a
    module reload -- a bad value returns the default with no exception."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    assert g._capacity_env_number("SOME_VAR_NOT_SET_XYZ", 42.0, float) == 42.0
    monkeypatch.setenv("CAPACITY_TEST_BAD_VALUE", "definitely-not-a-number")
    assert g._capacity_env_number("CAPACITY_TEST_BAD_VALUE", 42.0, float) == 42.0

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


def test_probe_none_defers_first_baseline_but_null_degrade_math_is_unaffected(monkeypatch, tmp_path):
    """N1(a) (fix round 2): capability=None (a probe that never landed) has
    no "ok" status either, so it must NOT establish the first-ever baseline
    -- same freeze risk as any other not-ok first probe (see the
    first_derivation-defers tests below). This supersedes this test's own
    prior behavior (pre-N1, capability=None used to write a null-degraded
    record on the very first cycle); _build_capacity_record's own
    null-degrade math (s_mean/queue_bound -> None, client_ceiling_s ->
    fallback) is unchanged and still exercised directly here so that
    regression coverage is not lost."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    asyncio.run(g._maybe_derive_capacity(None))
    assert g._read_capacity_records_sync(g.CAPACITY_LOG_PATH) == [], (
        "a capability=None first probe must defer, not establish a null basis")

    record = g._build_capacity_record(None, g._capacity_fingerprint(), "manual")
    assert record["derived"]["s_mean_s"] is None
    assert record["derived"]["queue_bound"] is None
    assert record["derived"]["client_ceiling_s"] == g.CAPACITY_SEARCH_TIMEOUT_FALLBACK_S


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


def test_log_max_records_zero_clamps_to_one(monkeypatch, tmp_path):
    """M4: records[-0:] is the WHOLE list, not zero records -- a
    CAPACITY_LOG_MAX_RECORDS of 0 (or negative) must clamp to 1 (keep at
    least the latest) rather than keeping everything ever written."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    monkeypatch.setattr(g, "CAPACITY_LOG_MAX_RECORDS", 0)

    for i in range(5):
        rec = g._build_capacity_record(_capability(), g._capacity_fingerprint(), "manual")
        asyncio.run(g._append_capacity_record(rec))

    records = g._read_capacity_records_sync(g.CAPACITY_LOG_PATH)
    assert len(records) == 1


# ── Mutation-check target ────────────────────────────────────────────────────
# Inverting the comparison in _capacity_drift_outside_band -- changing
# `ratio > band_factor or ratio < (1.0 / band_factor)` to
# `ratio >= band_factor or ratio <= (1.0 / band_factor)` -- makes
# test_drift_at_exactly_the_band_factor_does_not_fire fail (a record is
# written where the test asserts none is). Confirmed by hand: with `>=`
# substituted, that test's `len(records) == 1` assertion fails with 2.
