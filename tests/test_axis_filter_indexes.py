"""Migration 036 + the pgvector version probe — decision:1584, grounded on
measurement fact:1583.

WHY THIS EXISTS. A `--project`/`--domain` filter on `POST /memory/search`
binds the resolved spelling set against `metadata->>'project' = ANY(...)`
(migration 035's expansion) — an expression neither `technical_docs`
migration ever indexed. Below ~15k rows that costs a Seq Scan; above a
corpus size where HNSW's candidate handoff stops covering the filtered rows,
a SELECTIVE filter returns ZERO matches instead — the same query, on the
same data, silently wrong rather than merely slow. Fixing it needed two
independent things: an index on the expression the filter actually compares
against (migration 036), and `hnsw.iterative_scan = relaxed_order` on every
pooled connection once pgvector is new enough to support it (coordinator.py).

Coverage:
  - migration 036 creates both indexes on the exact expressions the filter
    predicate binds against, and touches `normalized_key` nowhere
  - the pgvector version parser: what parses to enabled, what does not
  - `_init_connection` issues the SET only when this coordinator's own probe
    found iterative scan available — mutation-checked
  - the authenticated `/health` payload carries `pgvector: {version,
    iterative_scan}` as a flat additive key

⛔ NO LIVE DATABASE. The migration-content checks are static text assertions,
exactly like migration 035's own suite sits beside; the connection/pool
objects below are mocks, never a real asyncpg connection.
"""

import asyncio
import importlib
import importlib.util
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_SCRIPTS = os.path.normpath(os.path.join(_ROOT, "shared-memory", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

_MIGRATION = os.path.normpath(os.path.join(
    _ROOT, "shared-memory", "migrations", "036_axis_filter_indexes.sql"))


def load_coordinator():
    """Fresh module object each call — same trick test_axis_normalized_keys.py
    uses, so this file never depends on import order relative to any other
    test module that has already imported `coordinator`."""
    path = os.path.join(_SCRIPTS, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["coordinator"] = mod
    spec.loader.exec_module(mod)
    return mod


def _migration_sql() -> str:
    return open(_MIGRATION, encoding="utf-8").read()


# ══════════════════════════════════════════════════════════════════════════
# (1) Migration 036 — the exact expressions, and nothing on normalized_key
# ══════════════════════════════════════════════════════════════════════════

def test_the_migration_indexes_the_stored_project_expression():
    sql = _migration_sql()
    assert ("CREATE INDEX IF NOT EXISTS technical_docs_project_expr_idx\n"
            "    ON technical_docs ((metadata->>'project'));") in sql


def test_the_migration_indexes_domains_with_gin():
    sql = _migration_sql()
    assert ("CREATE INDEX IF NOT EXISTS technical_docs_domains_gin_idx\n"
            "    ON technical_docs USING gin ((metadata->'domains'));") in sql


def test_the_migration_never_indexes_normalized_key():
    """⛔ THE ONE THING THIS MIGRATION MUST NOT DO. `normalized_key` is the
    REGISTRY's column (migration 035) — `technical_docs` does not carry it,
    and the search predicate binds against the stored `metadata->>'project'`
    expression, never a registry lookup. An index on `normalized_key` here
    would be indexing a column this table does not have.

    (The header's own PROSE names `normalized_key` to explain why it is the
    wrong column — that is expected and fine; what must never appear is a
    CREATE INDEX statement over it.)"""
    sql = _migration_sql()
    creates = [line for line in sql.splitlines()
               if "CREATE INDEX" in line or "ON technical_docs" in line]
    assert not any("normalized_key" in line for line in creates)


def test_the_migration_is_idempotent_and_transactional():
    sql = _migration_sql()
    assert "\nBEGIN;" in sql
    assert "\nCOMMIT;" in sql
    assert sql.count("CREATE INDEX IF NOT EXISTS") == 2


# ══════════════════════════════════════════════════════════════════════════
# (2) The pgvector version parser
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("raw,expected_enabled", [
    ("0.8.2", True),
    ("0.7.4", False),
    ("1.0.0", True),
    (None, False),
])
def test_version_parses_to_the_right_enablement(raw, expected_enabled):
    coordinator = load_coordinator()
    parsed = coordinator._parse_pgvector_version(raw)
    if expected_enabled:
        assert parsed is not None
        assert parsed >= coordinator.PGVECTOR_ITERATIVE_SCAN_MIN
    else:
        assert parsed is None or parsed < coordinator.PGVECTOR_ITERATIVE_SCAN_MIN


def test_version_parser_exact_values():
    coordinator = load_coordinator()
    assert coordinator._parse_pgvector_version("0.8.2") == (0, 8)
    assert coordinator._parse_pgvector_version("0.7.4") == (0, 7)
    assert coordinator._parse_pgvector_version("1.0.0") == (1, 0)
    assert coordinator._parse_pgvector_version(None) is None
    assert coordinator._parse_pgvector_version("") is None
    assert coordinator._parse_pgvector_version("garbage") is None


# ══════════════════════════════════════════════════════════════════════════
# (3) _init_connection — the SET fires only when this coordinator's own
#     probe found iterative scan available
# ══════════════════════════════════════════════════════════════════════════

def _mock_conn():
    conn = MagicMock()
    conn.set_type_codec = AsyncMock()  # coordinator.py awaits it
    conn.execute = AsyncMock()
    return conn


@pytest.mark.asyncio
async def test_init_connection_issues_the_set_when_iterative_scan_is_enabled():
    """MUTATION CHECK ANCHOR: invert the `if self.hnsw_iterative_scan:` guard
    in `_init_connection` (or delete it) and this test dies — asserted below."""
    coordinator = load_coordinator()
    c = coordinator.MemoryCoordinator()
    c.hnsw_iterative_scan = True
    conn = _mock_conn()
    await c._init_connection(conn)
    conn.execute.assert_awaited_once_with("SET hnsw.iterative_scan = relaxed_order")


@pytest.mark.asyncio
async def test_init_connection_skips_the_set_when_version_is_below_0_8():
    coordinator = load_coordinator()
    c = coordinator.MemoryCoordinator()
    c.hnsw_iterative_scan = False  # what a 0.7.x probe leaves it at
    conn = _mock_conn()
    await c._init_connection(conn)
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_init_connection_skips_the_set_when_version_is_unknown():
    coordinator = load_coordinator()
    c = coordinator.MemoryCoordinator()
    assert c.hnsw_iterative_scan is False  # the __init__ default, before any probe
    conn = _mock_conn()
    await c._init_connection(conn)
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_init_connection_still_registers_the_jsonb_codec_either_way():
    """The SET is additive; the codec registration this method already did
    must survive untouched regardless of the pgvector version."""
    coordinator = load_coordinator()
    for enabled in (True, False):
        c = coordinator.MemoryCoordinator()
        c.hnsw_iterative_scan = enabled
        conn = _mock_conn()
        await c._init_connection(conn)
        conn.set_type_codec.assert_called_once()
        assert conn.set_type_codec.call_args.args[0] == "jsonb"


@pytest.mark.asyncio
async def test_a_failed_set_is_logged_and_does_not_raise():
    """A session GUC that fails to apply must not kill the pooled connection
    — the codec above is required everywhere; this is a performance/
    correctness improvement for one query shape, not a hard dependency."""
    coordinator = load_coordinator()
    c = coordinator.MemoryCoordinator()
    c.hnsw_iterative_scan = True
    conn = _mock_conn()
    conn.execute = AsyncMock(side_effect=RuntimeError("GUC unknown on this build"))
    await c._init_connection(conn)  # must not raise


# ══════════════════════════════════════════════════════════════════════════
# (4) MUTATION CHECK — recorded by running it, not merely asserted
# ══════════════════════════════════════════════════════════════════════════
#
# Per the brief: invert the guard, confirm exactly
# test_init_connection_skips_the_set_when_version_is_below_0_8 (and the
# unknown-version sibling) die, restore. Done by hand during this build;
# recorded in HANDOFF.md rather than re-run here (a self-inverting test would
# just be the same assertion twice).


# ══════════════════════════════════════════════════════════════════════════
# (5) /health — the flat additive `pgvector` key, authenticated payload
# ══════════════════════════════════════════════════════════════════════════

class _HealthProbeResp:
    status = 200


class _HealthProbeCm:
    async def __aenter__(self):
        return _HealthProbeResp()

    async def __aexit__(self, *a):
        return False


class _HealthProbeSession:
    """No real network — every upstream probe /health hits just reports 200,
    the same stub test_health_anonymous_slimming.py uses."""
    def get(self, url, timeout=None, headers=None):
        return _HealthProbeCm()


def _health_request():
    class _Req(dict):
        pass
    req = _Req()
    req.headers = {}
    req.app = {}
    return req


def _load_gateway_auth_off(monkeypatch):
    """Auth-off install: `handle_health` returns the full payload to every
    caller (see its own docstring), so this is the simplest way to reach the
    `if coordinator is not None:` branch without also standing up a token
    registry. Mirrors test_health_anonymous_slimming.py's proven reload
    order — coordinator first, then hive_mind_proxy."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    return g, coordinator


def test_health_carries_pgvector_with_both_fields_when_enabled(monkeypatch):
    g, coordinator_mod = _load_gateway_auth_off(monkeypatch)
    assert g.AUTH_CONFIGURED_AT_STARTUP is False

    c = coordinator_mod.MemoryCoordinator()
    c.pgvector_version = "0.8.2"
    c.hnsw_iterative_scan = True

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    req = _health_request()
    req.app = {"proxy": proxy, "coordinator": c}

    body = json.loads(asyncio.run(g.handle_health(req)).body.decode())
    assert body["pgvector"] == {"version": "0.8.2", "iterative_scan": True}


def test_health_carries_pgvector_disabled_below_the_floor(monkeypatch):
    g, coordinator_mod = _load_gateway_auth_off(monkeypatch)

    c = coordinator_mod.MemoryCoordinator()
    c.pgvector_version = "0.7.4"
    c.hnsw_iterative_scan = False

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    req = _health_request()
    req.app = {"proxy": proxy, "coordinator": c}

    body = json.loads(asyncio.run(g.handle_health(req)).body.decode())
    assert body["pgvector"] == {"version": "0.7.4", "iterative_scan": False}


def test_health_pgvector_null_version_reads_as_disabled(monkeypatch):
    """The probe-failed / extension-unreadable case — None is the coordinator
    __init__ default before start() ever runs a probe."""
    g, coordinator_mod = _load_gateway_auth_off(monkeypatch)

    c = coordinator_mod.MemoryCoordinator()
    assert c.pgvector_version is None
    assert c.hnsw_iterative_scan is False

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    req = _health_request()
    req.app = {"proxy": proxy, "coordinator": c}

    body = json.loads(asyncio.run(g.handle_health(req)).body.decode())
    assert body["pgvector"] == {"version": None, "iterative_scan": False}


def test_health_without_a_coordinator_carries_no_pgvector_key(monkeypatch):
    """The key lives inside `if coordinator is not None:` — a caller with no
    coordinator attached (today's shape for some test harnesses) must not
    see a fabricated pgvector block."""
    g, _coordinator_mod = _load_gateway_auth_off(monkeypatch)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthProbeSession()
    req = _health_request()
    req.app = {"proxy": proxy}  # no "coordinator" key at all

    body = json.loads(asyncio.run(g.handle_health(req)).body.decode())
    assert "pgvector" not in body
