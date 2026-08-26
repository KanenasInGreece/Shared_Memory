"""Tests for coordinator.render_rem_by_model and the _latency_telemetry SQL
that feeds it (fact:1621).

Problem: technical_docs.rem_timing only carries a `service_ms` key when the
LLM response included llama.cpp's proprietary `timings` block. An
OpenAI-compatible external backend returns no such block, so its rows have
`service_ms: null` while `wall_ms`/`backend` are populated — the old
`WHERE ... (rem_timing->>'service_ms') IS NOT NULL` filter silently dropped
every external model from `by_model`. This file locks two things: the pure
rendering function keeps legacy keys unchanged for server-timed rows and adds
a wall-only row instead of dropping it, and the SQL string itself no longer
filters on service_ms.
"""
import inspect
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import coordinator  # noqa: E402


class _Row(dict):
    """Mapping-like stand-in for an asyncpg Record — supports r["key"]."""


def _server_row(**overrides):
    row = {
        "model": "gemma-4-12b",
        "n": 40,
        "n_service": 40,
        "max_batch": 5,
        "svc_p50": 920.0, "svc_p95": 1500.0,
        "con_p50": 10.0, "con_p95": 80.0,
        "wall_p50": 950.0, "wall_p95": 1600.0,
        "backend": "http://localhost:5000",
    }
    row.update(overrides)
    return _Row(row)


def _wall_only_row(**overrides):
    row = {
        "model": "ext-model",
        "n": 12,
        "n_service": 0,
        "max_batch": None,
        "svc_p50": None, "svc_p95": None,
        "con_p50": None, "con_p95": None,
        "wall_p50": 2100.0, "wall_p95": 3400.0,
        "backend": "https://api.example.com",
    }
    row.update(overrides)
    return _Row(row)


def test_server_timed_row_keeps_legacy_values_and_adds_new_keys():
    rows = [_server_row()]
    out = coordinator.render_rem_by_model(rows)
    assert len(out) == 1
    entry = out[0]
    # legacy keys, same values as before this change
    assert entry["model"] == "gemma-4-12b"
    assert entry["n"] == 40
    assert entry["max_batch_size"] == 5
    assert entry["service_ms"] == {"p50": 920.0, "p95": 1500.0}
    assert entry["contention_ms"] == {"p50": 10.0, "p95": 80.0}
    # new keys
    assert entry["wall_ms"] == {"p50": 950.0, "p95": 1600.0}
    assert entry["n_service"] == 40
    assert entry["backend"] == "http://localhost:5000"
    assert entry["timing_source"] == "server"


def test_wall_only_row_is_present_not_dropped():
    # This is the regression this whole change exists for: before the fix,
    # a row like this (n_service == 0, service/contention both None) never
    # reached the SELECT at all because of the service_ms filter. Here it
    # is fed straight into the pure renderer to lock the rendering half of
    # the fix independently of the SQL half.
    rows = [_wall_only_row()]
    out = coordinator.render_rem_by_model(rows)
    assert len(out) == 1
    entry = out[0]
    assert entry["model"] == "ext-model"
    assert entry["service_ms"] == {"p50": None, "p95": None}
    assert entry["contention_ms"] == {"p50": None, "p95": None}
    # mutation-check-style: assert the literal value on at least one side,
    # never only an equality between two expressions (fact:1309).
    assert entry["wall_ms"]["p50"] == 2100.0
    assert entry["wall_ms"]["p95"] == 3400.0
    assert entry["n_service"] == 0
    assert entry["backend"] == "https://api.example.com"
    assert entry["timing_source"] == "wall"


def test_ordering_of_input_rows_is_preserved():
    rows = [_server_row(model="a"), _wall_only_row(model="b"), _server_row(model="c")]
    out = coordinator.render_rem_by_model(rows)
    assert [e["model"] for e in out] == ["a", "b", "c"]


def test_none_percentile_stays_none_never_invented():
    # A row with n_service == 0 but ALSO no wall percentiles (degenerate,
    # should not happen live but must not crash or fabricate a number).
    row = _wall_only_row(wall_p50=None, wall_p95=None)
    out = coordinator.render_rem_by_model([row])
    assert out[0]["wall_ms"] == {"p50": None, "p95": None}
    assert out[0]["timing_source"] == "wall"


def test_latency_telemetry_sql_no_longer_filters_on_service_ms():
    # Regression lock: read the ACTUAL method source (not a copy in this
    # test) so a future edit reintroducing the service_ms filter fails here.
    src = inspect.getsource(coordinator.MemoryCoordinator._latency_telemetry)
    assert "service_ms') IS NOT NULL" not in src
    assert "wall_ms') IS NOT NULL" in src


# ── F1+F6: timing_source is three-valued (n now counts wall rows, not ──────────
#    service samples, so n_service can be strictly between 0 and n) ────────────

def test_timing_source_server_when_n_service_equals_n():
    row = _server_row(n=40, n_service=40)
    out = coordinator.render_rem_by_model([row])
    assert out[0]["n"] == 40
    assert out[0]["n_service"] == 40
    assert out[0]["timing_source"] == "server"


def test_timing_source_wall_when_n_service_zero():
    row = _wall_only_row(n=12, n_service=0)
    out = coordinator.render_rem_by_model([row])
    assert out[0]["n"] == 12
    assert out[0]["n_service"] == 0
    assert out[0]["timing_source"] == "wall"


def test_timing_source_mixed_when_n_service_between_zero_and_n():
    # Literal inputs from the coordinator's brief: a model served mostly by an
    # external backend, with one row that happened to carry server timings.
    row = _server_row(n=500, n_service=1)
    out = coordinator.render_rem_by_model([row])
    assert out[0]["n"] == 500
    assert out[0]["n_service"] == 1
    assert out[0]["timing_source"] == "mixed"


# ── F2: the pure renderer reads exactly the aliases the SQL produces ───────────

def test_render_rem_by_model_keys_match_sql_aliases():
    src = inspect.getsource(coordinator.MemoryCoordinator._latency_telemetry)
    marker = "rem_rows = await conn.fetch("
    marker_start = src.index(marker)
    # Slice ONLY the rem_rows query, not the later NREM `cyc` query — find the
    # call's own matching close-paren by depth-counting from the "(" that
    # opens conn.fetch(...), rather than the first ")" (which lands inside
    # the SQL text itself, e.g. "count(*)"'s close-paren, far too early).
    open_idx = marker_start + len(marker) - 1
    assert src[open_idx] == "("
    depth = 0
    end = None
    for i in range(open_idx, len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    assert end is not None, "did not find the matching close-paren for conn.fetch("
    rem_rows_src = src[marker_start:end]
    assert "cyc" not in rem_rows_src  # sanity: didn't overrun into the NREM query
    aliases = set(re.findall(r"AS (\w+)", rem_rows_src))
    assert aliases == {
        "model", "n", "n_service", "max_batch",
        "svc_p50", "svc_p95", "con_p50", "con_p95",
        "wall_p50", "wall_p95", "backend",
    }
    # And confirm the renderer reads exactly this set of row keys — not a
    # derived/recomputed one — by exercising it with a row built from
    # precisely these keys.
    row = _Row({a: None for a in aliases})
    row.update({"model": "m", "n": 1, "n_service": 0, "max_batch": None,
                "svc_p50": None, "svc_p95": None, "con_p50": None, "con_p95": None,
                "wall_p50": None, "wall_p95": None, "backend": None})
    out = coordinator.render_rem_by_model([row])
    assert out[0]["model"] == "m"


# ── F9: n_service=None and backend=None degrade honestly ───────────────────────

def test_n_service_none_renders_as_wall_with_zero():
    row = _wall_only_row(n_service=None)
    out = coordinator.render_rem_by_model([row])
    assert out[0]["timing_source"] == "wall"
    assert out[0]["n_service"] == 0


def test_backend_none_passes_through_as_none():
    row = _server_row(backend=None)
    out = coordinator.render_rem_by_model([row])
    assert out[0]["backend"] is None
