"""Fix batch A (v1.0.29): default connection strings encode the password, project
registration absorbs a spelling race, ENTITY_CONFUSABLE_SIMILARITY is validated, and
`_may_steer_llm` uses the effective role."""
import ast
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _ROOT / "shared-memory" / "scripts"
_MIGRATIONS_DIR = _ROOT / "shared-memory" / "migrations"
sys.path.insert(0, str(_SCRIPTS_DIR))

import secure_env  # noqa: E402

NASTY = "p@ss w:rd/+x%"


# -- helpers -------------------------------------------------------------------

def _load(monkeypatch, filename: str, *, password=NASTY, pg_conn=None):
    """Load a script as a fresh module with PG_CONN genuinely unset (or set)."""
    monkeypatch.delenv("PG_CONN", raising=False)
    secure_env._secrets.pop("PG_CONN", None)
    secure_env._secrets.pop("PG_PASSWORD", None)
    monkeypatch.setenv("PG_PASSWORD", password)
    if pg_conn is not None:
        monkeypatch.setenv("PG_CONN", pg_conn)
    spec = importlib.util.spec_from_file_location(
        f"batch_a_{filename[:-3]}", _SCRIPTS_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _decoded_password(dsn: str) -> str:
    from psycopg2.extensions import parse_dsn
    return parse_dsn(dsn)["password"]


# -- A1 ------------------------------------------------------------------------

def test_pg_dsn_round_trips_through_both_drivers():
    from psycopg2.extensions import parse_dsn
    parsed = parse_dsn(secure_env.pg_dsn(NASTY))
    assert parsed["user"] == "postgres"
    assert parsed["password"] == NASTY
    assert parsed["host"] == "localhost"
    assert parsed["port"] == "5432"
    assert parsed["dbname"] == "agent_data"


def test_pg_dsn_round_trips_through_asyncpg():
    from asyncpg import connect_utils
    parse = getattr(connect_utils, "_parse_connect_dsn_and_args", None)
    if parse is None:
        pytest.skip("asyncpg's DSN parser is not callable in this environment")
    kwargs = dict(dsn=secure_env.pg_dsn(NASTY), host=None, port=None, user=None,
                  password=None, passfile=None, database=None, ssl=None,
                  direct_tls=None, server_settings=None, target_session_attrs=None,
                  krbsrvname=None, gsslib=None, service=None, servicefile=None)
    try:
        _addrs, params = parse(**kwargs)
    except TypeError:
        pytest.skip("asyncpg's DSN parser signature differs in this version")
    assert params.password == NASTY


def test_pg_dsn_plain_password_is_byte_identical_to_the_old_form():
    assert secure_env.pg_dsn("plainpass1") == (
        "postgresql://postgres:plainpass1@localhost:5432/agent_data")


DAEMONS = [
    ("coordinator.py", "PG_DSN"),
    ("consolidation_loop.py", "PG_CONN"),
    ("rem_loop.py", "PG_CONN"),
]


@pytest.mark.parametrize("filename,attr", DAEMONS)
def test_daemon_default_dsn_decodes_the_exact_password(monkeypatch, filename, attr):
    mod = _load(monkeypatch, filename)
    assert _decoded_password(getattr(mod, attr)) == NASTY


@pytest.mark.parametrize("filename,attr", DAEMONS)
def test_an_explicit_pg_conn_is_used_verbatim(monkeypatch, filename, attr):
    raw = "postgresql://u:pw@db.internal:5433/custom"
    mod = _load(monkeypatch, filename, pg_conn=raw)
    assert getattr(mod, attr) == raw


def _builds_postgres_url_by_fstring(tree: ast.AST) -> bool:
    """A JoinedStr whose literal text opens a postgresql:// URL and which carries a
    password: either the `@` is in the same literal, or the f-string interpolates a
    password expression."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr) or not node.values:
            continue
        first = node.values[0]
        if not (isinstance(first, ast.Constant)
                and str(first.value).startswith("postgresql://")):
            continue
        rendered = ast.unparse(node)
        if "@" in rendered or "PG_PASSWORD" in rendered:
            return True
    return False


def test_no_script_builds_a_postgres_url_with_a_password_by_fstring():
    offenders = []
    for path in sorted(_SCRIPTS_DIR.glob("*.py")):
        if path.name in ("secure_env.py", "log_hygiene.py"):
            continue
        if _builds_postgres_url_by_fstring(ast.parse(path.read_text())):
            offenders.append(path.name)
    assert offenders == []


def test_the_source_guard_catches_a_hand_built_dsn():
    multi = ast.parse(
        "x = (f\"postgresql://{u}:\"\n"
        "     f\"{get('PG_PASSWORD')}@{h}:5432/db\")")
    single = ast.parse('y = f"postgresql://postgres:{p}@localhost:5432/agent_data"')
    assert _builds_postgres_url_by_fstring(multi)
    assert _builds_postgres_url_by_fstring(single)


def test_migration_tools_decode_the_exact_password(monkeypatch):
    sys.path.insert(0, str(_MIGRATIONS_DIR))
    import apply as apply_mod
    import generate_schema_init as gen_mod
    import verify_schema_init as ver_mod
    monkeypatch.delenv("PG_CONN", raising=False)
    monkeypatch.setenv("PG_PASSWORD", NASTY)
    assert _decoded_password(apply_mod._pg_conn()) == NASTY
    assert _decoded_password(gen_mod._pg_conn()) == NASTY
    assert _decoded_password(ver_mod._dsn("testdb")) == NASTY


# -- A2 ------------------------------------------------------------------------

def _coordinator(monkeypatch):
    monkeypatch.setenv("PG_PASSWORD", "x")
    import coordinator
    return coordinator


def _registering_coord(coordinator, insert_result, key_result=None, raises=None):
    coord = coordinator.MemoryCoordinator()
    conn = MagicMock()

    async def fetchval(sql, *args):
        if sql == coordinator.PROJECT_REGISTER_SQL:
            if raises is not None:
                raise raises
            return insert_result
        assert sql == coordinator.PROJECT_BY_KEY_SQL
        return key_result
    conn.fetchval = AsyncMock(side_effect=fetchval)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    coord._acquire = MagicMock(return_value=acq)
    return coord


@pytest.mark.asyncio
async def test_register_project_return_contract(monkeypatch):
    c = _coordinator(monkeypatch)
    assert await _registering_coord(c, None, "MyProj")._register_project(
        "myproj", "a") is False
    assert await _registering_coord(c, None, "myproj")._register_project(
        "myproj", "a") is True
    assert await _registering_coord(c, 7)._register_project("myproj", "a") is True
    assert await _registering_coord(
        c, None, raises=asyncpg.RaiseError("alias"))._register_project(
        "myproj", "a") is False


def test_project_register_sql_absorbs_every_conflict(monkeypatch):
    c = _coordinator(monkeypatch)
    assert "ON CONFLICT (" not in c.PROJECT_REGISTER_SQL
    assert "ON CONFLICT DO NOTHING" in c.PROJECT_REGISTER_SQL
    assert "RETURNING id" in c.PROJECT_REGISTER_SQL
    assert "normalized_key = axis_normalize($1)" in c.PROJECT_BY_KEY_SQL


@pytest.mark.asyncio
@pytest.mark.parametrize("body,expected", [
    ({"error": "project_spelling_variant", "message": "m"}, "refused"),
    (None, "unavailable"),
])
async def test_a_lost_project_race_is_a_400_or_a_503_never_a_500(
        monkeypatch, body, expected):
    c = _coordinator(monkeypatch)
    coord = c.MemoryCoordinator()
    coord._register_project = AsyncMock(return_value=False)
    coord._new_project_refusal = AsyncMock(return_value=body)
    report = {coord._PENDING_KEY: {"project": "myproj", "domains": []}}
    exc = (c.DomainRegistrationRefused if expected == "refused"
           else c.DomainRegistryUnavailable)
    with pytest.raises(exc) as info:
        await coord._commit_axis_registrations(report, "agent", {})
    if expected == "refused":
        assert info.value.body == body


@pytest.mark.asyncio
async def test_a_registration_returning_none_is_not_a_refusal(monkeypatch):
    c = _coordinator(monkeypatch)
    coord = c.MemoryCoordinator()
    coord._register_project = AsyncMock(return_value=None)
    coord._new_project_refusal = AsyncMock(return_value={"error": "x"})
    report = {coord._PENDING_KEY: {"project": "p", "domains": []}}
    await coord._commit_axis_registrations(report, "agent", {})
    coord._new_project_refusal.assert_not_awaited()


# -- A3 ------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["nan", "inf", "1.5", "0", "abc"])
def test_an_invalid_entity_floor_stops_startup_naming_the_variable(monkeypatch, raw):
    monkeypatch.setenv("ENTITY_CONFUSABLE_SIMILARITY", raw)
    with pytest.raises(ValueError, match="ENTITY_CONFUSABLE_SIMILARITY"):
        _load(monkeypatch, "coordinator.py", password="x")


def test_the_entity_floor_defaults_to_point_six(monkeypatch):
    monkeypatch.delenv("ENTITY_CONFUSABLE_SIMILARITY", raising=False)
    mod = _load(monkeypatch, "coordinator.py", password="x")
    assert mod.ENTITY_CONFUSABLE_SIMILARITY == 0.6
    monkeypatch.setenv("ENTITY_CONFUSABLE_SIMILARITY", "0.6")
    mod = _load(monkeypatch, "coordinator.py", password="x")
    assert mod.ENTITY_CONFUSABLE_SIMILARITY == 0.6


# -- A4 ------------------------------------------------------------------------

def _gateway(monkeypatch, tokens, roles):
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON",
                       json.dumps([{"url": "http://a:5000", "private_ok": True}]))
    monkeypatch.setenv("AGENT_TOKENS", tokens)
    if roles:
        monkeypatch.setenv("AGENT_ROLES", roles)
    else:
        monkeypatch.delenv("AGENT_ROLES", raising=False)
    secure_env._secrets.pop("AGENT_TOKENS", None)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    return g


class _Req(dict):
    headers: dict = {}


def _request(agent):
    return _Req({"authenticated_agent": agent})


def test_may_steer_uses_the_effective_role(monkeypatch):
    g = _gateway(monkeypatch, "monitor:tok_m", "monitor:admin")
    assert g._may_steer_llm(_request("monitor")) is False
    g = _gateway(monkeypatch, "maintenance-admin:tok_ma", "")
    assert g._may_steer_llm(_request("maintenance-admin")) is True
