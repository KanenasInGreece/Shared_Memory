"""Tests for DSN construction and password quote_plus encoding (A5).

A PG_PASSWORD containing characters like '@', ':', or '/' (e.g. 'a@b:c/d')
must be percent-encoded with urllib.parse.quote_plus when constructing DSNs,
so urlparse properly recovers the credentials and host/database components.
If PG_CONN is supplied whole, it is left untouched.
"""
import os
import sys
import urllib.parse
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "shared-memory" / "migrations"
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "shared-memory" / "scripts"
sys.path.insert(0, str(_MIGRATIONS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR))

import apply as apply_mod
import generate_schema_init as gen_mod
import verify_schema_init as ver_mod


def test_apply_pg_conn_encodes_password(monkeypatch):
    monkeypatch.delenv("PG_CONN", raising=False)
    monkeypatch.setenv("PG_PASSWORD", "a@b:c/d")
    dsn = apply_mod._pg_conn()
    u = urllib.parse.urlparse(dsn)
    assert u.username == "postgres"
    assert urllib.parse.unquote(u.password) == "a@b:c/d"
    assert u.hostname == "localhost"
    assert u.port == 5432
    assert u.path == "/agent_data"


def test_apply_pg_conn_leaves_explicit_pg_conn_untouched(monkeypatch):
    raw_conn = "postgresql://myuser:mypass@db.internal:5433/custom_db"
    monkeypatch.setenv("PG_CONN", raw_conn)
    monkeypatch.setenv("PG_PASSWORD", "a@b:c/d")
    assert apply_mod._pg_conn() == raw_conn


def test_generate_schema_init_pg_conn_encodes_password(monkeypatch):
    monkeypatch.delenv("PG_CONN", raising=False)
    monkeypatch.setenv("PG_PASSWORD", "a@b:c/d")
    dsn = gen_mod._pg_conn()
    u = urllib.parse.urlparse(dsn)
    assert u.username == "postgres"
    assert urllib.parse.unquote(u.password) == "a@b:c/d"
    assert u.hostname == "localhost"
    assert u.port == 5432
    assert u.path == "/agent_data"


def test_verify_schema_init_dsn_encodes_password(monkeypatch):
    monkeypatch.setenv("PG_PASSWORD", "a@b:c/d")
    monkeypatch.setenv("PG_USER", "postgres")
    monkeypatch.setenv("PG_HOST", "localhost")
    monkeypatch.setenv("PG_PORT", "5432")
    dsn = ver_mod._dsn("testdb")
    u = urllib.parse.urlparse(dsn)
    assert u.username == "postgres"
    assert urllib.parse.unquote(u.password) == "a@b:c/d"
    assert u.hostname == "localhost"
    assert u.port == 5432
    assert u.path == "/testdb"


def test_rem_loop_constructed_dsn_encodes_password(monkeypatch):
    import importlib.util
    import secure_env
    monkeypatch.setattr(secure_env, "get_secret", lambda key, default="": "a@b:c/d" if key == "PG_PASSWORD" else "")
    spec = importlib.util.spec_from_file_location("rem_loop_test", _SCRIPTS_DIR / "rem_loop.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    u = urllib.parse.urlparse(mod.PG_CONN)
    assert u.username == "postgres"
    assert urllib.parse.unquote(u.password) == "a@b:c/d"
    assert u.hostname == "localhost"
    assert u.port == 5432
    assert u.path == "/agent_data"
