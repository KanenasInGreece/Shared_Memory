"""Refused ports fail at once. A silent Postgres accept lasts only as long as
PGCONNECT_TIMEOUT, which the caller chooses. The domain backfill keeps the
bounds it already had.
"""
import ast
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "shared-memory" / "scripts"

CONNECT_SCRIPTS = (
    "backfill_project_of.py",
    "normalize_projects.py",
    "reconcile_project_edges.py",
    "sync_project_registry.py",
)
HEALTH_SCRIPTS = (
    "backfill_project_of.py",
    "backfill_promote_grounded.py",
    "reconcile_project_edges.py",
)
NO_PARAGRAPH = (
    "backfill_domain_of.py",
    "backfill_promote_grounded.py",
)

PARAGRAPH = (
    "A Postgres that accepts the connection and never answers is waited for\n"
    "without a limit, and Ctrl-C does not stop that wait (kill the process\n"
    "instead; over ssh, on the remote host). To bound it, put\n"
    "PGCONNECT_TIMEOUT=<seconds> in front of the command, inside the remote\n"
    "command over ssh; the number is yours to choose (libpq treats 0 as no\n"
    "limit and raises 1 to 2, and applies the limit to each address a host\n"
    "name resolves to)."
)

_GATEWAY_PROBE = (
    "import importlib, json, sys\n"
    "mod = importlib.import_module(sys.argv[1])\n"
    "raw, parsed = mod.gateway_version()\n"
    "json.dump({'raw': raw, 'parsed': parsed}, sys.stdout)\n"
)


def _env(tmp_path, **extra):
    empty = tmp_path / "secure.env"
    empty.write_text("")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "SECURE_ENV_FILE": str(empty),
        "COORDINATOR_UDS": "",
    }
    env.update(extra)
    return env


def _refused_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _Silent:
    """Accepts TCP connections and never reads or writes."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.port = self.sock.getsockname()[1]
        self.accepted = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.accepted.append(conn)

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        for conn in self.accepted:
            try:
                conn.close()
            except OSError:
                pass
        self._thread.join(timeout=1)


def _connect_env(tmp_path, script, port):
    neo_port = _refused_port()
    env = _env(
        tmp_path,
        PG_HOST="127.0.0.1",
        PG_PORT=str(port),
        PG_PASSWORD="x",
        PG_USER="postgres",
        PG_DATABASE="agent_data",
        NEO4J_PASSWORD="x",
        NEO4J_USER="neo4j",
        NEO4J_URI=f"bolt://127.0.0.1:{neo_port}",
    )
    args = []
    if script == "normalize_projects.py":
        env["PG_CONN"] = f"postgresql://postgres:x@127.0.0.1:{port}/agent_data"
        args = ["--map", "a=b"]
    if script == "sync_project_registry.py":
        root = tmp_path / "roots"
        (root / "one").mkdir(parents=True)
        env["PROJECT_ROOTS"] = str(root)
    return env, args


def _run(script, args, env, timeout):
    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        env=env,
        cwd=str(SCRIPTS),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc, time.perf_counter() - started


def _probe(module, env, timeout):
    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-c", _GATEWAY_PROBE, module],
        env=env,
        cwd=str(SCRIPTS),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc, time.perf_counter() - started


# Refused fails at once, it does not wait. A stall without PGCONNECT_TIMEOUT
# never ends. 5 s is the test's own unmeasured tolerance. The kill bound only
# stops a subprocess that did not return.
_REFUSAL_KILL_S = 15
_REFUSAL_BOUND_S = 5


@pytest.mark.parametrize("script", CONNECT_SCRIPTS)
def test_refused_postgres_errors_at_once(script, tmp_path):
    port = _refused_port()
    env, args = _connect_env(tmp_path, script, port)
    proc, elapsed = _run(script, args, env, timeout=_REFUSAL_KILL_S)
    assert proc.returncode != 0
    assert elapsed < _REFUSAL_BOUND_S
    assert "Connection refused" in proc.stderr


@pytest.mark.parametrize("script", HEALTH_SCRIPTS)
def test_refused_gateway_errors_at_once(script, tmp_path):
    port = _refused_port()
    env = _env(tmp_path, GATEWAY_URL=f"http://127.0.0.1:{port}")
    proc, elapsed = _probe(Path(script).stem, env, timeout=_REFUSAL_KILL_S)
    assert proc.returncode == 0, proc.stderr
    assert elapsed < _REFUSAL_BOUND_S
    pair = json.loads(proc.stdout)
    assert pair["parsed"] is None
    assert pair["raw"].startswith("unreachable (")
    assert "Connection refused" in pair["raw"]


@pytest.mark.parametrize("script", HEALTH_SCRIPTS)
def test_gateway_version_refused_returns_unparsed_pair(script, tmp_path):
    port = _refused_port()
    env = _env(tmp_path, GATEWAY_URL=f"http://127.0.0.1:{port}")
    proc, elapsed = _probe(Path(script).stem, env, timeout=_REFUSAL_KILL_S)
    assert proc.returncode == 0, proc.stderr
    assert elapsed < _REFUSAL_BOUND_S
    pair = json.loads(proc.stdout)
    assert pair["parsed"] is None
    assert isinstance(pair["raw"], str)
    assert pair["raw"].startswith("unreachable (")


class _VersionHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.hits += 1
        body = b'{"version": "not-a-version"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


@pytest.mark.parametrize("script", HEALTH_SCRIPTS)
def test_gateway_version_unparseable_returns_unparsed_pair(script, tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _VersionHandler)
    server.hits = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = _env(tmp_path, GATEWAY_URL=f"http://127.0.0.1:{server.server_port}")
        proc, elapsed = _probe(Path(script).stem, env, timeout=_REFUSAL_KILL_S)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
    assert proc.returncode == 0, proc.stderr
    assert elapsed < _REFUSAL_BOUND_S
    assert server.hits >= 1
    assert json.loads(proc.stdout) == {"raw": "not-a-version", "parsed": None}


# 2 and 4. The windows are 2..3.5 and 4..5.5, and they are disjoint, so a
# fixed 2 dies when the caller asked for 4 and a fixed 4 dies when the
# caller asked for 2. The subprocess timeout is the end of that window:
# the total wait allowed for the run.
@pytest.mark.parametrize("seconds", (2, 4))
@pytest.mark.parametrize("script", CONNECT_SCRIPTS)
def test_pgconnect_timeout_bounds_silent_postgres(script, seconds, tmp_path):
    listener = _Silent()
    try:
        env, args = _connect_env(tmp_path, script, listener.port)
        env["PGCONNECT_TIMEOUT"] = str(seconds)
        limit = seconds + 1.5
        try:
            proc, elapsed = _run(script, args, env, timeout=limit)
        except subprocess.TimeoutExpired:
            pytest.fail(
                f"{script} was still running after {limit}s "
                f"with PGCONNECT_TIMEOUT={seconds}"
            )
        assert proc.returncode != 0
        assert seconds <= elapsed <= limit, (
            f"{script} elapsed {elapsed:.3f}s, expected {seconds}..{limit} "
            f"(stderr tail: {proc.stderr[-400:]})"
        )
        assert len(listener.accepted) >= 1
        assert "timeout expired" in proc.stderr
    finally:
        listener.close()


def _calls(src, attr):
    tree = ast.parse(src)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == attr:
                found.append(node)
    return found


def test_domain_backfill_keeps_connect_and_urlopen_bounds():
    src = (SCRIPTS / "backfill_domain_of.py").read_text(encoding="utf-8")
    assert "connect_timeout=5" in src
    connects = _calls(src, "connect")
    assert any(
        kw.arg == "connect_timeout"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value == 5
        for node in connects
        for kw in node.keywords
    )
    opens = _calls(src, "urlopen")
    assert opens, "urlopen call missing"
    assert any(
        kw.arg == "timeout" and isinstance(kw.value, ast.Constant) and kw.value.value == 10
        for node in opens
        for kw in node.keywords
    )


def test_connect_scripts_drop_connect_timeout():
    for name in CONNECT_SCRIPTS:
        src = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "connect_timeout" not in src, name


def test_health_urlopen_has_no_timeout():
    for name in HEALTH_SCRIPTS:
        src = (SCRIPTS / name).read_text(encoding="utf-8")
        opens = _calls(src, "urlopen")
        assert opens, name
        for node in opens:
            segment = ast.get_source_segment(src, node)
            assert "timeout" not in segment, name
            assert all(kw.arg != "timeout" for kw in node.keywords)


def test_postgres_wait_paragraph_is_only_in_the_connect_scripts():
    for name in CONNECT_SCRIPTS:
        src = (SCRIPTS / name).read_text(encoding="utf-8")
        tree = ast.parse(src)
        assert f"\n\n{PARAGRAPH}\n\n" in src, name
        doc = ast.get_docstring(tree, clean=False)
        assert doc is not None and PARAGRAPH in doc, name
        after = src.split(PARAGRAPH, 1)[1]
        assert after.startswith("\n\n")
        next_line = after[2:].splitlines()[0]
        if name == "normalize_projects.py":
            assert next_line == "Usage:"
        else:
            assert next_line.startswith("    "), next_line
    for name in NO_PARAGRAPH:
        src = (SCRIPTS / name).read_text(encoding="utf-8")
        assert PARAGRAPH not in src, name


_GATEWAY_DOC = (
    "(raw, parsed) version of the RUNNING gateway. parsed is None when the\n"
    "gateway refuses the connection, cannot be reached, or reports a version\n"
    "whose first three dot-separated parts are not whole numbers. A host that\n"
    "never answers the connection attempt fails when the operating system gives\n"
    "up. A gateway that accepts the connection and never answers is waited for\n"
    "without a limit; in a terminal, Ctrl-C stops that wait."
)
_PROMOTE_DOC = (
    "(raw, parsed) version of the RUNNING gateway. parsed is None when the\n"
    "gateway refuses the connection, cannot be reached, or reports a version\n"
    "whose first three dot-separated parts are not whole numbers. A host that\n"
    "never answers the connection attempt fails when the operating system gives\n"
    "up. A gateway that accepts the connection and never answers is waited for\n"
    "without a limit; this script runs inside an event loop, so in a terminal\n"
    "press Ctrl-C twice to stop that wait."
)
_TWICE_SENTENCE = (
    "this script runs inside an event loop, so in a terminal press Ctrl-C "
    "twice to stop that wait."
)


def _function_doc(path, name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_docstring(node, clean=True)
    raise AssertionError(f"{name} is missing from {path.name}")


def _flat(text):
    return " ".join(text.split())


def test_gateway_version_docstring_names_an_unparsed_version():
    """The probe reports an unparsed version when the text is not made of
    numbers. The older wording claimed any unreadable answer, which is wider
    than what the function does."""
    for name in ("backfill_project_of.py", "reconcile_project_edges.py"):
        assert _function_doc(SCRIPTS / name, "gateway_version") == _GATEWAY_DOC
    promote = SCRIPTS / "backfill_promote_grounded.py"
    assert _function_doc(promote, "gateway_version") == _PROMOTE_DOC
    for name in (
        "backfill_project_of.py",
        "reconcile_project_edges.py",
        "backfill_promote_grounded.py",
    ):
        src = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "or its answer cannot be parsed" not in src


def test_ctrl_c_twice_is_only_in_the_event_loop_script():
    """The promotion backfill calls the probe from inside an event loop, where
    the first interrupt cancels the task and the next one stops the wait. The
    other two probes are ordinary calls, and one interrupt stops those."""
    promote = (SCRIPTS / "backfill_promote_grounded.py").read_text(encoding="utf-8")
    assert _TWICE_SENTENCE in _flat(promote)
    for name in ("backfill_project_of.py", "reconcile_project_edges.py"):
        src = (SCRIPTS / name).read_text(encoding="utf-8")
        assert _TWICE_SENTENCE not in _flat(src)
        assert "twice" not in _flat(_function_doc(SCRIPTS / name, "gateway_version"))


class _RecordingConn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.log.append("insert")

    def rollback(self):
        self.log.append("rollback")

    def commit(self):
        self.log.append("commit")

    def close(self):
        return None


class _RecordingDriver:
    def close(self):
        return None


def _load_script(name, monkeypatch, tmp_path):
    import importlib

    # Import runs _load_env() in this process, before the test can stub
    # anything. An empty file and a cleared host environment keep that read
    # off the operator's files. Dropping the cached module first means the
    # import is the shipped script; monkeypatch puts the old entry back.
    empty = tmp_path / "secure.env"
    empty.write_text("")
    monkeypatch.setenv("SECURE_ENV_FILE", str(empty))
    monkeypatch.setenv("COORDINATOR_UDS", "")
    for key in list(os.environ):
        if (
            key.startswith("PG_")
            or key.startswith("NEO4J_")
            or key in ("PGCONNECT_TIMEOUT", "GATEWAY_URL")
        ):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delitem(sys.modules, name, raising=False)
    scripts = str(SCRIPTS)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module(name)


@pytest.mark.parametrize(
    "module_name",
    ("backfill_project_of", "reconcile_project_edges"),
)
def test_apply_rolls_back_before_the_gateway_wait(module_name, monkeypatch, tmp_path):
    """Under --apply the reads have already started a transaction when the
    script asks the gateway for its version. That wait has no limit. The
    rollback belongs after the reads and before the wait, and the inserts
    below commit a new transaction.

    The connect, the reads, and the version probe are stubs that only record
    the order they ran. A gateway that never answers would leave this test
    waiting, so the check is that order and not a clock. The expected list is
    the whole path: it dies if the rollback is removed, if it moves to after
    the probe, or if a rollback is recorded once the inserts have begun and
    before commit closes them.
    """
    mod = _load_script(module_name, monkeypatch, tmp_path)
    log = []

    def connect(_dsn):
        log.append("connect")
        return _RecordingConn(log)

    def backfill_ids(_driver):
        log.append("read")
        return [1], 0

    def backfill_resolve(_conn, _ids):
        log.append("read")
        return {1: "kept"}

    def backfill_queued(_conn, _ids):
        log.append("read")
        return set()

    def reconcile_graph(_driver):
        log.append("read")
        return {1: ["other"]}

    def reconcile_pg(_conn):
        log.append("read")
        return {1: "kept"}

    def reconcile_queued(_conn, _ids):
        log.append("read")
        return set()

    def version():
        log.append("gateway_version")
        return "running", mod.MIN_GATEWAY_VERSION

    monkeypatch.setattr(mod.psycopg2, "connect", connect)
    monkeypatch.setattr(mod.GraphDatabase, "driver", lambda *_a, **_k: _RecordingDriver())
    monkeypatch.setattr(mod, "gateway_version", version)
    monkeypatch.setattr(sys, "argv", [f"{module_name}.py", "--apply"])
    if module_name == "backfill_project_of":
        monkeypatch.setattr(mod, "NEO4J_PASS", "x")
        monkeypatch.setattr(mod, "edgeless_fact_pg_ids", backfill_ids)
        monkeypatch.setattr(mod, "resolve", backfill_resolve)
        monkeypatch.setattr(mod, "already_queued", backfill_queued)
    else:
        monkeypatch.setattr(mod, "NEO4J_AUTH", ("neo4j", "x"))
        monkeypatch.setattr(mod, "graph_project_edges", reconcile_graph)
        monkeypatch.setattr(mod, "postgres_projects", reconcile_pg)
        monkeypatch.setattr(mod, "already_queued", reconcile_queued)

    assert mod.main() == 0
    assert log == [
        "connect",
        "read",
        "read",
        "read",
        "rollback",
        "gateway_version",
        "insert",
        "commit",
    ]
    # No rollback is recorded on the insert transaction. Commit is what
    # changes it.
    assert "rollback" not in log[log.index("insert"):log.index("commit")]
