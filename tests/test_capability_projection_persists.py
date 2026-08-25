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
    assert rr["throughput_chars_s"] == 1000
    assert rr["latency_s"] == 4.0
    # ...but the VERDICT does not travel with it (A-1 / ADV-1).
    assert rr["serves_full_payload"] is None
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
    assert merged["reranker"]["projection_stale"] is True
    # The alarming NUMBER survives; the verdict beside it does not (A-1).
    assert merged["reranker"]["serves_full_payload"] is None


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
    stale cycle equal the ok cycle's."""
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

    # A-4 (ADV-6): staleness is NOT reported here. `capacity` on /health is
    # the last DERIVED record and derivation is rare, so a flag in this
    # block would read "fresh" during exactly the outage it exists to
    # expose. It lives only where it is re-evaluated every cycle.
    for record in (fresh, stale):
        assert "reranker_projection_stale" not in record["derived"]


def test_staleness_is_reported_only_where_it_is_re_evaluated(monkeypatch, tmp_path):
    """A-4 (ADV-6). The live block re-computes every probe cycle; the
    capacity record is frozen between rare derivations. A reader must be
    able to trust the flag it finds, so there is exactly one place to find
    it -- and this test dies if a copy is ever put back in the derived
    block."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    snapshot = _run_probe_cycles(g, monkeypatch,
                                 [_ok_reading(), _failing_reading()])
    record = g._build_capacity_record(
        g._capability, g._capacity_fingerprint(), "manual")

    assert snapshot["reranker"]["projection_stale"] is True      # live
    assert "reranker_projection_stale" not in record["derived"]  # historical
    # ...and the record still says which reading it was derived from.
    assert record["probe"]["probe_stale"] is True


def test_capacity_record_keys_are_additive_only(monkeypatch, tmp_path):
    """Group 3 (monitor contract): every key the record carried before is
    still there, spelled the same way; the new ones are additions."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    record = g._build_capacity_record(
        _ok_reading(), g._capacity_fingerprint(), "manual")

    for key in ("s_mean_s", "s_max_measured_s", "s_mean_measured_s",
                "payload_basis", "payload_basis_sample_count",
                "payload_mean_chars_measured", "payload_max_chars_measured",
                "client_ceiling_s", "queue_bound", "tolerable_wait_s",
                "single_search_exceeds_wait",
                "recommended_reranker_mem_limit_bytes"):
        assert key in record["derived"], f"{key} disappeared from capacity.derived"
    for key in ("reranker_chars_per_s", "reranker_status",
                "embedder_chars_per_s", "probed_at"):
        assert key in record["probe"], f"{key} disappeared from capacity.probe"
    for key in ("reranker_measured_at", "embedder_measured_at", "probe_stale"):
        assert key in record["probe"]


# ── A-1 (ADV-1): a verdict is not a reading ────────────────────────────────

def test_no_affirmative_verdict_survives_onto_a_failing_backend(monkeypatch, tmp_path):
    """The carry must never publish an affirmative green beside a backend
    that answered nothing. `serves_full_payload` is the one derived
    JUDGEMENT in the measured set, and it is the one that inverts: `true`
    next to `status: "failing"` is a false green for any renderer that does
    not also happen to show the sibling stale flag."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    merged = g._merge_capability_projection(_ok_reading(), _failing_reading())

    for backend in ("reranker", "embedder"):
        block = merged[backend]
        assert block["status"] == "failing"
        assert block["serves_full_payload"] is None
        # Nothing else in the block asserts present-tense capability either.
        assert not any(v is True for k, v in block.items()
                       if k != "projection_stale"), block
    assert "serves_full_payload" not in g._PROJECTION_CARRY_KEYS


def test_a_fresh_cycle_still_publishes_the_real_verdict(monkeypatch, tmp_path):
    """A-1 nulls the verdict only while the reading is ageing -- a measured
    cycle publishes the verdict it actually computed, both ways."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    good = g._merge_capability_projection(None, _ok_reading())
    assert good["reranker"]["serves_full_payload"] is True

    slow = _ok_reading()
    slow["reranker"]["serves_full_payload"] = False
    slow["reranker"]["status"] = "too_slow"
    assert g._merge_capability_projection(
        None, slow)["reranker"]["serves_full_payload"] is False


# ── A-2 (ADV-2): the age is published, and it is the age at READ time ──────

_REAL_DATETIME = __import__("datetime").datetime
_UTC = __import__("datetime").timezone.utc


class _FixedClock:
    """datetime stand-in whose now() is frozen; everything else is real."""

    def __init__(self, at):
        self._at = at

    def now(self, tz=None):
        return self._at

    def __getattr__(self, name):
        return getattr(_REAL_DATETIME, name)


def test_projection_age_seconds_since_last_ok_at(monkeypatch, tmp_path):
    """Pinned VALUES (fact:1309), including the not-a-number cases."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    now = _REAL_DATETIME(2026, 8, 25, 10, 10, 0, tzinfo=_UTC)

    assert g._projection_age_s("2026-08-25T10:00:00+00:00", now) == 600.0
    assert g._projection_age_s("2026-08-25T10:10:00+00:00", now) == 0.0
    # Never measured, unstamped, unparseable -> None, never 0.0 (which would
    # read as "just measured").
    assert g._projection_age_s(None, now) is None
    assert g._projection_age_s("", now) is None
    assert g._projection_age_s("not-a-timestamp", now) is None
    # Clock skew: a future stamp reads as "just measured", never negative.
    assert g._projection_age_s("2026-08-25T10:20:00+00:00", now) == 0.0


def test_snapshot_publishes_a_live_age_that_grows_between_probes(monkeypatch, tmp_path):
    """The age is computed when /health READS the snapshot, not when the
    probe wrote it -- so a probe daemon that has stopped running shows a
    growing age instead of a frozen one. Same snapshot, two clocks, two
    ages, and NO cap on how large it gets."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    _run_probe_cycles(g, monkeypatch, [_ok_reading(), _failing_reading()])

    monkeypatch.setattr(g, "datetime",
                        _FixedClock(_REAL_DATETIME(2026, 8, 25, 10, 10, tzinfo=_UTC)))
    assert g.capability_snapshot()["reranker"]["projection_age_s"] == 600.0

    monkeypatch.setattr(g, "datetime",
                        _FixedClock(_REAL_DATETIME(2026, 9, 1, 10, 0, tzinfo=_UTC)))
    week_old = g.capability_snapshot()["reranker"]
    assert week_old["projection_age_s"] == 604800.0      # 7 days, uncapped
    assert week_old["projected_full_payload_s"] == 10.0  # still the only number there is


def test_snapshot_age_is_none_when_nothing_was_ever_measured(monkeypatch, tmp_path):
    """No stamp, no age -- and no invented 0.0. The pre-first-probe snapshot
    has no backend blocks at all and must neither grow any nor raise."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    assert g.capability_snapshot() == {"status": "unknown", "probed_at": None}

    _run_probe_cycles(g, monkeypatch, [_failing_reading()])

    snapshot = g.capability_snapshot()
    assert snapshot["reranker"]["projection_age_s"] is None
    assert snapshot["reranker"]["projection_stale"] is None


def test_snapshot_never_lets_a_reader_edit_the_carried_projection(monkeypatch, tmp_path):
    """The carried numbers are now the only copy of a measurement meant to
    outlive its cycle -- a consumer editing what capability_snapshot()
    returns must not reach the daemon's own state."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    _run_probe_cycles(g, monkeypatch, [_ok_reading(), _failing_reading()])

    snap = g.capability_snapshot()
    snap["reranker"].pop("projected_full_payload_s")
    snap["reranker"]["status"] = "vandalised"

    assert g._capability["reranker"]["projected_full_payload_s"] == 10.0
    assert g._capability["reranker"]["status"] == "failing"


# ── A-3 (ADV-5): capacity.probe must not mix cycles under one timestamp ────

def test_probe_block_dates_each_reading_and_flags_a_carried_one(monkeypatch, tmp_path):
    """`reranker_chars_per_s` used to mean "measured at probed_at". Now that
    it can be carried, the timestamp it was really measured at travels with
    it and `probe_stale` says the block mixes cycles."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    fingerprint = g._capacity_fingerprint()

    ok = g._merge_capability_projection(None, _ok_reading())
    fresh = g._build_capacity_record(ok, fingerprint, "manual")["probe"]
    assert fresh["probe_stale"] is False
    assert fresh["probed_at"] == "2026-08-25T10:00:00+00:00"
    assert fresh["reranker_measured_at"] == "2026-08-25T10:00:00+00:00"
    assert fresh["embedder_measured_at"] == "2026-08-25T10:00:00+00:00"

    stale = g._build_capacity_record(
        g._merge_capability_projection(copy.deepcopy(ok), _failing_reading()),
        fingerprint, "manual")["probe"]
    assert stale["reranker_chars_per_s"] == 1000                 # carried
    assert stale["probed_at"] == "2026-08-25T10:10:00+00:00"     # this cycle
    assert stale["reranker_measured_at"] == "2026-08-25T10:00:00+00:00"   # measured then
    assert stale["embedder_measured_at"] == "2026-08-25T10:00:00+00:00"
    assert stale["probe_stale"] is True
    assert stale["reranker_status"] == "failing"

    # A capability that never went through the merge (a caller predating it)
    # still dates its readings honestly, from the snapshot's own stamp.
    legacy = g._build_capacity_record(_ok_reading(), fingerprint, "manual")["probe"]
    assert legacy["reranker_measured_at"] == "2026-08-25T10:00:00+00:00"
    assert legacy["probe_stale"] is False


def test_a_stale_record_reaches_capacity_jsonl_carrying_the_flag(monkeypatch, tmp_path):
    """The durable log is where a poisoned row costs a future session a
    diagnosis, so the flag has to survive the round trip. Driven through the
    real trigger that can store a not-ok cycle: a fingerprint mismatch on
    this process's first probe (a rolling restart, or a shared log path)."""
    log_path = str(tmp_path / "cap.jsonl")
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    prior = g._build_capacity_record(
        g._merge_capability_projection(None, _ok_reading()),
        {"hardware": {"mem_total_bytes": 1}, "encoder_config": {"stale": True}},
        "manual")
    g._write_capacity_records_sync(log_path, [prior])

    stale_capability = g._merge_capability_projection(
        g._merge_capability_projection(None, _ok_reading()), _failing_reading())
    asyncio.run(g._maybe_derive_capacity(stale_capability))

    stored = g._read_capacity_records_sync(log_path)[-1]
    assert stored["trigger"] == "gateway_start_fingerprint_mismatch"
    assert stored["probe"]["probe_stale"] is True
    assert stored["probe"]["reranker_chars_per_s"] == 1000
    assert stored["probe"]["reranker_measured_at"] == "2026-08-25T10:00:00+00:00"
    assert stored["probe"]["probed_at"] == "2026-08-25T10:10:00+00:00"


# ── A-5 (T-01): the server mirror applies the client's ignorance rule ──────

def _fact_1560_shape():
    """The shape fact:1560 actually measured: the embedder probes fine
    (1.8 s projected) while the reranker answers nothing at all."""
    return {
        "probed_at": "2026-08-25T10:10:00+00:00",
        "status": "degraded",
        "reranker": {"probe_chars": 4000, "status": "failing",
                     "error": "ServerTimeoutError"},
        "embedder": {"probe_chars": 1000, "latency_s": 0.2,
                     "throughput_chars_s": 5000,
                     "projected_full_payload_s": 1.8,
                     "ceiling_s": 30.0, "serves_full_payload": True,
                     "status": "ok"},
    }


def test_partial_ignorance_never_ceilings_below_the_fallback(monkeypatch, tmp_path):
    """T-01. One backend's number is only a LOWER bound on the true cost
    while the other backend's cost is unknown. Pinned VALUES: the old mirror
    returned 30.0 here (1.8 * 1.5 + 15 = 17.7, clamped up to the 30 s
    floor) while the client returned 120.0 -- the mirror never saw the case
    the mechanism exists for."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    assert g._capacity_client_ceiling_s(_fact_1560_shape()) == 120.0
    assert g.CAPACITY_SEARCH_TIMEOUT_FALLBACK_S == 120.0
    assert g.CAPACITY_SEARCH_TIMEOUT_FLOOR_S == 30.0
    # The naive derivation this replaces, spelled out so the test states the
    # number it is refusing to return.
    assert max(30.0, 1.8 * 1.5 + 15) == 30.0

    # Same rule for a STALE block with no projection of its own...
    stale_shape = _fact_1560_shape()
    stale_shape["reranker"] = {"status": "ok", "projection_stale": True}
    assert g._capacity_client_ceiling_s(stale_shape) == 120.0

    # ...and for a projection that is present but unparseable: an unknown
    # cost, not a zero one.
    junk_shape = _fact_1560_shape()
    junk_shape["reranker"]["projected_full_payload_s"] = "soon"
    assert g._capacity_client_ceiling_s(junk_shape) == 120.0


def test_full_ignorance_and_full_knowledge_are_unchanged(monkeypatch, tmp_path):
    """The new floor fires ONLY on partial ignorance -- the two ends of the
    range keep their existing values."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    assert g._capacity_client_ceiling_s(None) == 120.0            # nothing known
    assert g._capacity_client_ceiling_s(_ok_reading()) == 37.5    # all known
    # A carried-but-positive projection is KNOWN, not unknown: the stale
    # flag alone must not inflate a ceiling that has a real number behind it.
    kept = g._merge_capability_projection(
        g._merge_capability_projection(None, _ok_reading()), _failing_reading())
    assert g._capacity_client_ceiling_s(kept) == 37.5
    # The operator's escape hatch still wins outright.
    monkeypatch.setattr(g, "CAPACITY_SEARCH_TIMEOUT_S", 42.0)
    assert g._capacity_client_ceiling_s(_fact_1560_shape()) == 42.0


def test_server_mirror_matches_the_client_on_the_fact_1560_shape(monkeypatch, tmp_path):
    """The docstring on _capacity_client_ceiling_s promises a parity test.
    The existing one exercises a single happy-path input and stayed green
    through the divergence T-01 measured, so the shape the mechanism exists
    for is pinned here as well -- on the VALUE, both sides."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    import importlib.util
    import inspect
    spec = importlib.util.spec_from_file_location(
        "memory_bridge_parity_1560",
        os.path.join(os.path.dirname(__file__), "..", "shared-memory-skill",
                     "shared-memory", "scripts", "memory_bridge.py"),
    )
    memory_bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(memory_bridge)

    shape = _fact_1560_shape()
    assert g._capacity_client_ceiling_s(shape) == 120.0          # the server, pinned
    assert memory_bridge.search_ceiling(_ok_reading()) == 37.5   # happy path agrees

    if "capacity" not in inspect.signature(memory_bridge.search_ceiling).parameters:
        import pytest
        pytest.skip(
            "client half of this rule is PR #310 (fix/client-ceiling-never-"
            "below-fallback), not yet on this branch: the shipped client "
            "returns 30.0 for the fact:1560 shape while this server now "
            "returns 120.0. This assertion must GO GREEN, not stay skipped, "
            "once #310 is merged -- if it is still skipping on main, the two "
            "doors have diverged and nothing else is watching.")
    assert memory_bridge.search_ceiling(shape) == 120.0
    assert g._capacity_client_ceiling_s(shape) == memory_bridge.search_ceiling(shape)
