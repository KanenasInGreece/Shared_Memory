"""Fix round batch G2 (v1.0.38): tests T1 through T9.
The update checks ask systemd which credentials the gateway's unit holds, and say
a hidden credential is the cause only when it is the one the fleet needs (fact:3280, fact:3279).
"""
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "shared-memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "tests"))

import check_config  # noqa: E402
import migrate_env as m  # noqa: E402
import secure_env  # noqa: E402


# ── T1: query_gateway_unit queries busctl for credentials ───────────────────

def test_t1_query_gateway_unit_asks_busctl_for_credentials(monkeypatch):
    """fails on a7438c8: query_gateway_unit queries busctl per property for credential ids instead of systemctl show."""
    invocations = []

    class _Proc:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _run(argv, **kw):
        invocations.append(list(argv))
        base = os.path.basename(argv[0])
        if base == "systemctl":
            unit_arg = argv[3]
            unit_id = f"{unit_arg}.service" if not unit_arg.endswith(".service") else unit_arg
            return _Proc(
                0,
                f"Environment=\nEnvironmentFiles=\nId={unit_id}\nLoadCredential=[unprintable]\nLoadCredentialEncrypted=[unprintable]\n",
            )
        elif base == "busctl":
            return _Proc(0, '{"type":"a(ss)","data":[]}\n')
        return _Proc(1, "")

    monkeypatch.setattr(m.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(m.subprocess, "run", _run)

    q = m.query_gateway_unit("hive-mind-gateway.service")
    assert q.credential_ids == frozenset()
    assert not hasattr(q, "load_credentials")

    systemctl_calls = [cmd for cmd in invocations if "systemctl" in cmd[0]]
    assert len(systemctl_calls) == 1
    show_argv = systemctl_calls[0]
    assert "LoadCredential" not in show_argv
    assert "LoadCredentialEncrypted" not in show_argv
    assert "-p" in show_argv and "Id" in show_argv

    busctl_calls = [cmd for cmd in invocations if "busctl" in cmd[0]]
    assert len(busctl_calls) == len(m.CREDENTIAL_PROPERTIES)
    queried_props = [cmd[-1] for cmd in busctl_calls]
    for prop in m.CREDENTIAL_PROPERTIES:
        assert prop in queried_props
    assert all("/org/freedesktop/systemd1/unit/hive_2dmind_2dgateway_2eservice" in cmd for cmd in busctl_calls)

    # Bare unit name relying on Id= returned by systemctl show
    invocations.clear()
    q_bare = m.query_gateway_unit("hive-mind-gateway")
    assert q_bare.credential_ids == frozenset()
    busctl_bare_calls = [cmd for cmd in invocations if "busctl" in cmd[0]]
    assert len(busctl_bare_calls) == len(m.CREDENTIAL_PROPERTIES)
    assert all("/org/freedesktop/systemd1/unit/hive_2dmind_2dgateway_2eservice" in cmd for cmd in busctl_bare_calls)

    # Unit given as my.gateway, show stub prints Id=my.gateway.service -> /org/freedesktop/systemd1/unit/my_2egateway_2eservice
    invocations.clear()
    q_dotted = m.query_gateway_unit("my.gateway")
    assert q_dotted.credential_ids == frozenset()
    busctl_dotted_calls = [cmd for cmd in invocations if "busctl" in cmd[0]]
    assert len(busctl_dotted_calls) == len(m.CREDENTIAL_PROPERTIES)
    assert all("/org/freedesktop/systemd1/unit/my_2egateway_2eservice" in cmd for cmd in busctl_dotted_calls)

    # No Id= line: busctl is never called and credential_ids is frozenset()
    invocations.clear()
    def _run_no_id(argv, **kw):
        invocations.append(list(argv))
        if "systemctl" in argv[0]:
            return _Proc(0, "Environment=\nEnvironmentFiles=\n")
        return _Proc(0, '{"type":"a(ss)","data":[]}\n')

    monkeypatch.setattr(m.subprocess, "run", _run_no_id)
    q_no_id = m.query_gateway_unit("hive-mind-gateway.service")
    assert q_no_id.status == "ok"
    assert q_no_id.credential_ids == frozenset()
    assert not any("busctl" in cmd[0] for cmd in invocations)


# ── T2: busctl parses a LoadCredential shape, SetCredential, and ImportCredential ───────

def test_t2_busctl_parses_load_set_and_import_shapes():
    """pins the new contract: _parse_credential_property decodes a(ss), a(say), and as busctl shapes, including ImportCredentialEx rename."""
    load_line = '{"type":"a(ss)","data":[["deepseek_api_key","/x"]]}\n'
    assert m._parse_credential_property("LoadCredential", load_line) == frozenset({"deepseek_api_key"})

    set_line = '{"type":"a(say)","data":[["k",[115,107]]]}\n'
    assert m._parse_credential_property("SetCredential", set_line) == frozenset({"k"})

    import_line = '{"type":"as","data":["dee*"]}\n'
    assert m._parse_credential_property("ImportCredential", import_line) == frozenset({"dee*"})

    # ImportCredentialEx rename rows (R3)
    ex_rename_glob = '{"type":"a(ss)","data":[["deepseek_*","x_"]]}\n'
    parsed_ex1 = m._parse_credential_property("ImportCredentialEx", ex_rename_glob)
    assert parsed_ex1 == frozenset({"x_*"})
    uq_ex1 = m.UnitQuery("ok", credential_ids=parsed_ex1)
    assert m.hidden_credentials(uq_ex1, ["DEEPSEEK_API_KEY"]) == []

    ex_prefix_match = '{"type":"a(ss)","data":[["a.*","deepseek_"]]}\n'
    parsed_ex2 = m._parse_credential_property("ImportCredentialEx", ex_prefix_match)
    assert parsed_ex2 == frozenset({"deepseek_*"})
    uq_ex2 = m.UnitQuery("ok", credential_ids=parsed_ex2)
    assert m.hidden_credentials(uq_ex2, ["DEEPSEEK_API_KEY"]) == ["DEEPSEEK_API_KEY"]

    # When rename is empty, keep original [0]
    ex_no_rename = '{"type":"a(ss)","data":[["deepseek_*",""]]}\n'
    assert m._parse_credential_property("ImportCredentialEx", ex_no_rename) == frozenset({"deepseek_*"})


# ── T3: _parse_credential_property failure rows and busctl error resilience ──

def test_t3_parse_credential_property_and_busctl_failure_handling(monkeypatch):
    """fails on a7438c8: busctl per-property stubs, Unknown property resilience, and failure handling."""
    assert m._parse_credential_property("LoadCredential", "garbage line not json") is None
    assert m._parse_credential_property("LoadCredential", '{"type":"a(ss)","data":"x"}') is None
    assert m._parse_credential_property("LoadCredential", "") == frozenset()
    assert m._parse_credential_property("LoadCredential", "   \n\n  ") == frozenset()

    class _Proc:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    # 1. busctl missing -> empty credential_ids with status ok
    monkeypatch.setattr(
        m.shutil,
        "which",
        lambda name: "/usr/bin/systemctl" if name == "systemctl" else None,
    )
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda argv, **kw: _Proc(0, "Environment=\nEnvironmentFiles=\nId=u.service\n"),
    )
    q1 = m.query_gateway_unit("u.service")
    assert q1.status == "ok"
    assert q1.credential_ids == frozenset()

    # 2. ImportCredentialEx returns rc 1 with stderr "Unknown property", and LoadCredential returns [["deepseek_api_key","/x"]] -> {"deepseek_api_key"}
    def _run_unknown_prop(argv, **kw):
        if "systemctl" in argv[0]:
            return _Proc(0, "Environment=\nEnvironmentFiles=\nId=u.service\n")
        prop = argv[-1]
        if prop == "ImportCredentialEx":
            return _Proc(
                1,
                "",
                stderr="Failed to get property ImportCredentialEx on interface org.freedesktop.systemd1.Service: Unknown interface org.freedesktop.systemd1.Service or property ImportCredentialEx.",
            )
        elif prop == "LoadCredential":
            return _Proc(0, '{"type":"a(ss)","data":[["deepseek_api_key","/x"]]}\n')
        return _Proc(0, '{"type":"a(ss)","data":[]}\n')

    monkeypatch.setattr(m.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(m.subprocess, "run", _run_unknown_prop)
    q2 = m.query_gateway_unit("u.service")
    assert q2.status == "ok"
    assert q2.credential_ids == frozenset({"deepseek_api_key"})

    # 3. Non-"Unknown" failure on any property -> frozenset()
    def _run_generic_failure(argv, **kw):
        if "systemctl" in argv[0]:
            return _Proc(0, "Environment=\nEnvironmentFiles=\nId=u.service\n")
        prop = argv[-1]
        if prop == "SetCredential":
            return _Proc(1, "", stderr="Failed to get property: AccessDenied")
        return _Proc(0, '{"type":"a(ss)","data":[]}\n')

    monkeypatch.setattr(m.subprocess, "run", _run_generic_failure)
    q3 = m.query_gateway_unit("u.service")
    assert q3.status == "ok"
    assert q3.credential_ids == frozenset()

    # 4. busctl OSError -> frozenset()
    def _run_oserror(argv, **kw):
        if "systemctl" in argv[0]:
            return _Proc(0, "Environment=\nEnvironmentFiles=\nId=u.service\n")
        raise OSError("busctl failed to execute")

    monkeypatch.setattr(m.subprocess, "run", _run_oserror)
    q4 = m.query_gateway_unit("u.service")
    assert q4.status == "ok"
    assert q4.credential_ids == frozenset()


# ── T4: _unit_object_path escape ─────────────────────────────────────────────

def test_t4_unit_object_path_escape():
    """pins the new contract: _unit_object_path applies systemd bus-label escape including leading digit rule."""
    assert (
        m._unit_object_path("hive-mind-gateway.service")
        == "/org/freedesktop/systemd1/unit/hive_2dmind_2dgateway_2eservice"
    )
    assert (
        m._unit_object_path("1x.service")
        == "/org/freedesktop/systemd1/unit/_31x_2eservice"
    )


# ── T5: hidden_credentials filtering and matching ───────────────────────────

def test_t5_hidden_credentials_filtering_and_matching():
    """fails on a7438c8: hidden_credentials filters unresolved token_env_names and matches credential_ids / secret env keys."""
    # Row 1: ids {"deepseek_api_key"}, names ["DEEPSEEK_API_KEY"], already unresolved -> ["DEEPSEEK_API_KEY"]
    uq1 = m.UnitQuery("ok", credential_ids=frozenset({"deepseek_api_key"}))
    assert m.hidden_credentials(uq1, ["DEEPSEEK_API_KEY"]) == ["DEEPSEEK_API_KEY"]

    # Row 2: ids {"pg_password"}, names ["DEEPSEEK_API_KEY"] -> [] (O-3/G2-2 false-note case)
    uq2 = m.UnitQuery("ok", credential_ids=frozenset({"pg_password"}))
    assert m.hidden_credentials(uq2, ["DEEPSEEK_API_KEY"]) == []

    # Row 3: glob {"deepseek_*"} -> matched
    uq3 = m.UnitQuery("ok", credential_ids=frozenset({"deepseek_*"}))
    assert m.hidden_credentials(uq3, ["DEEPSEEK_API_KEY"]) == ["DEEPSEEK_API_KEY"]

    # Row 4: a secret Environment key DEEPSEEK_API_KEY -> matched
    uq4 = m.UnitQuery("ok", environment={"DEEPSEEK_API_KEY": "sk-secret"})
    assert m.hidden_credentials(uq4, ["DEEPSEEK_API_KEY"]) == ["DEEPSEEK_API_KEY"]

    # Row 5: a non-secret Environment key -> []
    assert secure_env.is_secret_key("SOME_SETTING") is False
    uq5 = m.UnitQuery("ok", environment={"SOME_SETTING": "val"})
    assert m.hidden_credentials(uq5, ["SOME_SETTING"]) == []

    # Row 6: status query_failed -> []
    uq6 = m.UnitQuery("query_failed", credential_ids=frozenset({"deepseek_api_key"}))
    assert m.hidden_credentials(uq6, ["DEEPSEEK_API_KEY"]) == []


# ── T6: migrate_env report ordering and omission ─────────────────────────────

def test_t6_migrate_env_report_ordering_and_omission(tmp_path, monkeypatch, capsys):
    """fails on a7438c8: migrate_env reports both fallback reason and hidden credential note in order, omitting note for non-matching ids."""
    token_json = json.dumps([{"url": "http://a:5000", "token_env": "G2_TOKEN"}])
    reason = "LLM_BACKENDS_JSON produced no usable backend (every entry excluded) — SENTINEL"

    def _run_case(unit_q, unresolved=None):
        env_path = tmp_path / ".env"
        env_path.write_text(f"LLM_BACKENDS_JSON={token_json}\n")
        monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
        monkeypatch.setattr(m, "query_gateway_unit", lambda u: unit_q)
        pre = tmp_path / f"pre_{os.urandom(4).hex()}.json"
        img = {
            "urls": [],
            "fallback_reason": reason,
            "config_empty": False,
        }
        if unresolved is not None:
            img["token_env_unresolved"] = unresolved
        pre.write_text(
            json.dumps(
                {
                    "capture_schema": m.CAPTURE_SCHEMA,
                    "image": img,
                }
            )
        )
        rc = m.do_apply_or_dryrun(str(pre), False, "unit.service")
        assert rc == m.EXIT_OK
        return capsys.readouterr().out

    # Case 1: matching token_env -> old fallback text AND the note, in that order
    uq_matching = m.UnitQuery("ok", credential_ids=frozenset({"g2_token"}))
    out1 = _run_case(uq_matching, unresolved=["G2_TOKEN"])
    assert "SENTINEL" in out1
    assert "is supplied by the gateway's systemd unit" in out1
    assert out1.index("SENTINEL") < out1.index("is supplied by the gateway's systemd unit")
    assert "unaffected" not in out1
    assert "Nothing was changed" not in out1

    # Case 2: {"pg_password"} -> old text and no note
    uq_pg = m.UnitQuery("ok", credential_ids=frozenset({"pg_password"}))
    out2 = _run_case(uq_pg, unresolved=["G2_TOKEN"])
    assert "SENTINEL" in out2
    assert "is supplied by the gateway's systemd unit" not in out2
    assert "unaffected" not in out2
    assert "Nothing was changed" not in out2

    # Case 3 (new T6 row, P-1): matching id {"g2_token"} but token_env_unresolved is [] -> old text and no note
    out3 = _run_case(uq_matching, unresolved=[])
    assert "SENTINEL" in out3
    assert "is supplied by the gateway's systemd unit" not in out3


# ── T7: check_config in-process with fake proxy ──────────────────────────────

def test_t7_check_config_in_process_fake_proxy(monkeypatch):
    """fails on a7438c8: check_config phase_b_render appends note directly after DECLARED FLEET line when token matches, safe on error."""
    class _FakeProxy:
        LLM_POOL_FALLBACK_REASON = "SENTINEL reason"
        LLM_POOL_CONFIG_EMPTY = False
        LLM_BACKENDS = []
        LLM_WEIGHTS = {}
        LLM_BACKEND_MODELS = {}
        LLM_BACKEND_ROLES = {}
        LLM_BACKEND_EXTRAS = {}
        LLM_BACKEND_NCTX = {}
        LLM_BACKEND_TOKENS = {}
        LLM_BACKEND_PRIVATE_OK = {}
        LLM_BACKEND_PRIVATE_OK_EXPLICIT = {}
        AUTH_CONFIGURED_AT_STARTUP = True
        _LLM_BACKEND_ROLE_CONFIG_ERRORS = []
        DEFAULT_TARGET = ""

        @staticmethod
        def require_auth_when_provider_keys_configured():
            return None

        @staticmethod
        def require_valid_llm_routing_config():
            return None

    fake_proxy = _FakeProxy()
    monkeypatch.setitem(sys.modules, "hive_mind_proxy", fake_proxy)
    monkeypatch.setenv(
        "LLM_BACKENDS_JSON",
        json.dumps([{"url": "http://a:5000", "token_env": "G2_TEST_TOKEN"}]),
    )

    # 1. Matching token: exit 0, DECLARED FLEET line with SENTINEL reason, note naming G2_TEST_TOKEN on next line
    monkeypatch.setattr(
        m,
        "query_gateway_unit",
        lambda u: m.UnitQuery("ok", credential_ids=frozenset({"g2_test_token"})),
    )
    lines, code = check_config.phase_b_render()
    assert code == 0
    declared_idx = None
    for i, line in enumerate(lines):
        if "⚠ DECLARED FLEET NOT USABLE" in line and "SENTINEL reason" in line:
            declared_idx = i
            break
    assert declared_idx is not None, f"DECLARED FLEET line missing: {lines}"
    assert declared_idx + 1 < len(lines)
    assert "note: G2_TEST_TOKEN is supplied by the gateway's systemd unit" in lines[declared_idx + 1]

    # 2. Other token: no note
    monkeypatch.setattr(
        m,
        "query_gateway_unit",
        lambda u: m.UnitQuery("ok", credential_ids=frozenset({"other"})),
    )
    lines2, code2 = check_config.phase_b_render()
    assert code2 == 0
    body2 = "\n".join(lines2)
    assert "⚠ DECLARED FLEET NOT USABLE" in body2
    assert "supplied by the gateway's systemd unit" not in body2

    # 3. query_gateway_unit raises RuntimeError("SECRETVALUE"): no note, SECRETVALUE absent
    def _boom_query(u):
        raise RuntimeError("SECRETVALUE")

    monkeypatch.setattr(m, "query_gateway_unit", _boom_query)
    lines3, code3 = check_config.phase_b_render()
    assert code3 == 0
    body3 = "\n".join(lines3)
    assert "⚠ DECLARED FLEET NOT USABLE" in body3
    assert "supplied by the gateway's systemd unit" not in body3
    assert "SECRETVALUE" not in body3

    # 4. G2_TEST_TOKEN set in env, matching id -> no note (unseen filter)
    monkeypatch.setenv("G2_TEST_TOKEN", "secret-value")
    monkeypatch.setattr(
        m,
        "query_gateway_unit",
        lambda u: m.UnitQuery("ok", credential_ids=frozenset({"g2_test_token"})),
    )
    lines4, code4 = check_config.phase_b_render()
    assert code4 == 0
    body4 = "\n".join(lines4)
    assert "⚠ DECLARED FLEET NOT USABLE" in body4
    assert "supplied by the gateway's systemd unit" not in body4


# ── T8: check_config subprocess filters error log ────────────────────────────

def test_t8_check_config_subprocess_filters_fallback_error(tmp_path):
    """fails on a7438c8: check_config filters the gateway's no-usable-backend error while keeping token warnings."""
    from test_check_config import _run

    backends_json = json.dumps([{"url": "http://a:5000", "token_env": "G2_ABSENT_TOKEN"}])
    proc = _run(
        env_overrides={
            "SECURE_ENV_FILE": "",
            "LLM_BACKENDS_JSON": backends_json,
        },
        tmp_path=tmp_path,
    )
    assert "falling back to LLM_BACKENDS/LLM_DEFAULT_TARGET" not in proc.stderr
    assert "DECLARED FLEET NOT USABLE" in proc.stdout
    assert "G2_ABSENT_TOKEN" in proc.stderr


# ── T9: HIDDEN_CREDENTIAL_NOTE value assert ──────────────────────────────────

def test_t9_hidden_credential_note_exact_wording():
    """pins the new contract: HIDDEN_CREDENTIAL_NOTE format string matches the exact wording specified in M1.6."""
    expected = (
        "note: A is supplied by the gateway's systemd unit (a credential directive or a secret "
        "Environment= value), which this check cannot see, so the backend that needs it looks "
        "uncredentialed here. The running gateway reads it from the unit; the authenticated /memory/telemetry "
        "shows the gateway's own roster, with has_credential per backend, under config.llm_backends."
    )
    assert m.HIDDEN_CREDENTIAL_NOTE.format(names="A", verb="is") == expected
    assert m.format_hidden_credential_note(["A"]) == expected
    expected_multi = (
        "note: A, B are supplied by the gateway's systemd unit (a credential directive or a secret "
        "Environment= value), which this check cannot see, so the backend that needs it looks "
        "uncredentialed here. The running gateway reads it from the unit; the authenticated /memory/telemetry "
        "shows the gateway's own roster, with has_credential per backend, under config.llm_backends."
    )
    assert m.format_hidden_credential_note(["A", "B"]) == expected_multi
    assert m.extract_token_env_names(json.dumps([{"token_env": "A"}, {"token_env": "B"}])) == ["A", "B"]
    assert m.extract_token_env_names(None) == []


# ── Loader subprocess: image reports token_env_unresolved ────────────────────

def test_loader_snippet_reports_token_env_unresolved(tmp_path):
    """fails on a7438c8: _LOADER_SNIPPET reports token_env_unresolved in image for unresolved tokens only."""
    snippet = m._LOADER_SNIPPET.format(here=str(SCRIPTS), loader_semantics=m.LOADER_SEMANTICS)
    backends_json = json.dumps([{"url": "http://a:5000", "token_env": "G2_ABSENT_TOKEN"}])

    # 1. G2_ABSENT_TOKEN not set -> token_env_unresolved == ["G2_ABSENT_TOKEN"]
    env1 = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "LLM_BACKENDS_JSON": backends_json,
        "SECURE_ENV_FILE": "",
    }
    proc1 = subprocess.run([sys.executable, "-c", snippet], env=env1, capture_output=True, text=True, timeout=60)
    assert proc1.returncode == 0
    image1 = json.loads(proc1.stdout.strip().splitlines()[-1])
    assert image1.get("token_env_unresolved") == ["G2_ABSENT_TOKEN"]

    # 2. G2_ABSENT_TOKEN set -> token_env_unresolved == []
    env2 = dict(env1)
    env2["G2_ABSENT_TOKEN"] = "x"
    proc2 = subprocess.run([sys.executable, "-c", snippet], env=env2, capture_output=True, text=True, timeout=60)
    assert proc2.returncode == 0
    image2 = json.loads(proc2.stdout.strip().splitlines()[-1])
    assert image2.get("token_env_unresolved") == []

    # 3. G2_ABSENT_TOKEN reaches child through a .env file -> token_env_unresolved == []
    env_file = tmp_path / "test.env"
    env_file.write_text("G2_ABSENT_TOKEN=x\n")
    env3 = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "LLM_BACKENDS_JSON": backends_json,
        "SECURE_ENV_FILE": str(env_file),
    }
    proc3 = subprocess.run([sys.executable, "-c", snippet], env=env3, capture_output=True, text=True, timeout=60)
    assert proc3.returncode == 0
    image3 = json.loads(proc3.stdout.strip().splitlines()[-1])
    assert image3.get("token_env_unresolved") == []
