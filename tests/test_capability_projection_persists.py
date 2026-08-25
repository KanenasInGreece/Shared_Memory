"""INVARIANT: a projection, once measured, never disappears from /health;
it only ages and says so.

The defect (measured, fact:1560): `_capability_probe_daemon` replaced the
module-level capability snapshot WHOLESALE on every cycle, and the failing
branch of `_probe_capability` writes only `status`/`error`. So
`projected_full_payload_s` — the number both the server-side capacity
derivation and the CLIENT's own search timeout are sized from — vanished
from a backend's block at exactly the moment that backend was too busy to
answer. An absent number reads as "nothing measured, assume the default",
when the truth was "the last thing we measured said this is expensive".

Every test here is pure/synthetic: no gateway, no encoder, no database.
"""
import asyncio
import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def _load_gateway(monkeypatch, capacity_log_path):
    """Same reload discipline as test_capacity_derivation.py's own
    _load_gateway (itself modelled on test_health_anonymous_slimming.py):
    coordinator first, then hive_mind_proxy, with CAPACITY_LOG_PATH set
    BEFORE the reload since that module reads it at import time. Copied
    rather than imported -- sibling test modules are not importable as a
    package here, and every other file in this suite duplicates it too."""
    import importlib
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    monkeypatch.setenv("CAPACITY_LOG_PATH", str(capacity_log_path))
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    return g


# ── Fixtures: the two shapes _probe_capability actually produces ───────────

def _ok_reading(probed_at="2026-08-25T10:00:00+00:00",
                rr_chars_s=1000, rr_projected=10.0,
                emb_chars_s=2000, emb_projected=5.0):
    """A cycle in which both backends were successfully TIMED."""
    return {
        "probed_at": probed_at,
        "status": "ok",
        "gateway_host_load1": 1.5,
        "reranker": {
            "probe_chars": 4000, "latency_s": 4.0,
            "throughput_chars_s": rr_chars_s,
            "projected_full_payload_s": rr_projected,
            "ceiling_s": 30.0, "serves_full_payload": True, "status": "ok",
        },
        "embedder": {
            "probe_chars": 1000, "latency_s": 0.5,
            "throughput_chars_s": emb_chars_s,
            "projected_full_payload_s": emb_projected,
            "ceiling_s": 30.0, "serves_full_payload": True, "status": "ok",
        },
    }


def _failing_reading(probed_at="2026-08-25T10:10:00+00:00",
                     error="ServerTimeoutError"):
    """A cycle in which the probe itself failed — verbatim the shape the
    exception branch of _probe_capability builds (asserted against the real
    function by test_real_failing_probe_block_has_no_projection below)."""
    return {
        "probed_at": probed_at,
        "status": "degraded",
        "gateway_host_load1": 14.2,
        "reranker": {"probe_chars": 4000, "status": "failing", "error": error},
        "embedder": {"probe_chars": 1000, "status": "failing", "error": error},
    }


class _RaisingSession:
    """Minimal aiohttp-session stand-in whose every POST raises."""

    def post(self, *_a, **_kw):
        raise OSError("connection refused")


def test_real_failing_probe_block_has_no_projection(monkeypatch, tmp_path):
    """Grounds the _failing_reading fixture in the real code: when the probe
    raises, the block genuinely carries no projection — which is the whole
    reason the previous snapshot has to survive the cycle."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    out = asyncio.run(g._probe_capability(_RaisingSession()))

    for backend in ("reranker", "embedder"):
        assert out[backend]["status"] == "failing"
        assert out[backend]["error"] == "OSError"
        assert "projected_full_payload_s" not in out[backend]
    assert out["status"] == "degraded"


# ── The merge itself ───────────────────────────────────────────────────────

def test_failing_after_ok_keeps_the_measured_projection(monkeypatch, tmp_path):
    """THE INVARIANT. A failing cycle keeps the last measured values, adds
    projection_stale/last_ok_at, and keeps THIS cycle's own verdict."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    merged = g._merge_capability_projection(
        _ok_reading(), _failing_reading())

    rr = merged["reranker"]
    # The measured set survives -- pinned to VALUES, not merely "present"
    # (fact:1309: an equality between two expressions is half a guard).
    assert rr["projected_full_payload_s"] == 10.0
    assert rr["ceiling_s"] == 30.0
    assert rr["serves_full_payload"] is True
    assert rr["throughput_chars_s"] == 1000
    assert rr["latency_s"] == 4.0
    # ...and it is honestly labelled as ageing, with the age of the NUMBERS.
    assert rr["projection_stale"] is True
    assert rr["last_ok_at"] == "2026-08-25T10:00:00+00:00"
    # This cycle's own verdict is untouched -- the block does not claim to
    # be healthy just because it still carries numbers.
    assert rr["status"] == "failing"
    assert rr["error"] == "ServerTimeoutError"
    assert merged["status"] == "degraded"
    assert merged["probed_at"] == "2026-08-25T10:10:00+00:00"
    # Both backends, not just the reranker.
    assert merged["embedder"]["projected_full_payload_s"] == 5.0
    assert merged["embedder"]["projection_stale"] is True


def test_ok_after_failing_clears_the_stale_flag_with_fresh_numbers(monkeypatch, tmp_path):
    """Recovery: the fresh values REPLACE the carried ones and the flag goes
    to False (key present -- additive, never removed: Group 3)."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    stale = g._merge_capability_projection(_ok_reading(), _failing_reading())
    recovered = g._merge_capability_projection(
        stale,
        _ok_reading(probed_at="2026-08-25T10:20:00+00:00",
                    rr_chars_s=250, rr_projected=40.0),
    )

    rr = recovered["reranker"]
    assert rr["projected_full_payload_s"] == 40.0     # fresh, not the old 10.0
    assert rr["throughput_chars_s"] == 250
    assert rr["projection_stale"] is False
    assert rr["last_ok_at"] == "2026-08-25T10:20:00+00:00"
    assert rr["status"] == "ok"


def test_never_measured_backend_stays_shapeless_and_invents_nothing(monkeypatch, tmp_path):
    """A backend that has never once been timed gets NO projection and the
    third state -- None -- rather than a fabricated number or a False that
    would read as "measured, and fresh"."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    first = g._merge_capability_projection(None, _failing_reading())
    still = g._merge_capability_projection(first, _failing_reading(
        probed_at="2026-08-25T10:20:00+00:00"))

    for merged in (first, still):
        for backend in ("reranker", "embedder"):
            block = merged[backend]
            assert "projected_full_payload_s" not in block
            assert "ceiling_s" not in block
            assert "serves_full_payload" not in block
            assert block["projection_stale"] is None
            assert "last_ok_at" not in block


def test_last_ok_at_is_chained_not_reset_by_repeated_failures(monkeypatch, tmp_path):
    """Three failing cycles in a row still report when the surviving numbers
    were actually taken -- the stamp ages with the DATA, not with the last
    cycle that carried it forward."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    snapshot = _ok_reading()
    for minute in (10, 20, 30):
        snapshot = g._merge_capability_projection(
            snapshot, _failing_reading(probed_at=f"2026-08-25T10:{minute}:00+00:00"))

    assert snapshot["reranker"]["projected_full_payload_s"] == 10.0
    assert snapshot["reranker"]["last_ok_at"] == "2026-08-25T10:00:00+00:00"
    assert snapshot["reranker"]["projection_stale"] is True
    assert snapshot["probed_at"] == "2026-08-25T10:30:00+00:00"


def test_a_too_slow_reading_is_a_measurement_and_survives(monkeypatch, tmp_path):
    """`too_slow` means the probe SUCCEEDED and the answer was alarming --
    exactly the value a client must not lose on the next failing cycle."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    slow = _ok_reading(rr_projected=64.0)
    slow["reranker"]["status"] = "too_slow"
    slow["reranker"]["serves_full_payload"] = False
    slow["status"] = "degraded"

    merged = g._merge_capability_projection(slow, _failing_reading())

    assert merged["reranker"]["projected_full_payload_s"] == 64.0
    assert merged["reranker"]["serves_full_payload"] is False
    assert merged["reranker"]["projection_stale"] is True


def test_merge_leaves_a_malformed_block_alone(monkeypatch, tmp_path):
    """Fail-open (Group 3): this is an observability path -- a reading that
    is not the expected shape must never raise inside the probe daemon."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    junk = {"probed_at": "x", "status": "degraded",
            "reranker": "not-a-dict", "embedder": None}
    merged = g._merge_capability_projection(_ok_reading(), junk)
    assert merged["reranker"] == "not-a-dict"
    assert merged["embedder"] is None
    assert g._merge_capability_projection(_ok_reading(), None) is None


# ── The daemon: where the wholesale replacement lived ──────────────────────

def _run_probe_cycles(g, monkeypatch, readings):
    """Drive the REAL _capability_probe_daemon over a fixed list of probe
    readings (one per cycle) and stop. This is the mutation target: restore
    the wholesale `_capability = await _probe_capability(...)` assignment
    and the daemon tests below must die."""
    stop = asyncio.Event()
    cycles = {"n": 0}

    async def fake_probe(_session):
        reading = copy.deepcopy(readings[cycles["n"]])
        cycles["n"] += 1
        if cycles["n"] >= len(readings):
            stop.set()
        return reading

    async def fake_derive(_capability, coordinator=None):
        return None

    monkeypatch.setattr(g, "_probe_capability", fake_probe)
    monkeypatch.setattr(g, "_maybe_derive_capacity", fake_derive)
    monkeypatch.setattr(g, "CAPABILITY_PROBE_INTERVAL_S", 0.0)

    class _Proxy:
        session = object()

    asyncio.run(g._capability_probe_daemon(_Proxy(), stop))
    assert cycles["n"] == len(readings)
    return g.capability_snapshot()


def test_daemon_snapshot_keeps_the_projection_across_a_failing_cycle(monkeypatch, tmp_path):
    """End to end through the daemon loop and out of capability_snapshot()
    -- the dict /health publishes verbatim as `backend_capability`."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    snapshot = _run_probe_cycles(g, monkeypatch,
                                 [_ok_reading(), _failing_reading()])

    assert snapshot["reranker"]["projected_full_payload_s"] == 10.0
    assert snapshot["reranker"]["projection_stale"] is True
    assert snapshot["reranker"]["last_ok_at"] == "2026-08-25T10:00:00+00:00"
    assert snapshot["reranker"]["status"] == "failing"
    assert snapshot["embedder"]["projected_full_payload_s"] == 5.0


def test_daemon_snapshot_recovers_fresh_values_on_the_next_ok_cycle(monkeypatch, tmp_path):
    """ok -> failing -> ok, through the daemon: the stale flag clears and
    the newest measurement wins."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    snapshot = _run_probe_cycles(g, monkeypatch, [
        _ok_reading(),
        _failing_reading(),
        _ok_reading(probed_at="2026-08-25T10:20:00+00:00", rr_projected=22.0),
    ])

    assert snapshot["reranker"]["projected_full_payload_s"] == 22.0
    assert snapshot["reranker"]["projection_stale"] is False
    assert snapshot["reranker"]["last_ok_at"] == "2026-08-25T10:20:00+00:00"


# ── The consequence: what a client and the capacity record now see ─────────

def test_client_ceiling_no_longer_collapses_to_the_fallback_on_a_failing_cycle(
        monkeypatch, tmp_path):
    """The defect's actual cost. A slow-but-measured reranker (100 s
    projected) plus embedder (10 s) sizes the search timeout at
    (100 + 10) * 1.5 + 15 = 180.0 s. Lose the projection and the same
    deployment falls back to CAPACITY_SEARCH_TIMEOUT_FALLBACK_S (120.0 s)
    -- BELOW the cost already measured -- while the gateway keeps working
    the request. Values pinned on both sides (fact:1309)."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    ok = _ok_reading(rr_projected=100.0, emb_projected=10.0)
    assert g._capacity_client_ceiling_s(ok) == 180.0

    vanished = _failing_reading()                      # the OLD behaviour
    assert g._capacity_client_ceiling_s(vanished) == 120.0
    assert g.CAPACITY_SEARCH_TIMEOUT_FALLBACK_S == 120.0

    kept = g._merge_capability_projection(copy.deepcopy(ok), _failing_reading())
    assert g._capacity_client_ceiling_s(kept) == 180.0


def test_capacity_derived_is_identical_on_a_stale_cycle(monkeypatch, tmp_path):
    """capacity.derived stops degrading to its not-yet-measured fallbacks
    the moment a backend gets busy: s_mean_s and client_ceiling_s on a
    stale cycle equal the ok cycle's, and reranker_projection_stale (new,
    additive) is what tells the reader the numbers are ageing."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    fingerprint = g._capacity_fingerprint()

    # Both records are built from what the DAEMON would hold, i.e. a merged
    # snapshot -- the raw probe output never reaches capacity derivation.
    ok = g._merge_capability_projection(None, _ok_reading())
    fresh = g._build_capacity_record(ok, fingerprint, "manual")
    stale = g._build_capacity_record(
        g._merge_capability_projection(copy.deepcopy(ok), _failing_reading()),
        fingerprint, "manual")

    assert stale["derived"]["s_mean_s"] == fresh["derived"]["s_mean_s"] == 10.0
    assert (stale["derived"]["client_ceiling_s"]
            == fresh["derived"]["client_ceiling_s"] == 37.5)
    assert (stale["derived"]["single_search_exceeds_wait"]
            is fresh["derived"]["single_search_exceeds_wait"] is False)
    assert stale["derived"]["queue_bound"] == fresh["derived"]["queue_bound"] == 3

    assert fresh["derived"]["reranker_projection_stale"] is False
    assert stale["derived"]["reranker_projection_stale"] is True
    # Never measured -> None, never False (which would claim a fresh
    # measurement that never happened).
    never = g._build_capacity_record(
        g._merge_capability_projection(None, _failing_reading()),
        fingerprint, "manual")
    assert never["derived"]["reranker_projection_stale"] is None
    # ...and so does a caller that predates the merge entirely (no
    # capability at all, or a raw probe reading): absent, never False.
    legacy = g._build_capacity_record(None, fingerprint, "manual")
    assert legacy["derived"]["reranker_projection_stale"] is None
    raw = g._build_capacity_record(_ok_reading(), fingerprint, "manual")
    assert raw["derived"]["reranker_projection_stale"] is None


def test_capacity_derived_keys_are_additive_only(monkeypatch, tmp_path):
    """Group 3 (monitor contract): the new key is ADDED; every key the
    record carried before is still there, spelled the same way."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    derived = g._build_capacity_record(
        _ok_reading(), g._capacity_fingerprint(), "manual")["derived"]

    for key in ("s_mean_s", "s_max_measured_s", "s_mean_measured_s",
                "payload_basis", "payload_basis_sample_count",
                "payload_mean_chars_measured", "payload_max_chars_measured",
                "client_ceiling_s", "queue_bound", "tolerable_wait_s",
                "single_search_exceeds_wait",
                "recommended_reranker_mem_limit_bytes"):
        assert key in derived, f"{key} disappeared from capacity.derived"
    assert "reranker_projection_stale" in derived
