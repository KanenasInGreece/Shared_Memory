"""Pure measurement primitives for the telemetry contract (v0.9.74).

⛔ NOTHING IN HERE MAY RAISE INTO A WORK PATH. Every recorder is called from
inside a request or a daemon loop that has its own job to do, and a metric
that changes its caller's failure modes is worse than no metric at all
(CLAUDE.md: "adding a metric to a work path changes that path's failure
modes"). The two rules this module exists to make cheap to obey:

  * ``LatencyRing.record`` and ``Counter.bump`` swallow EVERY exception —
    they are wrapped internally, so a caller does not have to remember a
    try/except at each of the two dozen call sites.
  * Nothing here awaits, opens a connection, or reads a clock the caller has
    not already read. A ring is a bounded ``deque``; a percentile is a sort
    over at most ``maxlen`` floats.

No DB driver, no aiohttp, no coordinator import — so this module is unit
testable with nothing running, and so importing it can never drag a driver
into a process that does not have one (the ``nrem_gate`` precedent).
"""

from __future__ import annotations

from collections import deque
from typing import Iterable

__all__ = [
    "percentile",
    "LatencyRing",
    "Counter",
    "safe",
]


def percentile(values: Iterable[float], q: float) -> float | None:
    """Linear-interpolated percentile — the same definition Postgres'
    ``percentile_cont`` uses, so a ring percentile and a SQL percentile in the
    same payload mean the same thing.

    ``q`` is a fraction in [0, 1]. Returns None for an empty input rather than
    0.0: a ring nobody has written to has NOT measured a zero-millisecond
    call, and reporting 0 would make "never measured" read as "instantaneous"
    — the absence-is-not-zero rule this repo has paid for more than once.

    Pure; never raises on a well-formed input, and clamps ``q`` rather than
    rejecting it (a caller passing 0.95 as 95 gets p100, not an exception in
    a work path).
    """
    xs = sorted(float(v) for v in values)
    if not xs:
        return None
    if q <= 0:
        return xs[0]
    if q >= 1:
        return xs[-1]
    pos = q * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] + (xs[hi] - xs[lo]) * frac


class LatencyRing:
    """A bounded observation window plus the two counters that make it
    readable: how many calls were measured, and how many FAILED.

    ⚠ ERRORS ARE COUNTED, NEVER TIMED INTO THE WINDOW. A failing call is
    usually fast (a connection refused returns in microseconds) or pinned to a
    timeout ceiling — either way it describes the failure, not the service, and
    letting it into the latency window makes an outage read as a latency
    improvement. ``record`` is for successes; ``record_error`` is for the rest.

    ``window`` in the snapshot is the number of observations the percentiles
    were computed over, NOT the ring's capacity — a reader must be able to tell
    "p95 over 200 calls" from "p95 over 3 calls", and only the former is a
    percentile in any useful sense.
    """

    __slots__ = ("_ring", "calls", "errors", "last_ms", "max_ms", "last_payload_chars")

    def __init__(self, maxlen: int = 200):
        self._ring: deque = deque(maxlen=max(1, int(maxlen)))
        self.calls = 0
        self.errors = 0
        self.last_ms: float | None = None
        self.max_ms: float | None = None
        self.last_payload_chars: int | None = None

    def record(self, ms: float, payload_chars: int | None = None) -> None:
        """Record one SUCCESSFUL call. Never raises (see module docstring)."""
        try:
            v = float(ms)
            self._ring.append(v)
            self.calls += 1
            self.last_ms = v
            if self.max_ms is None or v > self.max_ms:
                self.max_ms = v
            if payload_chars is not None:
                self.last_payload_chars = int(payload_chars)
        except Exception:
            pass

    def record_error(self) -> None:
        """Record one FAILED call. Never raises, never enters the window."""
        try:
            self.errors += 1
        except Exception:
            pass

    def snapshot(self) -> dict:
        """The documented shape. ``max_ms`` is over this PROCESS's lifetime,
        not the ring — a capacity signal needs the worst call actually seen,
        which a window that has since rolled past it can no longer report."""
        vals = list(self._ring)
        return {
            "calls": self.calls,
            "errors": self.errors,
            "p50_ms": _r(percentile(vals, 0.5)),
            "p95_ms": _r(percentile(vals, 0.95)),
            "max_ms": _r(self.max_ms),
            "last_ms": _r(self.last_ms),
            "last_payload_chars": self.last_payload_chars,
            "window": len(vals),
        }


class Counter:
    """A named set of monotonic counters with a paired last-event timestamp.

    The pairing is the point (fact:1314's shape): a bare count resets with the
    process, so a poll-delta INVERTS across a restart and reads as "never
    happened" while the event was minutes ago. Stamped at the increment so the
    pair can never disagree.
    """

    __slots__ = ("_counts", "_last_ts")

    def __init__(self, keys: Iterable[str] = ()):
        self._counts: dict = {k: 0 for k in keys}
        self._last_ts: dict = {k: None for k in keys}

    def bump(self, key: str, ts: str | None = None, n: int = 1) -> None:
        """Increment one counter. Never raises (see module docstring)."""
        try:
            self._counts[key] = self._counts.get(key, 0) + int(n)
            if ts is not None:
                self._last_ts[key] = ts
        except Exception:
            pass

    def total(self) -> int:
        try:
            return sum(self._counts.values())
        except Exception:
            return 0

    def snapshot(self) -> dict:
        """{key: count} only. The timestamps are served by ``last_ts`` so a
        consumer that wants counts alone is not handed a nested shape it has to
        walk — and so a documented key path stays a leaf."""
        return dict(self._counts)

    def last_ts(self) -> dict:
        return dict(self._last_ts)


def safe(fn, *args, default=None, **kwargs):
    """Call ``fn`` and swallow anything it raises, returning ``default``.

    For the recording sites that are not a bare ring/counter bump — a snapshot
    assembled from several sources, a division, a dict comprehension over live
    state. The alternative is a try/except at every call site, which is exactly
    the thing that gets forgotten at the twenty-fourth one.
    """
    try:
        return fn(*args, **kwargs)
    except Exception:
        return default


def _r(v: float | None) -> float | None:
    return round(v, 1) if v is not None else None
