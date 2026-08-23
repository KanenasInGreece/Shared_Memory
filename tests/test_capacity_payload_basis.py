"""Measured-payload capacity basis (operator rulings, 2026-08-23, on top of
R0-I / decision:1424). s_mean_s always projected onto a fixed THEORETICAL
worst-case payload (search_candidate_floor x rerank_max_doc_chars = 491,520
chars) -- on the reference workstation this produced a single_search_
exceeds_wait false alarm (s_mean_s 36.4s against real search times of
2.78-10.84s wall; real searches measured a MEAN of 71,139
rerank_payload_chars and a MAX of 101,240).

RULING 1: the fix uses the coordinator's OBSERVED MAXIMUM payload as the
basis that feeds queue_bound/single_search_exceeds_wait, NOT the mean -- a
capacity signal must stay worst-case (max/mean = 1.42x on the reference
host; an average-case basis would under-project a search at the observed
max by exactly that factor, the wrong direction for a safety bound). The
mean is still computed and reported (s_mean_measured_s) as cheap,
informational context, but must never drive the two decision fields.

RULING 2: a capacity record is only ever (re)computed on a trigger, and at
the moment most triggers fire the coordinator's payload counters are at or
near zero -- so without an explicit trigger the whole feature is INERT in
normal operation (confirmed live: a fresh baseline + six real searches left
the record reading theoretical/samples 0). This file also covers the
`payload_threshold_crossed` trigger.

B-1/B-2/B-3 (reviewer, 2026-08-23): the trigger's FIRST design used a
process-local one-shot latch plus "the stored basis is still theoretical".
That was wrong across a restart -- the stored record on disk already said
"measured" from the previous process's life, so guard (b) was permanently
false for the rest of the install's life even as a NEW process's own
observed max grew past what the dead process ever saw (B-1, HIGH:
staleness in the UNSAFE direction for a capacity signal). The fix drops
the process-local latch entirely: the trigger now compares the LIVE
observed max against whatever max is already on the DURABLE stored
record, regardless of which process wrote it. This is restart-safe by
construction, storm-safe without a latch (the same reading never re-fires;
only a strictly larger one does), incidentally fixes B-2 (a failed append
just gets re-attempted next cycle -- no one-shot resource to have spent
prematurely), and B-3 closes the remaining edge case by requiring the
live observed max to actually exist (non-None) before firing at all.

Same reload pattern as test_capacity_derivation.py's _load_gateway; no live
infrastructure -- a bare object with the coordinator's five counter
attributes stands in for MemoryCoordinator (identical to the "coordinator
may be a partial mock" guarantee _capacity_payload_stats's own docstring
names)."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
sys.path.insert(0, os.path.dirname(__file__))

from test_capacity_derivation import _load_gateway, _capability  # noqa: E402


class _FakeCoordinator:
    """Stand-in exposing only the five attributes _capacity_payload_stats
    reads -- everything else a real MemoryCoordinator carries is
    irrelevant to this derivation and deliberately absent."""

    def __init__(self, successes=0, failures=0, chars_total=0, docs_total=0,
                 chars_max=0):
        self._rerank_successes = successes
        self._rerank_failures = failures
        self._rerank_payload_chars_total = chars_total
        self._rerank_payload_docs_total = docs_total
        self._rerank_payload_chars_max = chars_max


# ── Basis selection (ruling 1: MAX, not mean) ───────────────────────────────

def test_theoretical_basis_when_no_coordinator_passed(monkeypatch, tmp_path):
    """The default (None) coordinator -- every pre-existing caller of
    _build_capacity_record, including every test in test_capacity_
    derivation.py -- must reproduce EXACTLY the old, coordinator-less
    output: theoretical basis, s_mean_s unchanged, new fields present but
    empty/zeroed."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    rec = g._build_capacity_record(
        _capability(reranker_projected_s=10.0), g._capacity_fingerprint(), "manual")
    d = rec["derived"]
    assert d["payload_basis"] == "theoretical"
    assert d["payload_basis_sample_count"] == 0
    assert d["s_max_measured_s"] is None
    assert d["s_mean_measured_s"] is None
    assert d["payload_mean_chars_measured"] is None
    assert d["payload_max_chars_measured"] is None
    assert d["s_mean_s"] == 10.0   # UNCHANGED meaning -- verbatim theoretical


def test_theoretical_basis_when_samples_below_threshold(monkeypatch, tmp_path):
    """A coordinator IS wired through but has served fewer real searches
    than CAPACITY_PAYLOAD_MIN_SAMPLES (default 5) -- a fresh-ish install
    must not trust a handful of one-off searches as representative. The
    sample COUNT itself is still reported truthfully (fix: the comment
    used to claim 0 here, the code never did)."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    coord = _FakeCoordinator(successes=2, failures=1, chars_total=90000,
                              docs_total=66, chars_max=50000)
    rec = g._build_capacity_record(
        _capability(reranker_projected_s=10.0), g._capacity_fingerprint(),
        "manual", coord)
    d = rec["derived"]
    assert d["payload_basis"] == "theoretical"
    assert d["payload_basis_sample_count"] == 3, (
        "the true sample count is reported even on a theoretical basis -- "
        "never forced to 0"
    )
    assert d["s_max_measured_s"] is None
    assert d["s_mean_measured_s"] is None
    assert d["s_mean_s"] == 10.0


def test_measured_basis_driven_by_max_not_mean(monkeypatch, tmp_path):
    """The decisive ruling-1 test: construct a population where the mean
    and the max DISAGREE about whether the tolerable wait is exceeded, and
    prove the MAX is what actually drives queue_bound/single_search_
    exceeds_wait. Mean payload 1000 chars -> 1.0s projected (would clear a
    30s bar easily); one big outlier brings the max to 40,000 chars ->
    40.0s projected (exceeds it). If the implementation ever used the mean
    here instead of the max, this test silently starts passing on the
    wrong basis -- it is written to fail loudly if that regresses."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    coord = _FakeCoordinator(successes=5, failures=0, chars_total=5000,
                              docs_total=25, chars_max=40000)
    rec = g._build_capacity_record(
        _capability(reranker_chars_s=1000.0, reranker_projected_s=10.0),
        g._capacity_fingerprint(), "manual", coord)
    d = rec["derived"]
    assert d["payload_basis"] == "measured"
    assert d["payload_mean_chars_measured"] == 1000.0
    assert d["payload_max_chars_measured"] == 40000
    assert d["s_mean_measured_s"] == 1.0     # informational only
    assert d["s_max_measured_s"] == 40.0     # THIS drives the flag
    assert d["queue_bound"] == 0
    assert d["single_search_exceeds_wait"] is True, (
        "must be driven by the MAX (40.0s > 30.0s) -- a mean-driven "
        "implementation would report False here (1.0s < 30.0s)"
    )


def test_measured_basis_reference_workstation_false_alarm_is_resolved(monkeypatch, tmp_path):
    """The concrete false alarm from the operator's own measurement: a
    theoretical s_mean_s of 36.4s (> CAPACITY_TOLERABLE_WAIT_S 30.0) fires
    single_search_exceeds_wait=True with no coordinator/insufficient
    samples -- but once real searches establish an observed MAX consistent
    with the reported 101,240-char reading, the measured (max-based) basis
    must clear the flag with room to spare (~7.5s, per the ruling)."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    # No coordinator yet -- reproduces the reported false alarm.
    alarmed = g._build_capacity_record(
        _capability(reranker_projected_s=36.4), g._capacity_fingerprint(), "manual")
    assert alarmed["derived"]["payload_basis"] == "theoretical"
    assert alarmed["derived"]["single_search_exceeds_wait"] is True

    # 10 real searches; mean 71,139 and max 101,240 chars/search (the
    # operator's measured numbers). Throughput chosen to match the same
    # rate implied by the theoretical figure (491,520 chars / 36.4s ~=
    # 13,503 chars/s), so the max-based projection lands at ~7.5s -- the
    # concrete number the ruling itself names.
    coord = _FakeCoordinator(successes=10, failures=0,
                              chars_total=71139 * 10, docs_total=220,
                              chars_max=101240)
    cleared = g._build_capacity_record(
        _capability(reranker_chars_s=13503.0, reranker_projected_s=36.4),
        g._capacity_fingerprint(), "manual", coord)
    d = cleared["derived"]
    assert d["payload_basis"] == "measured"
    assert d["s_max_measured_s"] == 7.5
    assert d["single_search_exceeds_wait"] is False
    # The theoretical figure that caused the false alarm is still reported,
    # unchanged, alongside the measured one that now actually drives the flag.
    assert d["s_mean_s"] == 36.4


def test_measured_basis_requires_positive_reranker_throughput(monkeypatch, tmp_path):
    """Enough samples and a real max but a zero/missing current throughput
    reading (a probe that failed this cycle) must not divide by zero or
    fabricate a measured projection -- falls back to theoretical, same as
    too few samples. The true sample count is STILL reported (8), proving
    the fallback is about the projection, not about hiding the count."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    coord = _FakeCoordinator(successes=8, failures=0, chars_total=569112,
                              docs_total=176, chars_max=101240)
    rec = g._build_capacity_record(
        _capability(reranker_chars_s=0.0, reranker_projected_s=10.0),
        g._capacity_fingerprint(), "manual", coord)
    d = rec["derived"]
    assert d["payload_basis"] == "theoretical"
    assert d["payload_basis_sample_count"] == 8
    assert d["s_max_measured_s"] is None
    assert d["s_mean_measured_s"] is None


def test_capacity_payload_stats_degrades_on_partial_or_bad_coordinator(monkeypatch, tmp_path):
    """_capacity_payload_stats itself: None coordinator, an object missing
    every attribute, and one with a non-numeric attribute must all degrade
    to the all-zero/None snapshot rather than raising -- Group 3's fail-
    open discipline applies to this new read path (including the new max
    tracker) too."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")

    none_stats = g._capacity_payload_stats(None)
    assert none_stats == {"samples": 0, "chars_total": 0, "docs_total": 0,
                           "mean_chars_per_search": None,
                           "max_chars_per_search": None}

    class _Empty:
        pass

    empty_stats = g._capacity_payload_stats(_Empty())
    assert empty_stats["samples"] == 0
    assert empty_stats["mean_chars_per_search"] is None
    assert empty_stats["max_chars_per_search"] is None

    class _Bad:
        _rerank_successes = "not-a-number"
        _rerank_failures = 0
        _rerank_payload_chars_total = 0
        _rerank_payload_docs_total = 0
        _rerank_payload_chars_max = 0

    bad_stats = g._capacity_payload_stats(_Bad())
    assert bad_stats["samples"] == 0
    assert bad_stats["mean_chars_per_search"] is None
    assert bad_stats["max_chars_per_search"] is None


# ── Fail-open on the new env var ─────────────────────────────────────────────

def test_capacity_payload_min_samples_fails_open_on_bad_int(monkeypatch, tmp_path, caplog):
    """Same H2 contract as every other CAPACITY_* int setting: a malformed
    value logs one warning naming the variable and lands on the documented
    default (5) rather than crashing the gateway on import."""
    monkeypatch.setenv("CAPACITY_PAYLOAD_MIN_SAMPLES", "not-an-int")
    with caplog.at_level("WARNING"):
        g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    assert g.CAPACITY_PAYLOAD_MIN_SAMPLES == 5
    assert any("CAPACITY_PAYLOAD_MIN_SAMPLES" in r.getMessage() for r in caplog.records)


def test_capacity_payload_min_samples_env_override(monkeypatch, tmp_path):
    """A lower operator-set threshold trusts the measured basis sooner."""
    monkeypatch.setenv("CAPACITY_PAYLOAD_MIN_SAMPLES", "2")
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    assert g.CAPACITY_PAYLOAD_MIN_SAMPLES == 2

    coord = _FakeCoordinator(successes=2, failures=0, chars_total=20000,
                              docs_total=44, chars_max=12000)
    rec = g._build_capacity_record(
        _capability(reranker_chars_s=1000.0, reranker_projected_s=10.0),
        g._capacity_fingerprint(), "manual", coord)
    assert rec["derived"]["payload_basis"] == "measured"


# ── Wiring: _maybe_derive_capacity / daemon signature pass the coordinator ──

def test_maybe_derive_capacity_threads_coordinator_through(monkeypatch, tmp_path):
    """_maybe_derive_capacity's optional coordinator argument must reach
    _build_capacity_record -- verified end to end via the stored record
    rather than by patching internals."""
    log_path = tmp_path / "cap.jsonl"
    g = _load_gateway(monkeypatch, log_path)
    coord = _FakeCoordinator(successes=8, failures=0, chars_total=569112,
                              docs_total=176, chars_max=101240)

    asyncio.run(g._maybe_derive_capacity(
        _capability(reranker_chars_s=1000.0, reranker_projected_s=10.0), coord))

    records = g._read_capacity_records_sync(log_path)
    assert len(records) == 1
    assert records[-1]["derived"]["payload_basis"] == "measured"


def test_capability_probe_daemon_accepts_optional_coordinator_kwarg(monkeypatch, tmp_path):
    """The daemon signature gained an optional third parameter -- must still
    be callable positionally (main()'s call site) with no coordinator at
    all (every existing test/caller), i.e. it is additive, not breaking."""
    g = _load_gateway(monkeypatch, tmp_path / "cap.jsonl")
    import inspect
    sig = inspect.signature(g._capability_probe_daemon)
    params = list(sig.parameters)
    assert params[:2] == ["proxy", "stop_event"]
    assert sig.parameters["coordinator"].default is None


# ── Ruling 2 + B-1/B-2/B-3 fix: the payload_threshold_crossed trigger ──────
#
# B-1 (HIGH, reviewer 2026-08-23): the original design gated this trigger on
# a process-local one-shot latch PLUS "the stored basis is still
# theoretical" -- after any restart with an unchanged fingerprint, the
# stored record already said "measured" from the dead process, so the
# trigger could never fire again for the rest of the install's life, even
# as the new process's own observed max grew past what the dead process
# ever saw (staleness in the UNSAFE direction for a capacity signal). Fixed
# by dropping the latch and the "theoretical only" restriction: the trigger
# now fires whenever the LIVE observed max exceeds whatever max is already
# on the DURABLE stored record, regardless of which process wrote it.

def test_payload_threshold_crossed_fires_when_samples_cross_after_baseline(monkeypatch, tmp_path):
    """The concrete inertness bug the operator found live: a baseline
    derives theoretical/samples-0, then real traffic accumulates -- with
    no dedicated trigger the record would sit there forever (no
    fingerprint change, no config change, no reranker drift). This proves
    the new trigger picks that up on the very next probe cycle once the
    threshold is crossed (the stored record has no max yet, so ANY real
    observed max exceeds "none")."""
    log_path = tmp_path / "cap.jsonl"
    g = _load_gateway(monkeypatch, log_path)
    cap = _capability(reranker_chars_s=1000.0, reranker_projected_s=10.0)

    asyncio.run(g._maybe_derive_capacity(cap))   # baseline: first_derivation
    baseline = g._read_capacity_records_sync(log_path)
    assert len(baseline) == 1
    assert baseline[0]["trigger"] == "first_derivation"
    assert baseline[0]["derived"]["payload_basis"] == "theoretical"

    coord = _FakeCoordinator(successes=8, failures=0, chars_total=569112,
                              docs_total=176, chars_max=101240)
    asyncio.run(g._maybe_derive_capacity(cap, coord))   # samples now cross 5

    records = g._read_capacity_records_sync(log_path)
    assert len(records) == 2
    assert records[-1]["trigger"] == "payload_threshold_crossed"
    assert records[-1]["derived"]["payload_basis"] == "measured"


def test_payload_threshold_crossed_does_not_refire_on_unchanged_live_max(monkeypatch, tmp_path):
    """Repeated cycles with the SAME (already-stored) observed max must not
    re-derive on every loop -- storm-safety now comes from comparing
    against the DURABLE stored max (which the first firing already set to
    this exact value), not from a process-local latch."""
    log_path = tmp_path / "cap.jsonl"
    g = _load_gateway(monkeypatch, log_path)
    cap = _capability(reranker_chars_s=1000.0, reranker_projected_s=10.0)
    coord = _FakeCoordinator(successes=8, failures=0, chars_total=569112,
                              docs_total=176, chars_max=101240)

    asyncio.run(g._maybe_derive_capacity(cap))          # baseline
    asyncio.run(g._maybe_derive_capacity(cap, coord))    # crosses -> fires
    after_first_fire = len(g._read_capacity_records_sync(log_path))
    assert after_first_fire == 2

    # Same coordinator (same max, same sample count) -- must NOT fire again.
    for _ in range(3):
        asyncio.run(g._maybe_derive_capacity(cap, coord))
    final = g._read_capacity_records_sync(log_path)
    assert len(final) == 2, (
        "an unchanged observed max must not cause repeated re-derivation -- "
        "a storm here would be worse than the inertness bug this trigger "
        "fixes"
    )


def test_payload_threshold_crossed_fires_again_on_a_new_high_water_mark(monkeypatch, tmp_path):
    """Within the SAME process, once traffic pushes the observed max past
    what is already stored, the trigger must fire AGAIN -- this is the
    same durable-max comparison that makes the restart scenario work,
    exercised without a restart to isolate it as its own behaviour."""
    log_path = tmp_path / "cap.jsonl"
    g = _load_gateway(monkeypatch, log_path)
    cap = _capability(reranker_chars_s=1000.0, reranker_projected_s=10.0)

    asyncio.run(g._maybe_derive_capacity(cap))
    first_coord = _FakeCoordinator(successes=8, failures=0, chars_total=569112,
                                    docs_total=176, chars_max=101240)
    asyncio.run(g._maybe_derive_capacity(cap, first_coord))
    records = g._read_capacity_records_sync(log_path)
    assert len(records) == 2
    assert records[-1]["derived"]["payload_max_chars_measured"] == 101240

    # More traffic accumulates in the SAME process; a single bigger search
    # pushes the cumulative max higher.
    bigger_coord = _FakeCoordinator(successes=9, failures=0, chars_total=700000,
                                     docs_total=180, chars_max=200000)
    asyncio.run(g._maybe_derive_capacity(cap, bigger_coord))

    records = g._read_capacity_records_sync(log_path)
    assert len(records) == 3, "a genuinely larger observed max must re-derive"
    assert records[-1]["trigger"] == "payload_threshold_crossed"
    assert records[-1]["derived"]["payload_max_chars_measured"] == 200000


def test_payload_threshold_crossed_does_not_fire_below_threshold(monkeypatch, tmp_path):
    """Traffic below CAPACITY_PAYLOAD_MIN_SAMPLES must not trigger a
    re-derivation -- the stored basis stays theoretical until real
    traffic actually crosses the line."""
    log_path = tmp_path / "cap.jsonl"
    g = _load_gateway(monkeypatch, log_path)
    cap = _capability(reranker_chars_s=1000.0, reranker_projected_s=10.0)

    asyncio.run(g._maybe_derive_capacity(cap))
    below = _FakeCoordinator(successes=2, failures=1, chars_total=9000,
                              docs_total=6, chars_max=5000)
    asyncio.run(g._maybe_derive_capacity(cap, below))

    records = g._read_capacity_records_sync(log_path)
    assert len(records) == 1, "3 samples, below the default threshold of 5 -- must not fire"


def test_payload_threshold_crossed_requires_a_healthy_current_probe(monkeypatch, tmp_path):
    """Enough samples and a bigger max but a current probe that is NOT 'ok'
    this cycle must not fire (mirrors _build_capacity_record's own guard).
    Since there is no one-shot resource to spend, a later cycle with a
    healthy probe must still be able to fire it once the probe recovers --
    this falls out of the durable-comparison design for free (B-2)."""
    log_path = tmp_path / "cap.jsonl"
    g = _load_gateway(monkeypatch, log_path)
    healthy_cap = _capability(reranker_chars_s=1000.0, reranker_projected_s=10.0)
    unhealthy_cap = _capability(reranker_chars_s=1000.0, reranker_projected_s=10.0,
                                 reranker_status="failing")
    coord = _FakeCoordinator(successes=8, failures=0, chars_total=569112,
                              docs_total=176, chars_max=101240)

    asyncio.run(g._maybe_derive_capacity(healthy_cap))          # baseline
    asyncio.run(g._maybe_derive_capacity(unhealthy_cap, coord))  # samples ok, probe not

    records = g._read_capacity_records_sync(log_path)
    assert len(records) == 1, "an unhealthy current probe must not fire the trigger"

    asyncio.run(g._maybe_derive_capacity(healthy_cap, coord))   # probe recovers

    records = g._read_capacity_records_sync(log_path)
    assert len(records) == 2, "no one-shot resource was spent by the failed attempt"
    assert records[-1]["trigger"] == "payload_threshold_crossed"


def test_payload_threshold_crossed_does_not_fight_or_reorder_existing_triggers(monkeypatch, tmp_path):
    """When an existing trigger (probe_drift here) ALSO legitimately fires
    on the same cycle that crosses the payload threshold, the EXISTING
    trigger must win and take credit -- the new check runs strictly last,
    only when nothing else already decided to fire. The resulting record
    can still land on payload_basis 'measured' (the coordinator is passed
    to every trigger's record, not just this one) -- proving the two
    mechanisms cooperate rather than compete."""
    log_path = tmp_path / "cap.jsonl"
    g = _load_gateway(monkeypatch, log_path)
    baseline_cap = _capability(reranker_chars_s=1000.0, reranker_projected_s=10.0)
    drifted_cap = _capability(reranker_chars_s=2001.0, reranker_projected_s=10.0)  # just outside x2
    coord = _FakeCoordinator(successes=8, failures=0, chars_total=569112,
                              docs_total=176, chars_max=101240)

    asyncio.run(g._maybe_derive_capacity(baseline_cap))               # baseline
    asyncio.run(g._maybe_derive_capacity(drifted_cap, coord))          # both qualify at once

    records = g._read_capacity_records_sync(log_path)
    assert len(records) == 2
    assert records[-1]["trigger"] == "probe_drift", (
        "probe_drift is decided earlier in the trigger chain and must win"
    )
    assert records[-1]["derived"]["payload_basis"] == "measured", (
        "the coordinator still reaches the record produced by a DIFFERENT "
        "trigger -- the two mechanisms are not mutually exclusive"
    )


def test_all_empty_payload_never_fires_the_trigger(monkeypatch, tmp_path):
    """B-3 (LOW, fixed): a run of all-empty-content searches crosses the
    sample threshold (successes+failures) but leaves chars_max at 0, so
    _capacity_payload_stats reports max_chars_per_search as None. The fix
    requires that field to be non-None before firing at all -- unlike the
    OLD design (which used to fire once and still land on "theoretical"),
    the trigger must now never fire for this population, at any sample
    count, across any number of cycles."""
    log_path = tmp_path / "cap.jsonl"
    g = _load_gateway(monkeypatch, log_path)
    cap = _capability(reranker_chars_s=1000.0, reranker_projected_s=10.0)
    odd_coord = _FakeCoordinator(successes=8, failures=0, chars_total=0,
                                  docs_total=0, chars_max=0)

    asyncio.run(g._maybe_derive_capacity(cap))              # baseline
    for _ in range(4):
        asyncio.run(g._maybe_derive_capacity(cap, odd_coord))

    records = g._read_capacity_records_sync(log_path)
    assert len(records) == 1, (
        "an all-empty-payload population must never fire the trigger at "
        "all -- there is nothing here that could ever produce a real "
        "measured basis"
    )


# ── B-1 required regression test: the restart scenario ──────────────────────

def test_restart_scenario_rederives_when_live_max_exceeds_stored_max(monkeypatch, tmp_path):
    """The reviewer's own live reproduction, reproduced here without
    touching any operator state (module reload = fresh globals, fresh
    in-process counters; only the on-disk log path -- a tmp_path fixture,
    never ~/.shared-memory -- is shared between the two 'processes'):

      Process 1: first_derivation (theoretical) -> payload_threshold_
      crossed (measured, samples 10, max 100,000).

      Simulated restart: fresh module reload (fresh globals, fresh
      coordinator counters), SAME log file. Under the OLD design (latch +
      "last basis is theoretical") this produced ZERO new records even
      after real traffic with a LARGER max -- the bug (B-1, HIGH).

      Process 2: accumulates 12 real searches, observed max 150,000 (>
      the stored 100,000) -- must re-derive, and the served record must
      reflect process 2's own numbers, not process 1's."""
    log_path = tmp_path / "cap.jsonl"
    cap = _capability(reranker_chars_s=1000.0, reranker_projected_s=10.0)

    # Process 1.
    g1 = _load_gateway(monkeypatch, log_path)
    asyncio.run(g1._maybe_derive_capacity(cap))                    # first_derivation
    coord1 = _FakeCoordinator(successes=10, failures=0, chars_total=710000,
                               docs_total=220, chars_max=100000)
    asyncio.run(g1._maybe_derive_capacity(cap, coord1))            # payload_threshold_crossed
    process1_records = g1._read_capacity_records_sync(log_path)
    assert len(process1_records) == 2
    assert process1_records[-1]["derived"]["payload_max_chars_measured"] == 100000

    # Simulated restart: a fresh module reload gives fresh module-level
    # globals (_capacity_first_probe_done, _capacity_latest, etc. all reset
    # to their import-time defaults) and a brand-new (empty) fake
    # coordinator's counters below -- but reads/writes the SAME log_path.
    g2 = _load_gateway(monkeypatch, log_path)
    assert g2._capacity_first_probe_done is False, (
        "test setup invariant -- this must genuinely be a fresh process, "
        "not a continuation of process 1's state"
    )

    # Process 2's own first cycle: fingerprint unchanged -> no
    # gateway_start_fingerprint_mismatch, and no other trigger fires either
    # -- exactly the scenario that used to leave the record permanently
    # stuck under the old design.
    asyncio.run(g2._maybe_derive_capacity(cap))
    after_restart_only = g2._read_capacity_records_sync(log_path)
    assert len(after_restart_only) == 2, (
        "an unchanged fingerprint with no new traffic yet must not fire "
        "anything on its own"
    )

    # Process 2 accumulates 12 real searches with a LARGER observed max
    # (150,000 > the stored 100,000) -- the reviewer's own numbers.
    coord2 = _FakeCoordinator(successes=12, failures=0, chars_total=900000,
                               docs_total=264, chars_max=150000)
    asyncio.run(g2._maybe_derive_capacity(cap, coord2))

    records = g2._read_capacity_records_sync(log_path)
    assert len(records) == 3, (
        "the restarted process must be able to re-derive once its own "
        "traffic exceeds the dead process's stored max -- this is exactly "
        "the B-1 regression"
    )
    assert records[-1]["trigger"] == "payload_threshold_crossed"
    assert records[-1]["derived"]["payload_basis"] == "measured"
    assert records[-1]["derived"]["payload_max_chars_measured"] == 150000, (
        "the served record must reflect the LIVE process's observed max "
        "(150,000), not the dead process's (100,000)"
    )
    assert records[-1]["derived"]["payload_basis_sample_count"] == 12, (
        "and the live process's own sample count, not the dead process's 10"
    )


def test_restart_scenario_does_not_rederive_when_live_max_is_smaller(monkeypatch, tmp_path):
    """The other half of the restart fix: a restarted process whose fresh
    traffic has NOT yet exceeded the dead process's stored max must not
    regress the record to a smaller number -- the stored max is the more
    conservative (safer) figure until real traffic actually exceeds it."""
    log_path = tmp_path / "cap.jsonl"
    cap = _capability(reranker_chars_s=1000.0, reranker_projected_s=10.0)

    g1 = _load_gateway(monkeypatch, log_path)
    asyncio.run(g1._maybe_derive_capacity(cap))
    coord1 = _FakeCoordinator(successes=10, failures=0, chars_total=710000,
                               docs_total=220, chars_max=100000)
    asyncio.run(g1._maybe_derive_capacity(cap, coord1))
    assert len(g1._read_capacity_records_sync(log_path)) == 2

    g2 = _load_gateway(monkeypatch, log_path)
    coord2_small = _FakeCoordinator(successes=8, failures=0, chars_total=400000,
                                     docs_total=160, chars_max=60000)  # < 100,000
    asyncio.run(g2._maybe_derive_capacity(cap, coord2_small))

    records = g2._read_capacity_records_sync(log_path)
    assert len(records) == 2, (
        "a smaller observed max after a restart must not re-derive -- the "
        "larger, already-stored max stays as the more conservative reading"
    )
    assert records[-1]["derived"]["payload_max_chars_measured"] == 100000
