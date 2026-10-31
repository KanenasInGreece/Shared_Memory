"""One declaration. Each test dies on the failure it names."""
import asyncio
import hashlib
import importlib
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "shared-memory" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

GOOD = {"url": "http://a:5000", "n_ctx": 8192, "max_output_tokens": 2048}


def _reload(monkeypatch, raw):
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.delenv("LLM_DEFAULT_TARGET", raising=False)
    if raw is None:
        monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    else:
        monkeypatch.setenv("LLM_BACKENDS_JSON", raw)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    return importlib.reload(g)


def _apply(monkeypatch, raw, extra=None, tmp_path=None):
    """--apply reads the file the gateway will read, not the operator shell."""
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.delenv("LLM_DEFAULT_TARGET", raising=False)
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.delenv("AGENT_TOKENS_FILE", raising=False)
    directory = tmp_path if tmp_path is not None else Path(os.environ.get("TMPDIR", "/tmp"))
    env_file = directory / "apply-picture.env"
    lines = []
    if raw is not None:
        lines.append("LLM_BACKENDS_JSON=" + raw)
    for key, value in (extra or {}).items():
        lines.append(f"{key}={value}")
    env_file.write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_file))
    import migrate_env
    monkeypatch.setattr(migrate_env.shutil, "which", lambda name: None)
    return migrate_env.main(["--apply"])


def test_f1_missing_context_is_fatal_with_a_distinct_exit(monkeypatch):
    g = _reload(monkeypatch, json.dumps([{"url": "http://a:5000", "max_output_tokens": 10}]))
    assert g.LLM_BACKENDS == []
    assert ("http://a:5000", "n_ctx") in g.LLM_DECLARATION_ERRORS
    with pytest.raises(SystemExit) as exc:
        g.require_valid_llm_routing_config()
    assert exc.value.code == 78
    assert "n_ctx" in exc.value.message

    g = _reload(monkeypatch, json.dumps([{"url": "http://a:5000", "n_ctx": True, "max_output_tokens": 10}]))
    assert ("http://a:5000", "n_ctx") in g.LLM_DECLARATION_ERRORS


def test_f1_absent_or_empty_declaration_boots_degraded(monkeypatch):
    for raw in (None, "", "[]"):
        g = _reload(monkeypatch, raw)
        assert g.LLM_BACKENDS == []
        assert g.LLM_DECLARATION_ERRORS == []
        g.require_valid_llm_routing_config()
        dep = g._llm_pool_dependency({})
        assert dep["state"] == "degraded"
        assert dep["reason"] == "no_backend_declared"


@pytest.mark.parametrize("raw,entry,field", [
    ("{", "LLM_BACKENDS_JSON", "json"),
    ("{}", "LLM_BACKENDS_JSON", "type"),
    (json.dumps(["nope"]), "[0]", "entry"),
    (json.dumps([{"n_ctx": 1, "max_output_tokens": 1}]), "[0]", "url"),
    (json.dumps([{"url": "http://a:5000", "max_output_tokens": 1}]), "http://a:5000", "n_ctx"),
    (json.dumps([{"url": "http://a:5000", "n_ctx": 1}]), "http://a:5000", "max_output_tokens"),
    (json.dumps([{**GOOD, "priority": 0}]), "http://a:5000", "priority"),
    (json.dumps([{**GOOD, "token": "secret"}]), "http://a:5000", "token"),
    (json.dumps([{**GOOD, "extra_body": "nope"}]), "http://a:5000", "extra_body"),
    (json.dumps([{**GOOD, "max_inflight": False}]), "http://a:5000", "max_inflight"),
    (json.dumps([{"url": "http://user:pw@a:5000", "n_ctx": 1, "max_output_tokens": 1}]), "http://a:5000", "url"),
])
def test_f1b_validator_and_apply_name_every_fatal_class(monkeypatch, tmp_path, raw, entry, field):
    import secure_env
    monkeypatch.setenv("LLM_BACKENDS_JSON", raw)
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    assert (entry, field) in secure_env.llm_declaration_errors()
    env_file = tmp_path / "env"
    env_file.write_text("LLM_BACKENDS_JSON=" + raw + "\n")
    before = env_file.read_bytes()
    assert _apply(monkeypatch, raw, tmp_path=tmp_path) == 1
    assert env_file.read_bytes() == before


def test_f1b_reserved_name_is_a_validator_error(monkeypatch, tmp_path):
    import secure_env
    monkeypatch.setenv("AGENT_TOKENS", "consolidation:tok")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([GOOD]))
    assert ("consolidation", "agent") in secure_env.llm_declaration_errors()
    env_file = tmp_path / "env"
    env_file.write_text("keep\n")
    before = env_file.read_bytes()
    assert _apply(monkeypatch, json.dumps([GOOD]), {"AGENT_TOKENS": "consolidation:tok"}, tmp_path=tmp_path) == 1
    assert env_file.read_bytes() == before


def test_f2_empty_token_file_excludes_and_stays_up(monkeypatch, tmp_path, caplog):
    token_file = tmp_path / "empty.key"
    token_file.write_text("")
    monkeypatch.setenv("DEEPSEEK_API_KEY_FILE", str(token_file))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    raw = json.dumps([{**GOOD, "token_env": "DEEPSEEK_API_KEY"}])
    g = _reload(monkeypatch, raw)
    assert g.LLM_BACKENDS == []
    assert g.LLM_BACKENDS_EXCLUDED == [{"url": "http://a:5000", "reason": "token_file_empty"}]
    g.require_valid_llm_routing_config()
    import check_config
    lines, _code = check_config.phase_b_render()
    assert any("reason=token_file_empty" in line for line in lines)


class _HealthOk:
    status = 200


class _HealthCm:
    async def __aenter__(self):
        return _HealthOk()

    async def __aexit__(self, *a):
        return False


class _HealthSession:
    def get(self, *args, **kwargs):
        return _HealthCm()


def _health_body(g):
    """What GET /health returns. The llm enum is on this body, not on a helper the handler can ignore."""
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _HealthSession()
    request = type("Req", (), {})()
    request.headers = {}
    request.app = {"proxy": proxy, "coordinator": None}

    async def _run():
        resp = await g.handle_health(request)
        return json.loads(resp.body.decode())

    return asyncio.run(_run())


def test_f2b_whole_fleet_excluded_is_no_backend_and_llm_agrees(monkeypatch, tmp_path):
    token_file = tmp_path / "empty.key"
    token_file.write_text("")
    monkeypatch.setenv("DEEPSEEK_API_KEY_FILE", str(token_file))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    g = _reload(monkeypatch, json.dumps([{**GOOD, "token_env": "DEEPSEEK_API_KEY"}]))
    assert g._classify_no_eligible_constraint(0, 0) == "no_backend"
    dep = g._llm_pool_dependency({})
    assert dep["state"] == "degraded"
    assert "fit" not in dep["reason"]
    assert not g.LLM_BACKENDS
    body = _health_body(g)
    assert body["llm"] == "degraded"
    assert body["dependencies"]["llm_pool"]["state"] == "degraded"
    assert body["llm"] != "ok"


def test_f5_leftover_keys_still_serve_and_warn_once(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.WARNING)
    warns = [
        {**GOOD, "private_ok": False},
        {**GOOD, "roles": ["extract"]},
    ]
    silent = [
        dict(GOOD),
        {**GOOD, "private_ok": True},
        {**GOOD, "roles": ["extract", "judge"]},
    ]
    for entry in warns:
        caplog.clear()
        g = _reload(monkeypatch, json.dumps([entry]))
        assert g.LLM_BACKENDS == ["http://a:5000"]
        assert g._select_llm_backend(None) == "http://a:5000"
        serves = [r.message for r in caplog.records if "now serves both daemons" in r.message]
        assert len(serves) == 1
        assert "not trusted is not declared" in serves[0]
    for entry in silent:
        caplog.clear()
        g = _reload(monkeypatch, json.dumps([entry]))
        assert g.LLM_BACKENDS == ["http://a:5000"]
        assert not any("now serves both daemons" in r.message for r in caplog.records)


def _repo_env_fingerprint():
    """Identity of the checkout env file. Stat only: a write changes it, and the bytes are not this test's to print."""
    path = SCRIPTS.parent / ".env"
    if not path.exists():
        return "absent"
    st = path.stat()
    return (st.st_size, st.st_mtime_ns, st.st_ino)


def _point_at_file(monkeypatch, env_file):
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_file))
    for key in ("LLM_BACKENDS_JSON", "LLM_BACKENDS", "LLM_DEFAULT_TARGET", "AGENT_TOKENS", "AGENT_TOKENS_FILE"):
        monkeypatch.delenv(key, raising=False)
    import migrate_env
    monkeypatch.setattr(migrate_env.shutil, "which", lambda name: None)


def test_f6_apply_leaves_a_valid_file_unchanged(monkeypatch, tmp_path, capsys):
    env_file = tmp_path / "gateway.env"
    env_file.write_text("LLM_BACKENDS_JSON=" + json.dumps([GOOD]) + "\n")
    before = env_file.read_bytes()
    finger = _repo_env_fingerprint()
    _point_at_file(monkeypatch, env_file)
    import migrate_env
    assert migrate_env.main(["--apply"]) == 0
    assert "Nothing was written" in capsys.readouterr().out
    assert env_file.read_bytes() == before
    assert _repo_env_fingerprint() == finger


def test_f6_legacy_only_stops_before_any_write(monkeypatch, tmp_path, capsys):
    env_file = tmp_path / "gateway.env"
    env_file.write_text("LLM_BACKENDS=http://a:5000\n")
    before = env_file.read_bytes()
    finger = _repo_env_fingerprint()
    _point_at_file(monkeypatch, env_file)
    import migrate_env
    assert migrate_env.main(["--apply"]) == 1
    out = capsys.readouterr().out
    assert "legacy field LLM_BACKENDS" in out
    assert "lose reasoning" in out
    assert "would refuse" not in out
    assert env_file.read_bytes() == before
    assert _repo_env_fingerprint() == finger


def test_f6_invalid_file_names_the_field_and_writes_nothing(monkeypatch, tmp_path, capsys):
    env_file = tmp_path / "gateway.env"
    env_file.write_text(
        'LLM_BACKENDS_JSON=[{"url":"http://a:5000","max_output_tokens":1}]\n'
    )
    before = env_file.read_bytes()
    finger = _repo_env_fingerprint()
    _point_at_file(monkeypatch, env_file)
    import migrate_env
    assert migrate_env.main(["--apply"]) == 1
    out = capsys.readouterr().out
    assert "field n_ctx" in out
    assert env_file.read_bytes() == before
    assert _repo_env_fingerprint() == finger


def test_empty_default_target_does_not_stop_and_a_set_one_is_named(monkeypatch, tmp_path, capsys):
    import migrate_env
    monkeypatch.setattr(migrate_env.shutil, "which", lambda name: None)
    empty = tmp_path / "empty.env"
    empty.write_text("LLM_DEFAULT_TARGET=\n")
    _point_at_file(monkeypatch, empty)
    assert migrate_env.main(["--apply"]) == 0
    set_one = tmp_path / "legacy.env"
    set_one.write_text("LLM_DEFAULT_TARGET=http://a:5000\n")
    _point_at_file(monkeypatch, set_one)
    assert migrate_env.main(["--apply"]) == 1
    out = capsys.readouterr().out
    assert "legacy field LLM_DEFAULT_TARGET" in out
    assert "lose reasoning" in out
    assert "legacy field LLM_BACKENDS" not in out
    assert "would refuse" not in out


def test_json_null_is_a_type_fatal(monkeypatch):
    import secure_env
    monkeypatch.setenv("LLM_BACKENDS_JSON", "null")
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    assert ("LLM_BACKENDS_JSON", "type") in secure_env.llm_declaration_errors()


def test_schemeless_url_does_not_print_a_query_secret():
    import secure_env
    secret = "SEKRET"
    shown = secure_env._scrub_declared_url(f"api.example.test/v1?key={secret}&r=https://x")
    assert shown == "<unparseable>"
    assert secret not in shown


def test_query_credential_stays_out_of_the_refusal(monkeypatch, tmp_path, capsys):
    secret = "qsecret-not-for-logs"
    raw = json.dumps([{
        "url": f"http://backend.example.test/v1?api_key={secret}",
        "max_output_tokens": 8,
    }])
    g = _reload(monkeypatch, raw)
    journal = g._declaration_fatal_message()
    assert secret not in journal
    assert "backend.example.test" in journal
    import check_config
    report = "\n".join(check_config._phase_a_declaration_lines())
    assert secret not in report
    assert "backend.example.test" in report
    env_file = tmp_path / "gateway.env"
    env_file.write_text("LLM_BACKENDS_JSON=" + raw + "\n")
    _point_at_file(monkeypatch, env_file)
    import migrate_env
    assert migrate_env.main(["--apply"]) == 1
    out = capsys.readouterr().out
    assert secret not in out
    assert "backend.example.test" in out


def test_bad_port_userinfo_does_not_raise_at_import(monkeypatch):
    raw = json.dumps([{
        "url": "http://user:s3cr3t-port@host:abc/v1",
        "n_ctx": 1,
        "max_output_tokens": 1,
    }])
    g = _reload(monkeypatch, raw)
    rendered = str(g.LLM_DECLARATION_ERRORS)
    assert "s3cr3t-port" not in rendered
    assert g.LLM_DECLARATION_ERRORS


def test_reserved_name_in_the_secret_store_is_refused(monkeypatch):
    import secure_env
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([GOOD]))
    secure_env._secrets["AGENT_TOKENS"] = "consolidation:fixture-token-value-99"
    try:
        errors = secure_env.llm_declaration_errors()
    finally:
        secure_env._secrets.pop("AGENT_TOKENS", None)
    assert ("consolidation", "agent") in errors
    assert "fixture-token-value-99" not in str(errors)


def test_f7_reserved_names_do_not_raise_at_import_and_refuse_at_boot(monkeypatch, tmp_path):
    digest = "ab" * 32
    monkeypatch.setenv("AGENT_TOKENS", f"rem_daemon:sha256:{digest}")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([GOOD]))
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    assert "rem_daemon" in coordinator._RESERVED_DAEMON_NAMES_SEEN

    import generate_tokens
    monkeypatch.setattr(generate_tokens, "LOCAL_SKILL_ENV_PATHS", {})

    def _stub_mint(env_path=None, roster=None, revealing=None):
        generate_tokens._refuse_reserved_daemon_names(roster)
        return {}, {}, []

    monkeypatch.setattr(generate_tokens, "mint", _stub_mint)
    with pytest.raises(ValueError, match="reserved daemon name"):
        generate_tokens.mint(env_path=str(tmp_path / "absent.env"), roster=["consolidation"])

    async def _no_start(self):
        raise AssertionError("main passed the reserved-name refusal")

    monkeypatch.setattr(g.AsyncHiveMindProxy, "start_session", _no_start)
    monkeypatch.setattr(sys, "argv", ["hive_mind_proxy"])
    with pytest.raises(SystemExit) as exc:
        asyncio.run(g.main())
    assert exc.value.code == 78
    assert "rem_daemon" in exc.value.message


def test_f8_free_slots_counts_every_available_backend(monkeypatch):
    # Auth-off, so an anonymous /pool/status returns the roster. A prior test
    # may have left AUTH_CONFIGURED_AT_STARTUP true until coordinator reloads.
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    g = _reload(monkeypatch, json.dumps([
        GOOD,
        {"url": "http://b:5000", "n_ctx": 8192, "max_output_tokens": 2048},
    ]))
    g._llm_inflight["http://a:5000"] = 1

    class _Req(dict):
        headers = {}
        rel_url = None
        def get(self, key, default=None):
            return dict.get(self, key, default)

    import asyncio
    from aiohttp import web
    resp = asyncio.run(g.handle_pool_status(_Req()))
    body = json.loads(resp.body)
    assert body["free_slots"] == 1
    assert body["backends"]["http://b:5000"]["available"] is True
    assert body["backends"]["http://a:5000"]["available"] is False


def test_f9_empty_roster_is_not_pool_busy_and_a_refusal_is_uncharged(monkeypatch):
    """The main-loop gates at the consolidation and sweep branches. An empty roster is not busy, so the fold and the lineage pass run and nothing is deferred as pool_busy."""
    import asyncio
    import http.server
    import threading
    from datetime import datetime, timedelta
    import consolidation_loop as cl
    import pool_status

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"free_slots":0,"backends":{}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(pool_status, "POOL_STATUS_URL", f"http://127.0.0.1:{server.server_port}/pool/status")
    ran = []
    deferred = []
    ticks = {"n": 0}
    due = iter([(True, False), (False, False)])

    def _select(r, w, x, timeout):
        ticks["n"] += 1
        if ticks["n"] >= 2:
            daemon.is_running = False
        return ([], [], [])

    monkeypatch.setattr(cl.select, "select", _select)
    monkeypatch.setattr(cl, "_crun_recover_and_prune", lambda: None)
    monkeypatch.setattr(cl, "_crun_record_deferred", lambda cycle, reason: deferred.append((cycle, reason)))
    monkeypatch.setattr(cl, "consolidation_due", lambda *a, **k: next(due))
    monkeypatch.setattr(cl, "sweep_due", lambda *a, **k: True)

    class _Gate:
        def close(self):
            return None

    monkeypatch.setattr(cl, "_try_backup_shared_lock", lambda: _Gate())

    async def _conn(self):
        return object(), object()

    async def _note(self, now):
        return None

    async def _backlog(self, now, force=False):
        return [1]

    async def _wait(self):
        return False

    async def _fold(self, backlog):
        ran.append("fold")

    async def _lineage(self):
        ran.append("lineage")

    async def _insight(self):
        ran.append("insight")

    async def _ledger(self):
        return None

    monkeypatch.setattr(cl.ConsolidationDaemon, "_make_listen_conn", _conn)
    monkeypatch.setattr(cl.ConsolidationDaemon, "_note_pool_activity", _note)
    monkeypatch.setattr(cl.ConsolidationDaemon, "_refresh_backlog", _backlog)
    monkeypatch.setattr(cl.ConsolidationDaemon, "_wait_for_slot", _wait)
    monkeypatch.setattr(cl.ConsolidationDaemon, "run_consolidation_cycle", _fold)
    monkeypatch.setattr(cl.ConsolidationDaemon, "run_lineage_invalidation_pass", _lineage)
    monkeypatch.setattr(cl.ConsolidationDaemon, "run_insight_cycle", _insight)
    monkeypatch.setattr(cl.ConsolidationDaemon, "run_ledger_sweep", _ledger)

    daemon = cl.ConsolidationDaemon()
    daemon.is_running = True
    daemon._startup_sweep_done = True
    daemon.last_busy = datetime.now()
    daemon.last_sweep_time = datetime.now() - timedelta(hours=1)
    daemon.last_activity = datetime.now()
    try:
        asyncio.run(daemon.listen_for_events())
    finally:
        server.shutdown()
        server.server_close()
    assert ran == ["fold", "lineage", "insight"]
    assert not any(reason.startswith("pool_busy") for _cycle, reason in deferred)

    class _Resp:
        status_code = 422
        headers = {"X-SM-Fault-Origin": "gateway"}

        def json(self):
            return {"error": "no_eligible_backend", "constraint": "no_backend"}

    async def _post(client, payload, **kwargs):
        return _Resp()

    monkeypatch.setattr(cl, "_post_nrem", _post)
    daemon._last_llm_truncated = False
    result = asyncio.run(daemon._call_insight_llm("prompt", "entity", 1))
    assert result is None
    assert daemon._last_llm_truncated is False
    refusal = cl._routing_refusal(_Resp())
    assert refusal["constraint"] == "no_backend"
    assert "role" not in refusal
