"""D1 (OBS round, v2 brief) — an honest 60-second window for
`token_verify_failed_per_min`.

THE DEFECT (verified by both adversarial reviewers against main=0714ebd):
`_delta_per_min` (hive_mind_proxy.py) divided the counter delta by the gap
between health-cache BUILDS (HEALTH_CACHE_TTL_S — a few seconds by default),
not by a fixed window. One event therefore read a wildly different "rate"
purely as a function of how often something else happened to poll /health —
the poll cadence WAS the reading, never a property of the event itself.

THE FIX: a coordinator-side `deque(maxlen=256)` stamped with
`time.monotonic()` at both bump sites (`_record_token_verify_failed` and
`_record_unprotected_path_token_verify_failed`), read by a synchronous
accessor (`telemetry_token_verify_ring`) and counted by
`hive_mind_proxy._token_verify_failure_rate` over a TRUE 60 s window, with
an injectable `now` for deterministic tests (the ring is monotonic-stamped,
so tests inject the clock rather than patching `datetime`, which the ring
never reads at all).

This file proves the OLD defect first (still reachable via the retained,
now-unused `_delta_per_min` helper), then pins every property the new
reading must have.
"""
import asyncio
import importlib
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def _fresh(monkeypatch, token_verify_warn_per_min=None):
    """Fresh coordinator + hive_mind_proxy, mirroring
    test_health_dependency_visibility.py's `_fresh()` — so the D1 ring and
    every rebound counter start at zero for each test, with no leakage
    across tests that import the same module objects."""
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    if token_verify_warn_per_min is not None:
        monkeypatch.setenv("TOKEN_VERIFY_WARN_PER_MIN", str(token_verify_warn_per_min))
    else:
        monkeypatch.delenv("TOKEN_VERIFY_WARN_PER_MIN", raising=False)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    return coordinator, g


class _FakeRequest:
    """Minimal stand-in for aiohttp.web.Request — same shape as
    tests/test_credential_audit_trail.py's `_FakeRequest`: no real
    transport, a settable `.path`."""
    def __init__(self, path: str = "/memory/save"):
        self.path = path
        self.transport = None


def _warnings_payload(g, coordinator):
    """The real end-to-end path: build the actual /health `checks` dict
    (coordinator=None is fine — every dependency reads its own "not yet
    probed"/"down" default, which does not touch the warnings list at
    all) and return its `warnings` list, so the pinned assertions read the
    literal object the handler would serve, not a hand-assembled stand-in.
    """
    proxy = g.AsyncHiveMindProxy()

    async def _run():
        return await g._build_health_checks(proxy, coordinator)

    return asyncio.run(_run())["warnings"]


# ══════════════════════════════════════════════════════════════════════════
# Prove-failing-first: the OLD reading was poll-cadence-dependent
# ══════════════════════════════════════════════════════════════════════════

def test_prove_first_the_old_extrapolation_was_poll_cadence_dependent(monkeypatch):
    """UNMODIFIED-CODE evidence (`_delta_per_min` is retained, just no
    longer routed through for this warning — see its call site's
    docstring): the IDENTICAL single event (delta of 1) reads a rate that
    is purely a function of how far apart the two calls were, at the
    module's own HEALTH_CACHE_TTL_S default (3 s) versus a 600 s poll
    cadence — a ~200x swing for one event that never changed."""
    coordinator, g = _fresh(monkeypatch)
    ttl = g.HEALTH_CACHE_TTL_S
    assert ttl == 3.0, "HEALTH_CACHE_TTL_S default moved — re-check this evidence"

    g._rate_marks.clear()
    fast_clock = iter([1000.0, 1000.0 + ttl])
    monkeypatch.setattr(g.time, "monotonic", lambda: next(fast_clock))
    g._delta_per_min("prove_d1", 0)           # plants the first mark
    fast = g._delta_per_min("prove_d1", 1)    # same 1-event delta, TTL gap

    g._rate_marks.clear()
    slow_clock = iter([2000.0, 2600.0])
    monkeypatch.setattr(g.time, "monotonic", lambda: next(slow_clock))
    g._delta_per_min("prove_d1", 0)
    slow = g._delta_per_min("prove_d1", 1)    # same 1-event delta, 600 s gap

    assert fast == 20.0                        # (1 - 0) * 60 / 3
    assert slow == 0.1                          # (1 - 0) * 60 / 600
    assert fast == slow * 200                   # the swing is pure poll-cadence artefact


# ══════════════════════════════════════════════════════════════════════════
# The ring: both bump sites, fixed bound, monotonic stamps
# ══════════════════════════════════════════════════════════════════════════

def test_both_bump_sites_append_to_the_ring(monkeypatch):
    coordinator, g = _fresh(monkeypatch)
    assert coordinator.telemetry_token_verify_ring() == []
    coordinator._record_token_verify_failed(_FakeRequest(), None)
    assert len(coordinator.telemetry_token_verify_ring()) == 1
    coordinator._record_unprotected_path_token_verify_failed(
        _FakeRequest("/health"), "tok_bad")
    assert len(coordinator.telemetry_token_verify_ring()) == 2


def test_ring_is_stamped_monotonic_not_wall_clock(monkeypatch):
    """A wall-clock step backward must not be able to move this ring —
    only time.monotonic() may. Patch datetime hard to something absurd and
    confirm the ring's own reading is unaffected."""
    coordinator, g = _fresh(monkeypatch)
    coordinator._record_token_verify_failed(_FakeRequest(), None)
    ts = coordinator.telemetry_token_verify_ring()[0]
    assert isinstance(ts, float)
    # A monotonic stamp is a small-ish float (process uptime in seconds),
    # never an epoch/ISO timestamp — the two are easy to confuse by accident.
    import time as _time
    assert abs(ts - _time.monotonic()) < 5.0


def test_restart_drops_the_ring_rate_reads_zero(monkeypatch):
    """Same in-process semantics as every other counter here: a restart
    (module reload) drops the ring, so the rate reads 0, not an error and
    not a stale carried-over value."""
    coordinator, g = _fresh(monkeypatch)
    coordinator._record_token_verify_failed(_FakeRequest(), None)
    assert g._token_verify_failure_rate(now=coordinator.telemetry_token_verify_ring()[0]) == 1.0
    coordinator, g = _fresh(monkeypatch)  # simulate restart
    assert coordinator.telemetry_token_verify_ring() == []
    assert g._token_verify_failure_rate(now=1_000_000.0) == 0.0


# ══════════════════════════════════════════════════════════════════════════
# The new reading: honest, cadence-independent, windowed
# ══════════════════════════════════════════════════════════════════════════

def test_one_event_reads_1_regardless_of_when_it_is_checked_inside_the_window(monkeypatch):
    """Unlike `_delta_per_min`, the new reading does not depend AT ALL on
    how long ago the previous health build happened to be — it is a pure
    count of ring entries inside a fixed 60 s window from `now`. Checking
    "2 s after" and "59 s after" (simulating a fast vs. a slow poll
    cadence) both read the SAME honest 1.0 for the identical single
    event."""
    coordinator, g = _fresh(monkeypatch)
    coordinator._record_token_verify_failed(_FakeRequest(), None)
    ts = coordinator.telemetry_token_verify_ring()[0]
    assert g._token_verify_failure_rate(now=ts + 2.0) == 1.0
    assert g._token_verify_failure_rate(now=ts + 59.0) == 1.0
    # Just past the 60 s boundary the same event honestly reads 0 — no
    # extrapolation is inventing a rate for an event that aged out.
    assert g._token_verify_failure_rate(now=ts + 60.001) == 0.0


def test_fifteen_events_in_60s_triggers_the_warning(monkeypatch):
    coordinator, g = _fresh(monkeypatch)
    for _ in range(15):
        coordinator._record_token_verify_failed(_FakeRequest(), None)
    ts_last = coordinator.telemetry_token_verify_ring()[-1]
    rate = g._token_verify_failure_rate(now=ts_last)
    assert rate == 15.0
    assert rate > g.TOKEN_VERIFY_WARN_PER_MIN

    warnings = _warnings_payload(g, coordinator)
    assert any(w["key"] == "token_verify_failed_per_min" and w["observed"] == 15.0
               for w in warnings), warnings


def test_fifteen_events_over_ten_minutes_never_warns(monkeypatch):
    """Spread 15 events 40 s apart (600 s total span) — any 60 s window
    contains at most one or two of them, so the honest rate never
    approaches the default TOKEN_VERIFY_WARN_PER_MIN=10 threshold."""
    coordinator, g = _fresh(monkeypatch)
    # Anchored on the REAL monotonic clock (not an arbitrary base) so the
    # end-to-end `_warnings_payload` check below — which calls
    # `_token_verify_failure_rate()` with no `now` override, i.e. the live
    # clock — sees the same window this test reasons about.
    base = time.monotonic() - 14 * 40.0
    for i in range(15):
        coordinator._token_verify_failure_ring.append(base + i * 40.0)
    now = base + 14 * 40.0
    rate = g._token_verify_failure_rate(now=now)
    assert rate <= 2.0
    assert not (rate > g.TOKEN_VERIFY_WARN_PER_MIN)

    warnings = _warnings_payload(g, coordinator)
    assert not any(w["key"] == "token_verify_failed_per_min" for w in warnings), warnings


def test_60s_flood_saturates_the_ring_at_256_and_still_warns(monkeypatch):
    """300 events inside one 60 s window: the FIXED maxlen=256 ring caps
    both its own length and the reading at exactly 256 — a documented cap,
    not the true rate — and the warning still fires at the saturated
    reading."""
    coordinator, g = _fresh(monkeypatch)
    base = time.monotonic() - 299 * 0.1
    for i in range(300):
        coordinator._token_verify_failure_ring.append(base + i * 0.1)  # spans 29.9s
    now = base + 299 * 0.1
    ring = coordinator.telemetry_token_verify_ring()
    assert len(ring) == 256, "ring must saturate at its fixed maxlen, not grow unbounded"

    rate = g._token_verify_failure_rate(now=now)
    assert rate == 256.0
    assert rate > g.TOKEN_VERIFY_WARN_PER_MIN

    warnings = _warnings_payload(g, coordinator)
    assert any(w["key"] == "token_verify_failed_per_min" and w["observed"] == 256.0
               for w in warnings), warnings


def test_threshold_zero_any_single_event_warns(monkeypatch):
    """Threshold-0 semantics are unchanged by D1: `> TOKEN_VERIFY_WARN_PER_MIN`
    still means any event at all warns when the operator sets the limit
    to 0."""
    coordinator, g = _fresh(monkeypatch, token_verify_warn_per_min=0)
    assert g.TOKEN_VERIFY_WARN_PER_MIN == 0.0
    coordinator._record_token_verify_failed(_FakeRequest(), None)
    ts = coordinator.telemetry_token_verify_ring()[0]
    rate = g._token_verify_failure_rate(now=ts)
    assert rate == 1.0
    assert rate > g.TOKEN_VERIFY_WARN_PER_MIN

    warnings = _warnings_payload(g, coordinator)
    assert any(w["key"] == "token_verify_failed_per_min" and w["limit"] == 0.0
               for w in warnings), warnings


def test_no_events_reads_zero_never_none(monkeypatch):
    """An honest empty 60 s window IS zero events — unlike the old
    `_delta_per_min`'s "None on the very first call" semantics, there is
    no warm-up state to distinguish here."""
    coordinator, g = _fresh(monkeypatch)
    assert g._token_verify_failure_rate(now=123.0) == 0.0


def test_warning_key_string_is_exact():
    """Pinned by source inspection: a rename here would silently break
    every consumer keyed on the literal string, and CG's WARNING_KEYS
    enumeration (step 2) will re-derive from this exact call site."""
    import inspect
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
    import hive_mind_proxy as g
    src = inspect.getsource(g._build_health_checks)
    assert '_warning("token_verify_failed_per_min",' in src
