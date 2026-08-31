"""check_config.py (W1, D2/D3) — standalone script that renders the
framework's effective configuration and what the running gateway will DO
with it, without ever starting the gateway.

Unit-level tests call phase_a_render()/phase_b_render()/main() in-process
(monkeypatched env, SECURE_ENV_FILE pinned per conftest.py's hermeticity
rule). The PRIMARY proof that Phase A is genuinely stdlib-only is a real
subprocess run under `python3 -S` (site-packages stripped) — a static
import parse can miss a transitive third-party import inside secure_env.py
or log_hygiene.py, so this file exercises the actual interpreter rather
than only asserting import statements.
"""
import importlib
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import check_config  # noqa: E402
import framework_defaults  # noqa: E402

SCRIPT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "shared-memory", "scripts", "check_config.py"
)


def _run(args=(), env_overrides=None, tmp_path=None, python_flags=(), timeout=30):
    env = dict(os.environ)
    env.setdefault("SECURE_ENV_FILE", "")
    if tmp_path is not None:
        env["CREDENTIAL_AUDIT_LOG_PATH"] = str(tmp_path / "credential-audit.jsonl")
        env["CAPACITY_LOG_PATH"] = str(tmp_path / "capacity-derivations.jsonl")
    if env_overrides:
        env.update(env_overrides)
    cmd = [sys.executable, *python_flags, SCRIPT_PATH, *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


# ── PRIMARY: Phase A is genuinely stdlib-only (real subprocess, -S) ─────────

def test_phase_a_only_runs_under_a_stdlib_only_interpreter():
    """`-S` skips the site module entirely, so no site-packages directory
    (venv or system) is ever added to sys.path — any third-party import
    anywhere on Phase A's path would surface here as ModuleNotFoundError.
    This is the PRIMARY proof (a real subprocess), not the secondary
    static-import check below."""
    proc = _run(["--phase-a-only"], env_overrides={"SECURE_ENV_FILE": ""}, python_flags=["-S"])
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "Traceback" not in proc.stderr
    assert "environment-only" in proc.stdout


def test_phase_a_module_imports_are_a_stdlib_only_subset():
    """SECONDARY check: a static import scan of check_config.py's own
    top-level imports. Does not by itself prove secure_env.py/log_hygiene.py
    stay stdlib-only underneath — that is what the subprocess test above
    proves; this only guards check_config.py's own import list from
    regressing."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(check_config))
    names = set()
    for node in tree.body:  # MODULE-LEVEL only — a function-body import
                             # (hive_mind_proxy, inside phase_b_render) must
                             # not be caught here; ast.walk() would also
                             # descend into every function and misreport it
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    # framework_defaults / secure_env / log_hygiene are this repo's own
    # stdlib-only modules (see their own docstrings / tests); hive_mind_proxy
    # is imported ONLY inside phase_b_render(), a function body, never here
    # at module top-level scan scope.
    THIRD_PARTY_KNOWN = {"aiohttp", "asyncpg", "httpx", "neo4j", "fastmcp"}
    assert not (names & THIRD_PARTY_KNOWN), f"unexpected third-party import: {names & THIRD_PARTY_KNOWN}"
    assert "hive_mind_proxy" not in names, "hive_mind_proxy must be imported inside phase_b_render(), not at module scope"


# ── Absent vs unreadable-but-present .env ───────────────────────────────────

def test_absent_env_file_is_environment_only_and_exit_0(tmp_path):
    proc = _run(["--phase-a-only"], env_overrides={"SECURE_ENV_FILE": ""}, tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "none found — environment-only" in proc.stdout
    assert "ERROR" not in proc.stdout


def test_unreadable_but_present_env_file_is_exit_2(tmp_path):
    """A directory in place of the .env file is unreadable regardless of
    process privilege (unlike a chmod-000 file, which root can still read),
    so this reproduces reliably in any CI environment."""
    bogus = tmp_path / "not-a-real-env-file"
    bogus.mkdir()
    proc = _run(["--phase-a-only"], env_overrides={"SECURE_ENV_FILE": str(bogus)}, tmp_path=tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "ERROR" in proc.stdout
    assert "could not be read" in proc.stdout
    assert "Traceback" not in proc.stderr


# ── declared / present-but-empty / inherited verdicts (in-process unit) ────

def test_declared_state_when_non_empty(monkeypatch):
    monkeypatch.setenv("SECURE_ENV_FILE", "")
    monkeypatch.setenv("EMBEDDER_URL", "http://custom.example:9999")
    lines, ok = check_config.phase_a_render()
    assert ok
    body = "\n".join(lines)
    assert "EMBEDDER_URL" in body
    v = check_config._verdict("EMBEDDER_URL")
    assert v["state"] == "declared"
    assert v["effective"] == "http://custom.example:9999"


def test_present_but_empty_state_for_an_or_idiom_site_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SECURE_ENV_FILE", "")
    monkeypatch.setenv("EMBEDDER_URL", "")
    check_config.phase_a_render()
    v = check_config._verdict("EMBEDDER_URL")
    assert v["state"] == "present-but-empty"
    assert v["effective"] == framework_defaults.FRAMEWORK_DEFAULTS["EMBEDDER_URL"]["default"]


def test_present_but_empty_state_for_a_get_idiom_site_stays_empty(monkeypatch):
    monkeypatch.setenv("SECURE_ENV_FILE", "")
    monkeypatch.setenv("LLM_DEFAULT_TARGET", "")
    check_config.phase_a_render()
    v = check_config._verdict("LLM_DEFAULT_TARGET")
    assert v["state"] == "present-but-empty"
    assert v["effective"] == ""  # NOT the default — get idiom, matches the known latent


def test_inherited_default_state_when_absent(monkeypatch):
    monkeypatch.delenv("RERANKER_URL", raising=False)
    monkeypatch.setenv("SECURE_ENV_FILE", "")
    check_config.phase_a_render()
    v = check_config._verdict("RERANKER_URL")
    assert v["state"] == "inherited default"
    assert v["effective"] == framework_defaults.FRAMEWORK_DEFAULTS["RERANKER_URL"]["default"]


# ── Secrets: boolean only, value NEVER rendered ─────────────────────────────

def test_credentialed_secret_shows_true_boolean_never_the_value(tmp_path):
    secret_value = "s3cr3t-should-never-appear-anywhere-in-output"
    proc = _run(["--phase-a-only"],
                env_overrides={"SECURE_ENV_FILE": "", "NEO4J_PASSWORD": secret_value},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stderr
    full_output = proc.stdout + proc.stderr
    assert "NEO4J_PASSWORD" in full_output
    assert "has_credential=True" in full_output
    assert secret_value not in full_output, "the secret value itself must never appear in output"


def test_uncredentialed_secret_shows_false(tmp_path):
    proc = _run(["--phase-a-only"], env_overrides={"SECURE_ENV_FILE": ""}, tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "NEO4J_PASSWORD" in proc.stdout
    assert "has_credential=False" in proc.stdout


# ── Phase B: role-error → exit 1, malformed config → exit 2 (no traceback) ─

def test_role_config_error_leads_to_exit_1_would_refuse_to_start(tmp_path):
    backends_json = json.dumps([{"url": "http://a:5000", "roles": ["bogus"]}])
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS_JSON": backends_json},
                tmp_path=tmp_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "WOULD REFUSE TO START" in proc.stdout
    assert "Traceback" not in proc.stderr


def test_malformed_llm_backends_json_array_of_strings_is_exit_2_no_traceback(tmp_path):
    """Valid JSON, valid array — but each ENTRY is a string, not an object,
    so hive_mind_proxy's own _load_llm_backends() raises AttributeError at
    import time ('str' object has no attribute 'get'). Phase B must catch
    this, never let it surface as a raw traceback."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS_JSON": '["http://a:5000"]'},
                tmp_path=tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "UNAVAILABLE" in proc.stdout
    assert "Traceback" not in proc.stderr
    assert "Phase A" in proc.stdout  # Phase A output still printed


def test_import_crash_bare_host_port_embedder_url_still_prints_phase_a_and_exit_2(tmp_path):
    """The measured-common typo: no scheme at all. urlsplit reads this as
    scheme='embedder', which coordinator._encoder_url raises ValueError on
    at IMPORT time (before hive_mind_proxy even finishes importing it)."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "EMBEDDER_URL": "embedder.internal:8070"},
                tmp_path=tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "== Phase A" in proc.stdout
    assert "EMBEDDER_URL" in proc.stdout  # declared state visible in Phase A output too
    assert "UNAVAILABLE" in proc.stdout
    assert "ValueError" in proc.stdout
    assert "Traceback" not in proc.stderr


def test_valid_config_with_a_local_backend_is_exit_0(tmp_path):
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS": "http://a:5000"},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "none — the gateway would boot with this configuration." in proc.stdout


# ── Mutation check: the Phase-B except Exception wrapper actually guards ───
# (evidence recorded separately in HANDOFF.md per the build brief — this
# test file's own behaviour under the mutation IS the check; run manually
# with the wrapper narrowed/removed and confirm this test flips to failing
# with a raw traceback, then restore.)

def test_phase_b_render_never_raises_on_a_broken_import(monkeypatch, tmp_path):
    """In-process guard-shape check: phase_b_render() must return a
    (lines, 2) tuple, never propagate, when the import itself is broken —
    simulated by pointing PYTHONPATH-independent sys.modules at a stub that
    raises on import. This is a narrower, faster in-process companion to
    the subprocess-based crash tests above."""
    import builtins

    real_import = builtins.__import__

    def _boom_import(name, *a, **kw):
        if name == "hive_mind_proxy":
            raise RuntimeError("synthetic import failure for this test")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _boom_import)
    lines, code = check_config.phase_b_render()
    assert code == 2
    assert any("UNAVAILABLE" in line for line in lines)
    assert any("synthetic import failure" in line for line in lines)
