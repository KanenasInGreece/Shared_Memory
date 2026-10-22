"""Fix round batch D: sync_project_registry classification and per-name apply,
diagnostic_proxy.py removed, update_framework.sh restarts a running rerank shim.

The registry tests drive main() through a fake psycopg2 connection that answers by
matching SQL text. The by-key and alias-by-key answers are given ONLY when the SQL
contains `axis_normalize(%s)`, so a Python normalisation cannot satisfy them.
"""
import re
import sys
from pathlib import Path

import psycopg2
import psycopg2.errors
import pytest

_REPO = Path(__file__).parent.parent
_SCRIPTS = _REPO / "shared-memory" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(Path(__file__).parent))

import sync_project_registry as spr  # noqa: E402
from test_update_framework_live_execution import (  # noqa: E402
    _make_exec, _make_live_sandbox, _run_live, _stub_path_env, _strip_ansi,
)


# A raised-from-Python psycopg2 error has no server diagnostics, so pgcode is None;
# these carry the SQLSTATE a server would have set.
class _Raise(psycopg2.errors.RaiseException):
    pgcode = "P0001"


class _NoTable(psycopg2.errors.UndefinedTable):
    pgcode = "42P01"


class _Denied(psycopg2.errors.InsufficientPrivilege):
    pgcode = "42501"


FOLDERS = ["orbit/relay", "Old_Name", "gamma", "foo.bar", "foobar"]


def _axis(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        c = self.conn
        c.log.append(sql)
        self._rows = []
        if c.hook:
            handled = c.hook(self, sql, params)
            if handled:
                return
        if sql == "SELECT name FROM projects":
            self._rows = [(n,) for n in sorted(c.registered)]
        elif sql.startswith("SELECT name FROM projects WHERE normalized_key") \
                and "axis_normalize(%s)" in sql:
            c.by_key_calls += 1
            key = _axis(params[0])
            hit = [n for n in c.registered if _axis(n) == key]
            self._rows = [(hit[0],)] if hit else []
        elif "project_aliases" in sql and "axis_normalize(%s)" in sql:
            c.alias_key_calls += 1
            key = _axis(params[0])
            self._rows = [(a, p) for a, p in c.aliases if _axis(a) == key][:1]
        elif sql.startswith("SELECT a.name AS alias"):
            self._rows = list(c.aliases)
        elif "unnest" in sql:
            c.unnest_calls += 1
            self._rows = [(n, _axis(n)) for n in params[0]]
        elif sql.startswith("INSERT INTO projects"):
            name = params[0]
            c.insert_attempts.append(name)
            if name in c.raise_on:
                raise c.raise_on[name]
            if name not in c.no_id:
                c.inserted.append(name)
                self._rows = [(len(c.inserted),)]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, registered, aliases, raise_on=None, no_id=(), hook=None):
        self.registered = set(registered)
        self.aliases = list(aliases)
        self.raise_on = raise_on or {}
        self.no_id = set(no_id)
        self.hook = hook
        self.log, self.inserted, self.insert_attempts, self.events = [], [], [], []
        self.by_key_calls = self.alias_key_calls = self.unnest_calls = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")

    def close(self):
        pass


def _run_main(monkeypatch, capsys, conn, *args):
    monkeypatch.setattr(spr, "project_folders", lambda roots: set(FOLDERS))
    monkeypatch.setattr(spr.psycopg2, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(sys, "argv", ["sync_project_registry.py", "--roots", "/x", *args])
    rc = spr.main()
    return rc, capsys.readouterr()


def _conn(**kw):
    return FakeConn({"orbitrelay"}, [("old-name", "alpha2")], **kw)


def test_classification_asks_the_database_for_spellings(monkeypatch, capsys):
    conn = _conn()
    rc, out = _run_main(monkeypatch, capsys, conn)
    text = out.out
    assert rc == 0
    assert conn.by_key_calls and conn.alias_key_calls and conn.unnest_calls
    new_block = text.split("NEW — would be registered")[1].split("──")[0]
    assert "+ gamma" in new_block and "+ foo.bar" in new_block
    assert "+ foobar" not in new_block and "orbit/relay" not in new_block
    assert "Old_Name" not in new_block
    assert "'orbit/relay': spelling of registered project 'orbitrelay'" in text
    assert "'Old_Name': retired spelling of project 'alpha2'" in text
    assert "'foobar': spelling of folder 'foo.bar' — the registry keeps one" in text
    assert "rename the folder to 'alpha2'" in text


def test_apply_skips_a_refused_name_and_keeps_the_rest(monkeypatch, capsys):
    secret = "DISTINCTIVE-RAW-TRIGGER-MESSAGE"
    inserted_seen = []

    def hook(cur, sql, params):
        if sql.startswith("INSERT INTO projects"):
            inserted_seen.append(params[0])
        # after the no-id insert of gamma, the re-read by key finds the winner
        if sql.startswith("SELECT name FROM projects WHERE normalized_key") \
                and params[0] == "gamma" and "gamma" in inserted_seen:
            cur._rows = [("Gamma",)]
            return True
        return False

    conn = _conn(raise_on={"foo.bar": _Raise(secret)},
                 no_id={"gamma"}, hook=hook)
    rc, out = _run_main(monkeypatch, capsys, conn, "--apply")
    text = out.out + out.err
    assert rc == 0
    assert sorted(conn.insert_attempts) == ["foo.bar", "gamma"]
    assert "skipped: foo.bar — refused by the registry (P0001)" in text
    assert secret not in text
    assert "skipped: gamma — registered meanwhile as Gamma" in text
    assert conn.events == ["commit"], conn.events
    assert any(s.startswith("ROLLBACK TO SAVEPOINT") for s in conn.log)
    assert any("ON CONFLICT DO NOTHING RETURNING id" in s for s in conn.log)


def test_apply_registers_every_name_when_nothing_is_refused(monkeypatch, capsys):
    conn = _conn()
    rc, out = _run_main(monkeypatch, capsys, conn, "--apply")
    assert rc == 0
    assert sorted(conn.inserted) == ["foo.bar", "gamma"]
    assert conn.events == ["commit"]
    assert "Registered 2 project(s)" in out.out


def test_a_missing_alias_schema_is_skipped_and_any_other_error_surfaces():
    def missing(cur, sql, params):
        if "project_aliases" in sql:
            raise _NoTable("no table")
        return False

    conn = _conn(hook=missing)
    new, spellings, skipped = spr.classify_new(conn.cursor(), ["gamma"])
    assert skipped and new == ["gamma"]

    def denied(cur, sql, params):
        if "project_aliases" in sql:
            raise _Denied("denied")
        return False

    conn = _conn(hook=denied)
    with pytest.raises(_Denied):
        spr.classify_new(conn.cursor(), ["gamma"])


def test_diagnostic_proxy_is_deleted():
    assert not (_SCRIPTS / "diagnostic_proxy.py").exists()


# ── D3: update_framework.sh and the rerank shim ──

def _shim_env(tmp_path, *, is_active, docker_names=None, restart_rc=0):
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)
    stub = tmp_path / "stubbin"
    _make_exec(
        stub / "systemctl",
        '#!/usr/bin/env bash\n'
        f'echo "systemctl $*" >> "{log_path}"\n'
        'case "$*" in\n'
        f'  *is-active*) exit {0 if is_active else 3} ;;\n'
        f'  *restart*rerank-shim*) exit {restart_rc} ;;\n'
        'esac\nexit 0\n',
    )
    if docker_names is not None:
        _make_exec(
            stub / "docker",
            '#!/usr/bin/env bash\n'
            f'echo "docker $*" >> "{log_path}"\n'
            f'case "$1" in ps) echo "{docker_names}" ;; esac\nexit 0\n',
        )
    else:
        # a docker that names no container, so the "neither" case is deterministic
        _make_exec(stub / "docker", '#!/usr/bin/env bash\nexit 0\n')
    return repo, log_path, env


def _lines(log, prefix):
    return [ln for ln in log.read_text().splitlines() if ln.startswith(prefix)]


def test_active_shim_unit_is_restarted(tmp_path):
    repo, log, env = _shim_env(tmp_path, is_active=True)
    proc = _run_live(repo, env, "--skip-backup")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "systemctl --user restart rerank-shim.service" in log.read_text()


def test_no_shim_prints_nothing_to_restart(tmp_path):
    repo, log, env = _shim_env(tmp_path, is_active=False)
    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)
    assert proc.returncode == 0, out
    assert "restart rerank-shim.service" not in log.read_text()
    assert "no rerank shim is running" in out


def test_shim_container_gets_a_warning_and_is_never_restarted(tmp_path):
    repo, log, env = _shim_env(tmp_path, is_active=False, docker_names="b580-rerank-shim")
    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)
    assert proc.returncode == 0, out
    assert "docker restart b580-rerank-shim" in out
    assert not [ln for ln in _lines(log, "docker") if "restart" in ln]
    assert _lines(log, "docker ps")


def test_dry_run_calls_neither_systemctl_nor_docker_for_the_shim(tmp_path):
    repo, log, env = _shim_env(tmp_path, is_active=True, docker_names="b580-rerank-shim")
    proc = _run_live(repo, env, "--skip-backup", "--dry-run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "rerank-shim" not in log.read_text()
    assert not _lines(log, "docker")


def test_a_failing_shim_restart_does_not_fail_the_update(tmp_path):
    repo, log, env = _shim_env(tmp_path, is_active=True, restart_rc=1)
    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)
    assert proc.returncode == 0, out
    assert "restarting rerank-shim.service failed" in out
    assert "Update complete and VERIFIED" in out


def test_every_insert_runs_inside_its_own_savepoint(monkeypatch, capsys):
    """A real Postgres aborts the whole transaction on an error unless the row has a
    SAVEPOINT to roll back to; the fake cannot model that, so pin the statement order."""
    conn = _conn(raise_on={"foo.bar": _Raise("x")})
    _run_main(monkeypatch, capsys, conn, "--apply")
    inserts = [i for i, s in enumerate(conn.log) if s.startswith("INSERT INTO projects")]
    assert inserts, conn.log
    for i in inserts:
        assert conn.log[i - 1].startswith("SAVEPOINT "), conn.log[i - 1]
