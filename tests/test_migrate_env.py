"""migrate_env.py (W3, Backend_Declaration_Spec_2026-08-30 §4, decision:1846)
— the standalone .env migration tool.

⛔ HARNESS DISCIPLINE (W0 T1 pattern — never touches the real .env): every
test that resolves an env file passes its OWN `SECURE_ENV_FILE` (monkeypatch)
pointing at a `tmp_path` fixture file, and every test that writes a backup
monkeypatches `HOME` at a `tmp_path` sentinel too — `backup_env_file()` reads
`~` via `os.path.expanduser`, which honours `HOME`. Nothing in this file ever
reads or writes the operator's real `shared-memory/.env` or
`~/.shared-memory/env-backups/`.

Two kinds of test:
  * PURE unit tests — classify()/plan_*()/two_layer_compare()/parsing helpers
    called directly against constructed image dicts and raw lines. No
    subprocess, no filesystem beyond what a test itself creates.
  * END-TO-END tests — do_capture()/do_apply_or_dryrun() against a real
    fixture .env, using the REAL loader subprocess (this suite's own
    `uv run --with aiohttp --with asyncpg --with httpx --with neo4j ...`
    invocation gives that subprocess the same deps). `_force_no_unit()`
    monkeypatches `shutil.which` so every end-to-end test is deterministic
    regardless of whether the host running the suite has a systemd --user
    session at all — the unit-owned/query-failed branches are instead
    exercised directly against STUBBED subprocess.run() output (the env
    corpus itself cannot reach them, matching the W3 brief's own note).
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import migrate_env as m  # noqa: E402




def _force_no_unit(monkeypatch):
    """Deterministic 'no systemd unit' status regardless of the host running
    this suite — see module docstring."""
    monkeypatch.setattr(m.shutil, "which", lambda name: None)


def _write_env(tmp_path, text):
    p = tmp_path / ".env"
    p.write_text(text)
    return p


def _aged_preimage(tmp_path, gateway_unit=None):
    """Ruling A(a) (§6.4): a REAL --capture-preimage, downgraded to look
    like an OLD-code (pre-W4) capture — private_ok True for every backend
    without an explicit false (the pre-flip default), and no
    "loader_semantics" key (absence reads as 1, the old generation).
    private_ok's default direction is the ONLY thing that changed between
    generations, so this is exactly what an old-code self-capture (or an
    old-code --capture-preimage) would have produced for THIS install —
    used to open migrate_env's loader-semantics boundary in a test without
    hand-maintaining a parallel loader. `gateway_unit` defaults to
    `_NO_UNIT` (a module-level constant elsewhere in this file — resolved
    at call time, so definition order does not matter)."""
    pre_path = tmp_path / "pre.json"
    rc = m.do_capture(str(pre_path), gateway_unit or _NO_UNIT)
    assert rc == m.EXIT_OK, "aging fixture: capture itself failed"
    payload = json.loads(pre_path.read_text())
    image = payload["image"]
    explicit = image.get("private_ok_explicit", {})
    private_ok = image.get("private_ok", {})
    for url in image.get("urls", []):
        if not explicit.get(url):
            private_ok[url] = True   # the pre-W4 default this url would have carried
    image.pop("loader_semantics", None)
    pre_path.write_text(json.dumps(payload))
    return str(pre_path)


# ─────────────────────────────────────────────────────────────────────────
# systemctl output parsing (pure)
# ─────────────────────────────────────────────────────────────────────────

def test_parse_systemctl_environment_splits_space_separated_pairs():
    raw = "Environment=PATH=/usr/bin FOO=bar\n"
    assert m._parse_systemctl_environment(raw) == {"PATH": "/usr/bin", "FOO": "bar"}


def test_parse_systemctl_environment_empty_line_yields_nothing():
    assert m._parse_systemctl_environment("Environment=\n") == {}


def test_parse_systemctl_environment_handles_real_shell_quoting_json_value():
    """SEC H-1 (fix round): taken verbatim from a real `systemctl show -p
    Environment` run against a stock unit whose value embeds double
    quotes (exactly LLM_BACKENDS_JSON's own shape — it ALWAYS contains
    `"`, so systemd ALWAYS shell-quotes it). PRE-FIX, the naive
    `str.split()` parser tokenized the leading `"` into the key name
    itself (`'"LLM_BACKENDS_JSON'`), which matches no MANAGED_KEYS name —
    the R-C unit-ownership gate silently never fired for the one key it
    matters most for. Verified failing against the pre-fix parser before
    this fix landed (recorded in the fix-round report, not re-asserted
    here as a second implementation)."""
    raw = ('Environment="LLM_BACKENDS_JSON=[{\\"url\\": \\"http://a:5000\\", '
           '\\"weight\\": 1}]"\n')
    result = m._parse_systemctl_environment(raw)
    assert result == {"LLM_BACKENDS_JSON": '[{"url": "http://a:5000", "weight": 1}]'}


def test_parse_systemctl_environment_handles_real_shell_quoting_space_value():
    """The second real-shaped fixture SEC H-1 cited: a value containing a
    literal space, also shell-quoted by systemd (measured on a live host
    against breakpoint-pre-basic.service)."""
    raw = 'Environment="SHELL_PROMPT_PREFIX=pre-basic "\n'
    result = m._parse_systemctl_environment(raw)
    assert result == {"SHELL_PROMPT_PREFIX": "pre-basic "}


def test_parse_systemctl_environment_unbalanced_quoting_fails_closed():
    """A line shlex genuinely cannot tokenize raises _EnvironmentParseError
    rather than silently guessing — the caller (query_gateway_unit) turns
    this into query_failed, declining every write."""
    with pytest.raises(m._EnvironmentParseError):
        m._parse_systemctl_environment('Environment="UNBALANCED=value\n')


def test_query_gateway_unit_unparseable_environment_line_is_query_failed(monkeypatch):
    """query_gateway_unit()'s own fail-closed wiring for the parser above."""
    monkeypatch.setattr(m.shutil, "which", lambda name: "/usr/bin/systemctl")

    class _Proc:
        returncode = 0
        stdout = 'Environment="UNBALANCED=value\n'
        stderr = ""

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _Proc())
    q = m.query_gateway_unit("hive-mind-gateway.service")
    assert q.status == "query_failed"


def test_parse_systemctl_environment_files_extracts_bare_path():
    raw = "EnvironmentFiles=/etc/foo/bar.env (ignore_errors=no)\n"
    assert m._parse_systemctl_environment_files(raw) == [("/etc/foo/bar.env", False)]
    optional = "EnvironmentFiles=-/x (ignore_errors=yes)\nEnvironmentFiles=/y (ignore_errors=yes)\n"
    assert m._parse_systemctl_environment_files(optional) == [("/x", True), ("/y", True)]


def test_query_gateway_unit_no_systemctl_proceeds_normally(monkeypatch):
    monkeypatch.setattr(m.shutil, "which", lambda name: None)
    q = m.query_gateway_unit("hive-mind-gateway.service")
    assert q.status == "no_systemctl"
    assert q.owned_keys == frozenset()


def test_query_gateway_unit_ok_reports_owned_managed_keys(monkeypatch, tmp_path):
    """The unit-owned branch — stubbed systemctl show output, per the W3
    brief's own note that the env corpus cannot reach this. SEC H-1 (fix
    round): the fixture is now the REAL shell-quoted shape systemd
    actually emits for a value containing double quotes — the original
    cut of this test used an unquoted form systemd would never produce,
    which is exactly why the parser bug went undetected."""
    monkeypatch.setattr(m.shutil, "which", lambda name: "/usr/bin/systemctl")

    class _Proc:
        returncode = 0
        stdout = ('Environment="LLM_BACKENDS_JSON=[{\\"url\\": \\"http://a:5000\\"}]" FOO=bar\n'
                   'EnvironmentFiles=\n')
        stderr = ""

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _Proc())
    q = m.query_gateway_unit("hive-mind-gateway.service")
    assert q.status == "ok"
    assert q.owned_keys == {"LLM_BACKENDS_JSON"}


def test_query_gateway_unit_nonzero_exit_is_query_failed(monkeypatch):
    monkeypatch.setattr(m.shutil, "which", lambda name: "/usr/bin/systemctl")

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "Unit not found"

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _Proc())
    q = m.query_gateway_unit("hive-mind-gateway.service")
    assert q.status == "query_failed"


def test_query_gateway_unit_unreadable_environment_file_is_query_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(m.shutil, "which", lambda name: "/usr/bin/systemctl")

    class _Proc:
        returncode = 0
        stdout = f"Environment=\nEnvironmentFiles={tmp_path / 'nope.env'} (ignore_errors=no)\n"
        stderr = ""

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _Proc())
    q = m.query_gateway_unit("hive-mind-gateway.service")
    assert q.status == "query_failed"


def test_query_gateway_unit_reads_managed_key_names_only_from_environment_file(tmp_path, monkeypatch):
    monkeypatch.setattr(m.shutil, "which", lambda name: "/usr/bin/systemctl")
    env_file = tmp_path / "unit.env"
    env_file.write_text("LLM_BACKENDS_JSON=[{\"url\":\"http://a:5000\"}]\nSOME_OTHER=1\n")

    class _Proc:
        returncode = 0
        stdout = f"Environment=\nEnvironmentFiles={env_file} (ignore_errors=no)\n"
        stderr = ""

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _Proc())
    q = m.query_gateway_unit("hive-mind-gateway.service")
    assert q.status == "ok"
    assert q.owned_keys == {"LLM_BACKENDS_JSON"}


def test_build_faithful_env_excludes_secret_classified_unit_environment_keys():
    """SEC L-4 (fix round): a secret-classified name delivered via the
    unit's OWN `Environment=` line (e.g. a provider key as
    `Environment=DEEPSEEK_API_KEY=...`) must never reach the loader
    subprocess's environment — copying it wholesale would put a secret in
    that child's own /proc/<pid>/environ, the exact widening secure_env's
    split-env design (PR A1) exists to prevent everywhere else. Config
    keys in the same Environment= line are unaffected."""
    q = m.UnitQuery("ok", owned_keys=set(),
                     environment={"DEEPSEEK_API_KEY": "should-never-reach-the-child",
                                   "SOME_CONFIG_VAR": "fine-to-pass-through"})
    faithful = m.build_faithful_env(q)
    assert "DEEPSEEK_API_KEY" not in faithful
    assert faithful.get("SOME_CONFIG_VAR") == "fine-to-pass-through"


# ─────────────────────────────────────────────────────────────────────────
# Raw env-file parsing (pure)
# ─────────────────────────────────────────────────────────────────────────




# ─────────────────────────────────────────────────────────────────────────
# verify_only_planned_keys_changed — the FILE-LEVEL check (SEC M-4 / QA MED-9)
# ─────────────────────────────────────────────────────────────────────────









# ─────────────────────────────────────────────────────────────────────────
# The decision ladder — classify() (pure, all 5 cases)
# ─────────────────────────────────────────────────────────────────────────

def _apply_env(monkeypatch, tmp_path, **env):
    """do_apply reads the env file the gateway will read. The operator shell is not that file."""
    _force_no_unit(monkeypatch)
    for key in ("LLM_BACKENDS_JSON", "LLM_BACKENDS", "LLM_DEFAULT_TARGET", "AGENT_TOKENS"):
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / "gateway.env"
    env_file.write_text("".join(f"{key}={value}\n" for key, value in env.items()))
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_file))
    return m.do_apply("hive-mind-gateway-does-not-exist.service")


def test_case_0_present_but_empty_json_is_usable(monkeypatch, tmp_path, capsys):
    """An empty JSON value declares no backend. The validator writes nothing."""
    assert _apply_env(monkeypatch, tmp_path, LLM_BACKENDS_JSON="") == m.EXIT_OK
    assert "Nothing was written" in capsys.readouterr().out


def test_case_2_unusable_json_stops_and_does_not_read_the_csv(monkeypatch, tmp_path, capsys):
    """Bad JSON stops the update. A live CSV line beside it is not a migration."""
    assert _apply_env(
        monkeypatch, tmp_path, LLM_BACKENDS_JSON="{not json", LLM_BACKENDS="http://a:5000",
    ) == m.EXIT_STOP
    out = capsys.readouterr().out
    assert "field json" in out
    assert "Nothing was written" not in out


def test_case_4_neither_key_is_no_backend(monkeypatch, tmp_path, capsys):
    """No declaration is a running gateway with no reasoning, not a fallback target."""
    assert _apply_env(monkeypatch, tmp_path) == m.EXIT_OK
    out = capsys.readouterr().out
    assert "Nothing was written" in out
    assert "LLM_DEFAULT_TARGET" not in out


_GOOD_JSON = json.dumps([{"url": "http://a:5000", "n_ctx": 8192, "max_output_tokens": 2048}])
_BAD_JSON = json.dumps([{"url": "http://a:5000", "max_output_tokens": 1}])


def test_apply_reads_a_reserved_name_from_agent_tokens_file(monkeypatch, tmp_path, capsys):
    """A roster that exists only behind AGENT_TOKENS_FILE is what the gateway loads."""
    digest = "ab" * 32
    roster = tmp_path / "roster"
    roster.write_text(f"consolidation:sha256:{digest}\n")
    env_file = tmp_path / "gateway.env"
    env_file.write_text(f"LLM_BACKENDS_JSON={_GOOD_JSON}\nAGENT_TOKENS_FILE={roster}\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_file))
    monkeypatch.setenv("AGENT_TOKENS", "operator:tok")
    for key in ("LLM_BACKENDS_JSON", "LLM_BACKENDS", "LLM_DEFAULT_TARGET"):
        monkeypatch.delenv(key, raising=False)
    _force_no_unit(monkeypatch)
    picture = m._gateway_env(m.query_gateway_unit("hive-mind-gateway.service"))
    assert digest not in json.dumps(picture)
    assert m.build_faithful_env(m.UnitQuery("no_systemctl")).get("AGENT_TOKENS") is None
    assert m.do_apply("hive-mind-gateway.service") == m.EXIT_STOP
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "consolidation" in text
    assert "field agent" in text
    assert digest not in text


def test_apply_unit_secure_env_file_beats_the_process_file(monkeypatch, tmp_path, capsys):
    """The unit's SECURE_ENV_FILE is the file the gateway will read."""
    good = tmp_path / "good.env"
    good.write_text(f"LLM_BACKENDS_JSON={_GOOD_JSON}\n")
    bad = tmp_path / "bad.env"
    bad.write_text(f"LLM_BACKENDS_JSON={_BAD_JSON}\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(good))
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setattr(
        m, "query_gateway_unit",
        lambda unit: m.UnitQuery("ok", environment={"SECURE_ENV_FILE": str(bad)}),
    )
    assert m.do_apply("hive-mind-gateway.service") == m.EXIT_STOP
    assert "field n_ctx" in capsys.readouterr().out


def test_apply_good_shell_json_does_not_hide_a_bad_file(monkeypatch, tmp_path, capsys):
    """A usable LLM_BACKENDS_JSON in the operator shell is not the gateway's file."""
    bad = tmp_path / "bad.env"
    bad.write_text(f"LLM_BACKENDS_JSON={_BAD_JSON}\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(bad))
    monkeypatch.setenv("LLM_BACKENDS_JSON", _GOOD_JSON)
    _force_no_unit(monkeypatch)
    assert m.do_apply("hive-mind-gateway.service") == m.EXIT_STOP
    assert "field n_ctx" in capsys.readouterr().out


def test_apply_bad_shell_json_does_not_stop_a_good_file(monkeypatch, tmp_path, capsys):
    """A bad LLM_BACKENDS_JSON in the operator shell does not stop a usable file."""
    good = tmp_path / "good.env"
    good.write_text(f"LLM_BACKENDS_JSON={_GOOD_JSON}\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(good))
    monkeypatch.setenv("LLM_BACKENDS_JSON", _BAD_JSON)
    _force_no_unit(monkeypatch)
    assert m.do_apply("hive-mind-gateway.service") == m.EXIT_OK
    assert "Nothing was written" in capsys.readouterr().out


def test_apply_directory_secure_env_file_warns_and_does_not_traceback(monkeypatch, tmp_path, capsys):
    """A directory is not a file. The ordinary refusal loads nothing and does not traceback."""
    bogus = tmp_path / "not-a-file"
    bogus.mkdir()
    monkeypatch.setenv("SECURE_ENV_FILE", str(bogus))
    for key in ("LLM_BACKENDS_JSON", "LLM_BACKENDS", "LLM_DEFAULT_TARGET", "AGENT_TOKENS"):
        monkeypatch.delenv(key, raising=False)
    _force_no_unit(monkeypatch)
    assert m.do_apply("hive-mind-gateway.service") == m.EXIT_OK
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "is not a file" in captured.err
    assert "Nothing was written" in captured.out


def test_apply_lists_an_off_host_backend_and_stays_quiet_for_a_self_hosted_one(monkeypatch, tmp_path, capsys):
    """A declared public address is named once. A private address is not. The notice does not stop the update."""
    secret = "query-secret-must-stay-out"
    entries = [
        {"url": "http://127.0.0.1:9", "n_ctx": 8192, "max_output_tokens": 2048},
        {"url": "http://localhost:9", "n_ctx": 8192, "max_output_tokens": 2048},
        {"url": "http://192.168.1.9:9", "n_ctx": 8192, "max_output_tokens": 2048},
        {"url": "http://[::1]:9", "n_ctx": 8192, "max_output_tokens": 2048},
        {"url": "http://100.64.0.1:9", "n_ctx": 8192, "max_output_tokens": 2048},
        {"url": "http://card", "n_ctx": 8192, "max_output_tokens": 2048},
        {"url": "http://llm.lan:9", "n_ctx": 8192, "max_output_tokens": 2048},
        {"url": "http://box.ts.net:9", "n_ctx": 8192, "max_output_tokens": 2048},
        {"url": f"https://api.example.test/v1?key={secret}", "n_ctx": 8192, "max_output_tokens": 2048},
        {"url": "http://8.8.8.8:9", "n_ctx": 8192, "max_output_tokens": 2048},
    ]
    assert _apply_env(monkeypatch, tmp_path, LLM_BACKENDS_JSON=json.dumps(entries)) == m.EXIT_OK
    out = capsys.readouterr().out
    assert "Nothing was written" in out
    assert out.count("receives corpus text from both daemons; remove it if not trusted") == 2
    assert "https://api.example.test/v1 receives corpus text" in out
    assert "http://8.8.8.8:9 receives corpus text" in out
    assert secret not in out
    assert "127.0.0.1" not in out
    assert "localhost" not in out
    assert "192.168.1.9" not in out
    assert "llm.lan" not in out
    assert "ts.net" not in out


def _stub_show(monkeypatch, stdout):
    class _Proc:
        returncode = 0
        stderr = ""

        def __init__(self):
            self.stdout = stdout

    monkeypatch.setattr(m.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _Proc())


def _environment_assignment(json_text):
    escaped = json_text.replace("\\", "\\\\").replace('"', '\\"')
    return f'Environment="LLM_BACKENDS_JSON={escaped}"\n'


def test_missing_optional_environment_file_is_skipped(monkeypatch, tmp_path, capsys):
    """A missing EnvironmentFile=-/x does not drop Environment=. Bad JSON there stops the update."""
    missing = tmp_path / "no-such.env"
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.delenv("LLM_DEFAULT_TARGET", raising=False)
    monkeypatch.setenv("SECURE_ENV_FILE", "")
    _stub_show(
        monkeypatch,
        _environment_assignment(_BAD_JSON)
        + f"EnvironmentFiles=-{missing} (ignore_errors=yes)\nId=hive-mind-gateway.service\n",
    )
    assert m.do_apply("hive-mind-gateway.service") == m.EXIT_STOP
    out = capsys.readouterr().out
    assert "field n_ctx" in out
    assert "unreadable" not in out
    assert "Nothing was written" not in out
    assert "Traceback" not in out

    capsys.readouterr()
    _stub_show(
        monkeypatch,
        _environment_assignment(_GOOD_JSON)
        + f"EnvironmentFiles={missing} (ignore_errors=yes)\nId=hive-mind-gateway.service\n",
    )
    assert m.do_apply("hive-mind-gateway.service") == m.EXIT_OK
    usable = capsys.readouterr().out
    assert "Nothing was written" in usable
    assert "unreadable" not in usable


def test_unreadable_required_environment_file_is_said_out_loud(monkeypatch, tmp_path, capsys):
    """A required EnvironmentFile that cannot be read is named. The update does not say usable."""
    missing = tmp_path / "required.env"
    marker = "SECRET-IN-ENVIRONMENT-MUST-STAY-OUT"
    monkeypatch.setenv("SECURE_ENV_FILE", "")
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    _stub_show(
        monkeypatch,
        _environment_assignment(_BAD_JSON)
        + f"EnvironmentFiles={missing} (ignore_errors=no)\nId=hive-mind-gateway.service\n",
    )
    assert m.do_apply("hive-mind-gateway.service") == m.EXIT_STOP
    captured = capsys.readouterr()
    assert f"EnvironmentFile {str(missing)!r} unreadable" in captured.out
    assert "Nothing was written" not in captured.out
    assert "Traceback" not in captured.err
    assert marker not in captured.out + captured.err


def test_non_utf8_unit_environment_file_is_refused_without_its_content(monkeypatch, tmp_path, capsys):
    """A unit EnvironmentFile that is not UTF-8 names the path and does not traceback."""
    unit_env = tmp_path / "unit.env"
    marker = "MARKER_NOTUTF8_UNIT"
    unit_env.write_bytes(f"{marker}=value\xff\n".encode("latin-1"))
    monkeypatch.setenv("SECURE_ENV_FILE", "")
    monkeypatch.setenv("LLM_BACKENDS_JSON", _GOOD_JSON)
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.delenv("LLM_DEFAULT_TARGET", raising=False)
    _stub_show(
        monkeypatch,
        f"Environment=\nEnvironmentFiles={unit_env} (ignore_errors=no)\nId=hive-mind-gateway.service\n",
    )
    assert m.do_apply("hive-mind-gateway.service") == m.EXIT_STOP
    captured = capsys.readouterr()
    assert f"{unit_env} is not UTF-8" in captured.out
    assert marker not in captured.out
    assert marker not in captured.err
    assert "Traceback" not in captured.err
    assert "Nothing was written" not in captured.out


def test_non_utf8_env_file_is_refused_without_its_content(monkeypatch, tmp_path, capsys):
    """A .env that is not UTF-8 refuses with the path and none of the file."""
    env_file = tmp_path / "gateway.env"
    marker = "MARKER_NOTUTF8"
    env_file.write_bytes(f"{marker}=\xff\n".encode("latin-1"))
    _force_no_unit(monkeypatch)
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_file))
    assert m.do_apply("hive-mind-gateway.service") == m.EXIT_STOP
    captured = capsys.readouterr()
    assert f"{env_file} is not UTF-8" in captured.out
    assert marker not in captured.out
    assert marker not in captured.err
    assert "Traceback" not in captured.err
    assert "Nothing was written" not in captured.out


def test_apply_bad_json_in_an_environment_file_stops(monkeypatch, tmp_path, capsys):
    """EnvironmentFile= values are what systemd will set. A bad declaration there stops --apply."""
    unit_env = tmp_path / "unit.env"
    unit_env.write_text(f"LLM_BACKENDS_JSON={_BAD_JSON}\n")
    good = tmp_path / "good.env"
    good.write_text(f"LLM_BACKENDS_JSON={_GOOD_JSON}\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(good))
    monkeypatch.setenv("LLM_BACKENDS_JSON", _GOOD_JSON)

    class _Proc:
        returncode = 0
        stdout = f"Environment=\nEnvironmentFiles={unit_env} (ignore_errors=no)\n"
        stderr = ""

    monkeypatch.setattr(m.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _Proc())
    assert m.do_apply("hive-mind-gateway.service") == m.EXIT_STOP
    out = capsys.readouterr().out
    assert "field n_ctx" in out
    assert "Nothing was written" not in out


def test_apply_good_json_in_an_environment_file_ignores_a_legacy_line_in_dotenv(monkeypatch, tmp_path, capsys):
    """A usable EnvironmentFile= declaration wins over a leftover LLM_BACKENDS line in the env file."""
    unit_env = tmp_path / "unit.env"
    unit_env.write_text(f"LLM_BACKENDS_JSON={_GOOD_JSON}\n")
    legacy = tmp_path / "legacy.env"
    legacy.write_text("LLM_BACKENDS=http://legacy.example:9\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(legacy))
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.delenv("LLM_BACKENDS", raising=False)

    class _Proc:
        returncode = 0
        stdout = f"Environment=\nEnvironmentFiles={unit_env} (ignore_errors=no)\n"
        stderr = ""

    monkeypatch.setattr(m.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _Proc())
    assert m.do_apply("hive-mind-gateway.service") == m.EXIT_OK
    out = capsys.readouterr().out
    assert "Nothing was written" in out
    assert "legacy field" not in out
    assert "http://legacy.example" not in out




# ─────────────────────────────────────────────────────────────────────────
# plan_case_json_usable — the eligibility predicate + loader-shape tolerance
# ─────────────────────────────────────────────────────────────────────────















# ─────────────────────────────────────────────────────────────────────────
# probe_backend — the wall-clock deadline itself (QA H3 / SEC M-5 follow-up,
# NEW-1 micro-round: the fix landed with no test proving the deadline is
# what actually bounds a blocked probe).
# ─────────────────────────────────────────────────────────────────────────



# ─────────────────────────────────────────────────────────────────────────
# Interactive confirm — y / N / EOF / deadline, probe stubbed both ways
# ─────────────────────────────────────────────────────────────────────────





















# ─────────────────────────────────────────────────────────────────────────
# Two-layer comparison — the property test, and its mutation-check corpus
# ─────────────────────────────────────────────────────────────────────────

def _full_image(**over):
    base = {
        "urls": ["http://a:5000"],
        "weights": {"http://a:5000": 1.0},
        "has_token": {"http://a:5000": False},
        "models": {"http://a:5000": None},
        "roles": {"http://a:5000": None},
        "n_ctx": {"http://a:5000": None},
        "max_inflight": {"http://a:5000": None},
        "extra_body": {"http://a:5000": None},
        "private_ok": {"http://a:5000": True},
        "private_ok_explicit": {"http://a:5000": False},
        "fallback_reason": None,
        "config_empty": False,
        "guard_routing": {"raised": False, "message": None},
        "guard_auth": {"raised": False, "message": None},
    }
    base.update(over)
    return base




















# ── private_ok direction rules (ruling A(a)/§6.4) — QA HIGH-2/MED-2/MED-3 ──
# `private_ok` left the Layer-1 equality set for this Layer-2
# planned-direction comparison; these three tests are its ONLY remaining
# coverage (the parametrized drift test above no longer includes it).







# ── loader_semantics type guard (SEC M-4) ───────────────────────────────────







# ── provenance comment on a materialising write (QA MED-4) ─────────────────





# ─────────────────────────────────────────────────────────────────────────
# No-secret / scrub discipline
# ─────────────────────────────────────────────────────────────────────────

def test_capture_json_never_contains_a_token_env_value_only_has_token_bool(tmp_path, monkeypatch, capsys):
    """The capture is a fixed stub. A token value, and the token name, stay out of it."""
    _force_no_unit(monkeypatch)
    secret = "s3cr3t-must-never-appear-in-the-capture-file"
    env_text = (
        'LLM_BACKENDS_JSON=[{"url":"https://api.example.com/v1","token_env":"X_KEY", "n_ctx": 8192, "max_output_tokens": 2048}]\n'
        f"X_KEY={secret}\n"
    )
    env_path = _write_env(tmp_path, env_text)
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("X_KEY", secret)
    out_path = tmp_path / "pre.json"
    rc = m.do_capture(str(out_path), "hive-mind-gateway-does-not-exist.service")
    assert rc == m.EXIT_OK
    raw = out_path.read_text()
    assert secret not in raw, "the token_env-resolved secret leaked into the capture JSON"
    assert secret not in capsys.readouterr().out
    assert "X_KEY" not in raw
    payload = json.loads(raw)
    assert payload["image"]["urls"] == []
    assert "has_token" not in payload["image"]
    import stat as _stat
    assert _stat.S_IMODE(out_path.stat().st_mode) == 0o600


def test_capture_json_omits_a_query_credential_url(tmp_path, monkeypatch, capsys):
    """Capture writes no URL. A query credential in the declaration stays out of the file and stdout."""
    _force_no_unit(monkeypatch)
    env_text = 'LLM_BACKENDS_JSON=[{"url":"http://a:5000?token=form-only-fixture-credential", "n_ctx": 8192, "max_output_tokens": 2048}]\n'
    env_path = _write_env(tmp_path, env_text)
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    out_path = tmp_path / "pre.json"
    rc = m.do_capture(str(out_path), "hive-mind-gateway-does-not-exist.service")
    assert rc == m.EXIT_OK
    raw = out_path.read_text()
    captured = capsys.readouterr()
    assert "form-only-fixture-credential" not in raw
    assert "form-only-fixture-credential" not in captured.out
    assert "form-only-fixture-credential" not in captured.err
    payload = json.loads(raw)
    assert payload["image"]["urls"] == []
    assert "read-only capture" in payload["image"]["note"]










# ─────────────────────────────────────────────────────────────────────────
# End-to-end: do_capture / do_apply_or_dryrun against a real fixture .env,
# real loader subprocess (this suite's own deps cover it).
# ─────────────────────────────────────────────────────────────────────────

_NO_UNIT = "hive-mind-gateway-does-not-exist-in-this-test.service"




















































# ─────────────────────────────────────────────────────────────────────────
# Capture-schema check
# ─────────────────────────────────────────────────────────────────────────





def _full_image_from_capture(tmp_path, monkeypatch):
    """Helper: a real, complete image via the actual capture path, reused
    by the schema test above so its fixture stays honest (a real loader
    output) rather than a hand-typed partial dict."""
    out = tmp_path / "helper-pre.json"
    m.do_capture(str(out), _NO_UNIT)
    return json.loads(out.read_text())["image"]




# ─────────────────────────────────────────────────────────────────────────
# Backup + restore mechanics
# ─────────────────────────────────────────────────────────────────────────










_H9_ENDPOINTS = "EMBEDDER_URL=http://a:8070\nRERANKER_URL=http://a:8071\n"




