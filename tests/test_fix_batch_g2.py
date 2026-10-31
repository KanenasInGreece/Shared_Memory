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

def test_t6_migrate_env_report_ordering_and_omission():
    """The note follows a token_missing exclusion when the unit supplies that name. Anything else prints nothing."""
    missing = {"llm_backends_excluded": [{"url": "http://a:5000", "reason": "token_missing"}]}
    other = {"llm_backends_excluded": [{"url": "http://a:5000", "reason": "token_file_empty"}]}
    uq_matching = m.UnitQuery("ok", credential_ids=frozenset({"g2_token"}))
    line = m._hidden_credential_line(uq_matching, missing, ["G2_TOKEN"])
    assert line is not None
    assert "G2_TOKEN" in line
    assert "token_missing" in line
    assert "SENTINEL" not in line

    uq_pg = m.UnitQuery("ok", credential_ids=frozenset({"pg_password"}))
    assert m._hidden_credential_line(uq_pg, missing, ["G2_TOKEN"]) is None
    assert m._hidden_credential_line(uq_matching, other, ["G2_TOKEN"]) is None
    assert m._hidden_credential_line(uq_matching, missing, []) is None


# ── T7: check_config in-process with fake proxy ──────────────────────────────

def test_t7_check_config_in_process_fake_proxy(monkeypatch):
    """An excluded token_missing entry names the unit-supplied variable. A miss, a raised query, or a visible secret prints no note and no secret."""
    class _FakeProxy:
        LLM_BACKENDS = []
        LLM_BACKENDS_EXCLUDED = [{"url": "http://a:5000", "reason": "token_missing"}]
        LLM_WEIGHTS = {}
        LLM_BACKEND_MODELS = {}
        LLM_BACKEND_NCTX = {}
        LLM_BACKEND_MAX_OUTPUT = {}
        LLM_BACKEND_PRIORITY = {}
        LLM_BACKEND_TOKENS = {}

        @staticmethod
        def require_auth_when_provider_keys_configured():
            return None

        @staticmethod
        def require_valid_llm_routing_config():
            return None

    fake_proxy = _FakeProxy()
    monkeypatch.setitem(sys.modules, "hive_mind_proxy", fake_proxy)
    monkeypatch.delenv("G2_TEST_TOKEN", raising=False)
    monkeypatch.setenv(
        "LLM_BACKENDS_JSON",
        json.dumps([{"url": "http://a:5000", "token_env": "G2_TEST_TOKEN", "n_ctx": 8192, "max_output_tokens": 2048}]),
    )

    monkeypatch.setattr(
        m,
        "query_gateway_unit",
        lambda u: m.UnitQuery("ok", credential_ids=frozenset({"g2_test_token"})),
    )
    lines, code = check_config.phase_b_render()
    assert code == 0
    body = "\n".join(lines)
    assert "reason=token_missing" in body
    assert "note: G2_TEST_TOKEN is supplied by the gateway's systemd unit" in body
    excluded_at = body.index("reason=token_missing")
    assert excluded_at < body.index("note: G2_TEST_TOKEN")

    monkeypatch.setattr(
        m,
        "query_gateway_unit",
        lambda u: m.UnitQuery("ok", credential_ids=frozenset({"other"})),
    )
    lines2, code2 = check_config.phase_b_render()
    assert code2 == 0
    body2 = "\n".join(lines2)
    assert "reason=token_missing" in body2
    assert "supplied by the gateway's systemd unit" not in body2

    def _boom_query(u):
        raise RuntimeError("SECRETVALUE")

    monkeypatch.setattr(m, "query_gateway_unit", _boom_query)
    lines3, code3 = check_config.phase_b_render()
    assert code3 == 0
    body3 = "\n".join(lines3)
    assert "reason=token_missing" in body3
    assert "supplied by the gateway's systemd unit" not in body3
    assert "SECRETVALUE" not in body3

    monkeypatch.setenv("G2_TEST_TOKEN", "secret-value")
    monkeypatch.setattr(
        m,
        "query_gateway_unit",
        lambda u: m.UnitQuery("ok", credential_ids=frozenset({"g2_test_token"})),
    )
    lines4, code4 = check_config.phase_b_render()
    assert code4 == 0
    body4 = "\n".join(lines4)
    assert "reason=token_missing" in body4
    assert "supplied by the gateway's systemd unit" not in body4
    assert "secret-value" not in body4


# ── T8: check_config subprocess filters error log ────────────────────────────

def test_t8_check_config_subprocess_filters_fallback_error(tmp_path):
    """fails on a7438c8: check_config filters the gateway's no-usable-backend error while keeping token warnings."""
    from test_check_config import _run

    backends_json = json.dumps([{"url": "http://a:5000", "token_env": "G2_ABSENT_TOKEN", "n_ctx": 8192, "max_output_tokens": 2048}])
    proc = _run(
        env_overrides={
            "SECURE_ENV_FILE": "",
            "LLM_BACKENDS_JSON": backends_json,
        },
        tmp_path=tmp_path,
    )
    assert "falling back to LLM_BACKENDS/LLM_DEFAULT_TARGET" not in proc.stderr
    assert "reason=token_missing" in proc.stdout
    assert "G2_ABSENT_TOKEN" in proc.stderr
    assert "sk-" not in proc.stdout


# ── T9: HIDDEN_CREDENTIAL_NOTE value assert ──────────────────────────────────

def test_t9_hidden_credential_note_exact_wording():
    """pins the new contract: HIDDEN_CREDENTIAL_NOTE format string matches the exact wording specified in M1.6."""
    expected = (
        "note: A is supplied by the gateway's systemd unit (a credential directive or a secret "
        "Environment= value), which this check cannot see, so the entry is excluded with token_missing. "
        "The running gateway reads it from the unit; the authenticated /memory/telemetry "
        "shows the gateway's own roster, with has_credential per backend, under config.llm_backends."
    )
    assert m.HIDDEN_CREDENTIAL_NOTE.format(names="A", verb="is") == expected
    assert m.format_hidden_credential_note(["A"]) == expected
    assert "sk-" not in expected
    expected_multi = (
        "note: A, B are supplied by the gateway's systemd unit (a credential directive or a secret "
        "Environment= value), which this check cannot see, so the entry is excluded with token_missing. "
        "The running gateway reads it from the unit; the authenticated /memory/telemetry "
        "shows the gateway's own roster, with has_credential per backend, under config.llm_backends."
    )
    assert m.format_hidden_credential_note(["A", "B"]) == expected_multi
    assert m.extract_token_env_names(json.dumps([{"token_env": "A"}, {"token_env": "B"}])) == ["A", "B"]
    assert m.extract_token_env_names(None) == []


