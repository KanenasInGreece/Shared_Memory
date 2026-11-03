"""v1.0.46 telemetry for rem_pending_clusters and rem_pending_incomplete.

N9. Dies when the two values are taken from different rows, when a missing
key on an older row hides a later finished cycle, or when the flag is cast
with int() (false would then equal 0 and still pass an equality check).
"""
import asyncio
import importlib.util
import os
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import coordinator as co
import telemetry_contract as tc

_signal_path = os.path.join(os.path.dirname(__file__), "test_consolidation_signal.py")
_spec = importlib.util.spec_from_file_location("_census_row_helper", _signal_path)
_signal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_signal)
_no_census_row = _signal._no_census_row


def _source():
    path = os.path.join(
        os.path.dirname(__file__), "..", "shared-memory", "scripts", "coordinator.py")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def test_t1_sql_reads_both_from_the_latest_finished_row():
    """N9. Both keys are the latest completed or crashed row of that cycle
    type, with the same FILTER and ORDER BY, not the latest row that merely
    contains the key. Dies when a failed first check can still serve an
    older count."""
    src = _source()
    clusters = (
        "(array_agg((extra->>'rem_pending_clusters')::int ORDER BY started_at DESC) "
        "FILTER (WHERE outcome IN ('completed','crashed')))[1] AS rem_pending_clusters"
    )
    flag = (
        "(array_agg((extra->>'rem_pending_incomplete')::boolean ORDER BY started_at DESC) "
        "FILTER (WHERE outcome IN ('completed','crashed')))[1] AS rem_pending_incomplete"
    )
    assert clusters in " ".join(src.split())
    assert flag in " ".join(src.split())
    start = src.index("_LATEST_EXTRA_KEYS = (")
    end = src.index(")", start)
    assert "rem_pending_clusters" in src[start:end]
    assert "rem_pending_incomplete" not in src[start:end]


def _serve(row):
    coord = co.MemoryCoordinator()
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[row])
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    coord._acquire = MagicMock(return_value=acq)
    return asyncio.run(coord._compute_consolidation_health())


def test_t2_flag_is_a_bool_and_the_count_is_an_int():
    """N9. The served flag is a bool for true and for false, and null is
    null. The count is an int. Dies when the flag goes through the int()
    output line: false would become 0, and type(v) is bool would fail even
    though false == 0."""
    for flag, count in ((True, 2), (False, 0), (None, None)):
        row = dict(_no_census_row("insight"),
                   rem_pending_clusters=count, rem_pending_incomplete=flag)
        out = _serve(row)["insight"]
        got_flag = out["rem_pending_incomplete"]
        got_count = out["rem_pending_clusters"]
        if flag is None:
            assert got_flag is None
            assert got_count is None
        else:
            assert type(got_flag) is bool
            assert got_flag is flag
            assert type(got_count) is int
            assert got_count == count


def test_contract_text_for_the_two_keys():
    """The contract names both keys as insight-only, and the flag is null
    exactly when the count is null."""
    clusters = tc.TELEMETRY["consolidation.*.rem_pending_clusters"]
    flag = tc.TELEMETRY["consolidation.*.rem_pending_incomplete"]
    assert clusters["types"] == ("int", "null")
    assert flag["types"] == ("bool", "null")
    assert clusters["since"] == "1.0.46"
    assert flag["since"] == "1.0.46"
    assert "at least this many, possibly more" in clusters["note"]
    assert "null exactly when rem_pending_clusters is null" in flag["note"]
    assert "overcounts a deferred component" in tc.TELEMETRY["nrem.decision_cycles"]["note"]
    assert "REM-processed" in tc.TELEMETRY["consolidation.*.eligible_clusters"]["note"]
