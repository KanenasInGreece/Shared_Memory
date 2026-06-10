"""Unit tests for the NREM global density sweep gating + re-queue backstop.

The sweep exists because the event-driven path only evaluates clusters touched
by a fresh save: clusters that cross the density threshold via REM enrichment
(rem_processed flips after the save notification was consumed) or while the
daemon was down never fire (retrospective on decision pg_id 214). These tests
cover the pure gating rule and the backstop-clock fix for re-queued work — no
DB or Neo4j required.
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
from consolidation_loop import sweep_due, ConsolidationDaemon

NOW = datetime(2026, 6, 10, 12, 0, 0)


def test_sweep_due_when_idle_and_interval_elapsed():
    assert sweep_due(
        NOW,
        last_sweep_time=NOW - timedelta(hours=2),
        last_activity=NOW - timedelta(minutes=5),
        has_pending=False,
        idle_threshold=60, sweep_interval=3600,
    )


def test_sweep_blocked_by_pending_entry_points():
    # Event-driven work takes priority — never sweep under it.
    assert not sweep_due(
        NOW,
        last_sweep_time=NOW - timedelta(hours=2),
        last_activity=NOW - timedelta(minutes=5),
        has_pending=True,
        idle_threshold=60, sweep_interval=3600,
    )


def test_sweep_blocked_during_active_save_window():
    assert not sweep_due(
        NOW,
        last_sweep_time=NOW - timedelta(hours=2),
        last_activity=NOW - timedelta(seconds=10),
        has_pending=False,
        idle_threshold=60, sweep_interval=3600,
    )


def test_sweep_blocked_before_interval_elapsed():
    assert not sweep_due(
        NOW,
        last_sweep_time=NOW - timedelta(minutes=30),
        last_activity=NOW - timedelta(minutes=5),
        has_pending=False,
        idle_threshold=60, sweep_interval=3600,
    )


def test_first_sweep_fires_on_startup():
    # __init__ seeds last_sweep_time = datetime.min so the first idle tick
    # drains clusters that became eligible while the daemon was down.
    assert sweep_due(
        NOW,
        last_sweep_time=datetime.min,
        last_activity=NOW - timedelta(minutes=5),
        has_pending=False,
        idle_threshold=60, sweep_interval=3600,
    )


def test_daemon_seeds_startup_sweep():
    daemon = ConsolidationDaemon()
    assert daemon.last_sweep_time == datetime.min


def test_requeue_starts_backstop_clock():
    # Re-queued failed work previously had first_notification_time=None, so
    # the MAX_DEFERRAL backstop never armed and sustained GPU activity could
    # defer it forever.
    daemon = ConsolidationDaemon()
    assert daemon.first_notification_time is None
    daemon._requeue([1, 2, 3])
    assert daemon.pending_pg_ids == {1, 2, 3}
    assert daemon.first_notification_time is not None


def test_requeue_does_not_reset_running_clock():
    daemon = ConsolidationDaemon()
    t0 = datetime(2026, 6, 10, 11, 0, 0)
    daemon.first_notification_time = t0
    daemon.pending_pg_ids = {7}
    daemon._requeue([8, 9])
    assert daemon.first_notification_time == t0
    assert daemon.pending_pg_ids == {7, 8, 9}


def test_requeue_empty_is_noop():
    daemon = ConsolidationDaemon()
    daemon._requeue([])
    assert daemon.pending_pg_ids == set()
    assert daemon.first_notification_time is None
