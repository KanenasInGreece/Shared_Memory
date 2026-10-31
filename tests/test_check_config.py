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


def _run(args=(), env_overrides=None, tmp_path=None, python_flags=(), timeout=30, env_unset=()):
    env = dict(os.environ)
    env.setdefault("SECURE_ENV_FILE", "")
    env["GATEWAY_UNIT"] = "sm-test-nonexistent-gateway.service"
    if tmp_path is not None:
        env["CREDENTIAL_AUDIT_LOG_PATH"] = str(tmp_path / "credential-audit.jsonl")
        env["CAPACITY_LOG_PATH"] = str(tmp_path / "capacity-derivations.jsonl")
    for key in env_unset:
        env.pop(key, None)
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
    """A get-idiom row still keeps a present-but-empty value. LLM_DEFAULT_TARGET
    is no longer a row; EMBED_MAX_CONTEXT_TOKENS is."""
    monkeypatch.setenv("SECURE_ENV_FILE", "")
    monkeypatch.setenv("EMBED_MAX_CONTEXT_TOKENS", "")
    check_config.phase_a_render()
    v = check_config._verdict("EMBED_MAX_CONTEXT_TOKENS")
    assert v["state"] == "present-but-empty"
    assert v["effective"] == ""


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


def test_secret_census_notes_canonical_normalisation(monkeypatch):
    """QA finding 7: D.2 stores every discovered token_env name canonically
    (upper-cased), so a lowercase-declared spelling ("token_env":
    "openrouter_cred") renders as OPENROUTER_CRED — a name that appears
    nowhere in the operator's own .env/JSON. The census must say so rather
    than silently implying an exact-spelling match."""
    monkeypatch.setenv("SECURE_ENV_FILE", "")
    lines, ok = check_config.phase_a_render()
    assert ok
    body = "\n".join(lines)
    assert "CANONICAL" in body or "canonical" in body
    assert "normalised" in body or "normalized" in body


# ── Phase B: role-error → exit 1, malformed config → exit 2 (no traceback) ─

def test_role_config_error_leads_to_exit_1_would_refuse_to_start(tmp_path):
    """A leftover roles list is not a startup refusal. The entry loads and the gateway would boot."""
    backends_json = json.dumps([{"url": "http://a:5000", "roles": ["bogus"], "n_ctx": 8192, "max_output_tokens": 2048}])
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS_JSON": backends_json},
                tmp_path=tmp_path)
    full = proc.stdout + proc.stderr
    assert proc.returncode == 0, full
    assert "http://a:5000" in proc.stdout
    assert "WOULD REFUSE TO START" not in full
    assert "would boot" in proc.stdout.lower()
    assert "Traceback" not in proc.stderr


def test_malformed_llm_backends_json_array_of_strings_is_excluded_not_an_import_crash(tmp_path):
    """A string entry is a declaration FATAL (field entry), exit 1.
    The string value can be a credentialed URL and must not be printed."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS_JSON": '["http://a:5000"]'},
                tmp_path=tmp_path)
    full = proc.stdout + proc.stderr
    assert proc.returncode == 1, full
    assert "Phase A" in proc.stdout
    assert "field entry" in proc.stdout
    assert "WOULD REFUSE TO START" in proc.stdout
    assert "AttributeError" not in full
    assert "http://a:5000" not in full
    assert "Traceback" not in proc.stderr


def test_import_crash_bare_host_port_embedder_url_still_prints_phase_a_and_exit_2(tmp_path):
    """The measured-common typo: no scheme at all. urlsplit reads this as
    scheme='embedder', which coordinator._encoder_url raises ValueError on
    at IMPORT time (before hive_mind_proxy even finishes importing it).
    SEC-HIGH (fold round, PR #347): ValueError is NOT on the safe-message
    allowlist — only the type name is shown, never _encoder_url's own
    message (which can embed the operator-supplied EMBEDDER_URL value)."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "EMBEDDER_URL": "embedder.internal:8070"},
                tmp_path=tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "== Phase A" in proc.stdout
    assert "EMBEDDER_URL" in proc.stdout  # declared state visible in Phase A output too
    assert "UNAVAILABLE" in proc.stdout
    assert "ValueError" in proc.stdout
    assert "must be an http(s) URL" not in proc.stdout  # _encoder_url's own message, never shown
    assert "Traceback" not in proc.stderr


# ── H3 (HYG round S2) — the Phase-B import-failure hint names a bad
#    EMBEDDER_URL/RERANKER_URL as a cause, scoped to the "UNAVAILABLE --
#    import failed:" line ONLY. Phase A's own row already prints
#    "EMBEDDER_URL" unconditionally (see the crash test above), so a naive
#    whole-stdout assertion would pass without the hint wording ever
#    changing -- this isolates the one line the fix actually touches. ──────

def test_phase_b_import_failure_hint_names_the_bad_encoder_url_cause(tmp_path):
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "EMBEDDER_URL": "embedder.internal:8070"},
                tmp_path=tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    unavailable_lines = [line for line in proc.stdout.splitlines()
                          if "UNAVAILABLE — import failed:" in line]
    assert unavailable_lines, proc.stdout
    line = unavailable_lines[0]
    assert "EMBEDDER_URL" in line
    assert "RERANKER_URL" in line
    # The two absence assertions other tests pin (near :208 and :227 in this
    # file) must stay green -- checked here too, scoped to this same line.
    assert "has no attribute" not in line
    assert "must be an http(s) URL" not in line


def test_valid_config_with_a_local_backend_is_exit_0(tmp_path):
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS": "http://a:5000"},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "none — the gateway would boot with this configuration." in proc.stdout


# ── QA Q2 (fold round, PR #347, the substantive finding) — a parse-error
#    LLM_BACKENDS_JSON is caught INSIDE hive_mind_proxy's own loader and
#    silently replaced by the legacy fallback: import succeeds, both guard
#    functions pass, and without the warning line below the report would
#    read as a clean, intended single-backend roster over a vanished
#    declared fleet. ───────────────────────────────────────────────────────

def test_llm_pool_fallback_reason_is_rendered_prominently_and_exit_stays_0(tmp_path):
    """Unusable JSON is a declaration FATAL. There is no legacy fallback."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS_JSON": "{not json"},
                tmp_path=tmp_path)
    full = proc.stdout + proc.stderr
    assert proc.returncode == 1, full
    assert "field json" in proc.stdout
    assert "WOULD REFUSE TO START" in proc.stdout
    assert "DECLARED FLEET NOT USABLE" not in full
    assert "legacy fallback" not in full.lower()
    assert "{not json" not in full


def test_a_clean_llm_backends_json_shows_no_fallback_warning(tmp_path):
    """Negative case for the above: a genuinely usable declared fleet gets
    no fallback warning at all."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS_JSON": '[{"url":"http://a:5000", "n_ctx": 8192, "max_output_tokens": 2048}]'},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "DECLARED FLEET NOT USABLE" not in proc.stdout


# ── W3 (Backend_Declaration_Spec_2026-08-30 §4 / decision:1846) —
#    LLM_POOL_CONFIG_EMPTY gets its own flagged Phase-B line, mutually
#    exclusive with LLM_POOL_FALLBACK_REASON's line (D1). ───────────────────

def test_config_empty_is_rendered_as_its_own_flagged_line_and_exit_stays_0(tmp_path):
    """Nothing declared at all (no LLM_BACKENDS_JSON, no LLM_BACKENDS) ->
    LLM_POOL_CONFIG_EMPTY is True -> its own NO BACKEND DECLARED line,
    still exit 0 (the gateway boots on the bare LLM_DEFAULT_TARGET
    fallback)."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": ""}, tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "NO BACKEND DECLARED" in proc.stdout
    assert "DECLARED FLEET NOT USABLE" not in proc.stdout


def test_config_empty_line_absent_when_a_backend_is_declared(tmp_path):
    """A CSV LLM_BACKENDS line is not a declaration. The roster stays empty."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS": "http://a:5000"},
                tmp_path=tmp_path)
    full = proc.stdout + proc.stderr
    assert proc.returncode == 0, full
    assert "NO BACKEND DECLARED" in proc.stdout
    assert "DECLARED FLEET NOT USABLE" not in full
    assert "http://a:5000" not in full


def test_config_empty_and_fallback_reason_lines_are_mutually_exclusive(tmp_path):
    """Unusable JSON empties the pool and refuses startup. No fallback line."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS_JSON": "{not json"},
                tmp_path=tmp_path)
    full = proc.stdout + proc.stderr
    assert proc.returncode == 1, full
    assert "NO BACKEND DECLARED" in proc.stdout
    assert "WOULD REFUSE TO START" in proc.stdout
    assert "field json" in proc.stdout
    assert "DECLARED FLEET NOT USABLE" not in full
    assert "{not json" not in full


# ── §6.2 (M11 + case-0 census, W4/decision:1824) — present-but-empty /
#    composed latents, report-only, rendered in Phase B as one scannable
#    block. ────────────────────────────────────────────────────────────────

def test_w4_census_zero_latents_on_a_clean_declared_fleet(tmp_path):
    proc = _run(env_overrides={"SECURE_ENV_FILE": "",
                                "LLM_BACKENDS_JSON": '[{"url":"http://a:5000","private_ok":true, "n_ctx": 8192, "max_output_tokens": 2048}]'},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "W4 census" in proc.stdout
    assert "0 latent case(s) present on this install." in proc.stdout


def test_w4_census_counts_present_but_empty_encoder_key(tmp_path):
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS": "http://a:5000",
                                "EMBEDDER_URL": ""},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "EMBEDDER_URL is present but EMPTY" in proc.stdout
    assert "1 latent case(s) present on this install." in proc.stdout


def test_w4_census_counts_present_but_empty_llm_backends_json(tmp_path):
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS_JSON": ""},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "LLM_BACKENDS_JSON is present but EMPTY" in proc.stdout


def test_w4_census_counts_the_llm_default_target_composed_empty_latent(tmp_path):
    """An empty LLM_DEFAULT_TARGET is ignored. It does not compose a backend."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_DEFAULT_TARGET": ""},
                tmp_path=tmp_path)
    full = proc.stdout + proc.stderr
    assert proc.returncode == 0, full
    assert "NO BACKEND DECLARED" in proc.stdout
    assert "LLM_BACKENDS composed to ['']" not in full


# ── §6.3 (R-B announce, W4/decision:1824) — a roles-carrying entry with no
#    explicit private_ok no longer serves role-less traffic. ───────────────

def test_r_b_announce_on_a_roles_only_entry_with_no_explicit_private_ok(tmp_path):
    """A roles list does not keep the backend out of the roster."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": "",
                                "LLM_BACKENDS_JSON": '[{"url":"http://a:5000","roles":["extract"], "n_ctx": 8192, "max_output_tokens": 2048}]'},
                tmp_path=tmp_path)
    full = proc.stdout + proc.stderr
    assert proc.returncode == 0, full
    assert "http://a:5000" in proc.stdout
    assert "would boot" in proc.stdout
    assert "R-B (W4)" not in full


def test_r_b_announce_absent_when_private_ok_is_explicit(tmp_path):
    proc = _run(env_overrides={"SECURE_ENV_FILE": "",
                                "LLM_BACKENDS_JSON":
                                    '[{"url":"http://a:5000","roles":["extract"],"private_ok":true, "n_ctx": 8192, "max_output_tokens": 2048}]'},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "R-B (W4)" not in proc.stdout


# ── QA HIGH-1 (fix round) — check_config renders M-5'/P-5' per-entry, the
#    SECOND of the two nominated instruments (the startup log is the
#    first). The closing "Gateway startup refusals ... none" line must
#    also stop reading as an unqualified all-clear once either fires. ──────

def test_m5_announce_on_a_credentialed_backend_with_neither_roles_nor_private_ok(tmp_path):
    """A credentialed entry with no roles is in the roster. The key stays out of the report."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "AGENT_TOKENS": "claude:tok_abc",
                                "DEEPSEEK_API_KEY": "sk-test",
                                "LLM_BACKENDS_JSON":
                                    '[{"url":"https://api.deepseek.com/v1","token_env":"DEEPSEEK_API_KEY", "n_ctx": 8192, "max_output_tokens": 2048}]'},
                tmp_path=tmp_path)
    full = proc.stdout + proc.stderr
    assert proc.returncode == 0, full
    assert "https://api.deepseek.com/v1" in proc.stdout
    assert "has_credential=True" in proc.stdout
    assert "sk-test" not in full
    assert "M-5' (W4)" not in full
    assert "NEVER be selected" not in full
    assert "none — the gateway would boot with this configuration." in proc.stdout


def test_m5_announce_absent_when_roles_are_declared(tmp_path):
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "AGENT_TOKENS": "claude:tok_abc",
                                "DEEPSEEK_API_KEY": "sk-test",
                                "LLM_BACKENDS_JSON":
                                    '[{"url":"https://api.deepseek.com/v1","token_env":"DEEPSEEK_API_KEY",'
                                    '"roles":["extract"], "n_ctx": 8192, "max_output_tokens": 2048}]'},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "M-5' (W4)" not in proc.stdout
    assert "Degraded, not clean" not in proc.stdout


def test_p5_announce_roles_absent_says_safe_by_construction(tmp_path):
    """private_ok false does not exclude the backend and does not degrade the roster."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "AGENT_TOKENS": "",
                                "LLM_BACKENDS_JSON":
                                    '[{"url":"http://a:5000","private_ok":false, "n_ctx": 8192, "max_output_tokens": 2048}]'},
                tmp_path=tmp_path)
    full = proc.stdout + proc.stderr
    assert proc.returncode == 0, full
    assert "http://a:5000" in proc.stdout
    assert "would boot" in proc.stdout
    assert "P-5' (W4)" not in full
    assert "Degraded, not clean" not in proc.stdout


def test_p5_announce_roles_carrying_says_not_safe_by_construction(tmp_path):
    """private_ok false plus a roles list still loads. Neither key is a refusal."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "AGENT_TOKENS": "",
                                "LLM_BACKENDS_JSON":
                                    '[{"url":"http://a:5000","private_ok":false,"roles":["extract"], "n_ctx": 8192, "max_output_tokens": 2048}]'},
                tmp_path=tmp_path)
    full = proc.stdout + proc.stderr
    assert proc.returncode == 0, full
    assert "http://a:5000" in proc.stdout
    assert "would boot" in proc.stdout
    assert "P-5' (W4)" not in full
    assert "WOULD REFUSE TO START" not in full


def test_p5_announce_absent_on_an_undeclared_backend(tmp_path):
    """The narrowed predicate (`private_ok_explicit AND not private_ok`)
    must NOT fire on the pervasive, unremarkable default -- an undeclared
    backend never states private_ok at all."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "AGENT_TOKENS": "",
                                "LLM_BACKENDS_JSON": '[{"url":"http://a:5000","roles":["extract"], "n_ctx": 8192, "max_output_tokens": 2048}]'},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "P-5' (W4)" not in proc.stdout


def test_no_degraded_warning_qualifier_on_a_fully_explicit_clean_fleet(tmp_path):
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "AGENT_TOKENS": "claude:tok_abc",
                                "LLM_BACKENDS_JSON": '[{"url":"http://a:5000","private_ok":true, "n_ctx": 8192, "max_output_tokens": 2048}]'},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "none — the gateway would boot with this configuration." in proc.stdout
    assert "Degraded, not clean" not in proc.stdout
    assert "M-5' (W4)" not in proc.stdout
    assert "P-5' (W4)" not in proc.stdout


# ── W5 E-reduced (declared half only) — check_config renders the RECOGNISED
#    SHAPE of a backend's declared extra_body thinking-suppression, never
#    the raw dict (extra_body is operator-supplied and could contain
#    anything, including a key). N2: mutation-checked below. ───────────────

def test_extra_body_recognised_deepseek_shape_is_rendered(tmp_path):
    """The report lists the backend and does not print the extra_body dict."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": "",
                                "LLM_BACKENDS_JSON":
                                    '[{"url":"http://a:5000","n_ctx":8192,"max_output_tokens":2048,'
                                    '"extra_body":{"thinking":{"type":"disabled"}}}]'},
                tmp_path=tmp_path)
    full = proc.stdout + proc.stderr
    assert proc.returncode == 0, full
    assert "http://a:5000" in proc.stdout
    assert '"type": "disabled"' not in full
    assert '"type":"disabled"' not in full
    assert "disabled" not in full


def test_extra_body_absent_renders_none_declared(tmp_path):
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS_JSON": '[{"url":"http://a:5000", "n_ctx": 8192, "max_output_tokens": 2048}]'},
                tmp_path=tmp_path)
    full = proc.stdout + proc.stderr
    assert proc.returncode == 0, full
    assert "http://a:5000" in proc.stdout
    assert "extra_body" not in full


def test_extra_body_unrecognised_shape_renders_present_unrecognised_never_raw(tmp_path):
    # extra_body is operator-supplied and could carry a key. The report must not print it.
    proc = _run(env_overrides={"SECURE_ENV_FILE": "",
                                "LLM_BACKENDS_JSON":
                                    '[{"url":"http://a:5000","n_ctx":8192,"max_output_tokens":2048,'
                                    '"extra_body":{"some_future_key":"sk-should-never-appear"}}]'},
                tmp_path=tmp_path)
    full = proc.stdout + proc.stderr
    assert proc.returncode == 0, full
    assert "http://a:5000" in proc.stdout
    assert "sk-should-never-appear" not in full
    assert "some_future_key" not in full


def test_token_env_key_is_absent_from_check_config_output(tmp_path):
    """A key parked in token_env is not a variable name and must not be printed."""
    secret = "sk-fixture-not-a-name-9917"
    proc = _run(env_overrides={
        "SECURE_ENV_FILE": "",
        "LLM_BACKENDS_JSON": json.dumps([{
            "url": "http://a:5000",
            "n_ctx": 8192,
            "max_output_tokens": 2048,
            "token_env": secret,
        }]),
    }, tmp_path=tmp_path)
    full = proc.stdout + proc.stderr
    assert secret not in full
    assert secret.upper() not in full
    assert "<not a variable name>" in full


def test_phase_a_flags_openai_base_url_key_without_printing_the_url(tmp_path):
    """First-install trap: OpenAI SDK's base_url instead of url. Phase A
    (stdlib, no gateway import) must name the wrong key and not echo the
    value — a mistaken key can still carry a credentialed URL."""
    proc = _run(
        ["--phase-a-only"],
        env_overrides={
            "SECURE_ENV_FILE": "",
            "LLM_BACKENDS_JSON": json.dumps([
                {"base_url": "https://api.example.invalid/v1",
                 "token_env": "DEEPSEEK_API_KEY", "private_ok": True},
            ]),
        },
        tmp_path=tmp_path,
        python_flags=["-S"],
    )
    full = proc.stdout + proc.stderr
    assert proc.returncode == 1, full
    assert "field url" in proc.stdout
    assert "exit 78" in proc.stdout
    assert "api.example.invalid" not in full


def test_check_config_module_contract_no_longer_says_extra_body_is_a_later_wave():
    # M10: the module's own docstring claimed extra_body display "belongs to
    # a later wave" -- W5 IS that wave, and the stale claim must be gone.
    src = open(os.path.join(os.path.dirname(__file__), "..", "shared-memory",
                             "scripts", "check_config.py")).read()
    assert "belongs to a later wave" not in src


# ── SEC-HIGH (fold round, PR #347) — check_config must be unable to print a
#    raw secret through ANY exception path, in EITHER phase. Policy: ALWAYS
#    the exception's type name; str(exc) shown (scrubbed) ONLY for the
#    small known-safe allowlist (ImportError/ModuleNotFoundError — pure
#    dependency messages, no config payload); every other type gets
#    type-name-only plus a fixed hint, never its own message. ──────────────

def test_render_exception_allowlisted_type_shows_its_scrubbed_message():
    out = check_config._render_exception(ImportError("No module named 'x'"), "a hint")
    assert out == "ImportError: No module named 'x'"


def test_render_exception_non_allowlisted_type_shows_type_and_hint_only():
    out = check_config._render_exception(ValueError("secret-abc-should-not-appear"), "a fixed hint")
    assert out == "ValueError — a fixed hint"
    assert "secret-abc-should-not-appear" not in out


def test_phase_a_secret_bearing_exception_shows_type_only_never_the_secret(monkeypatch):
    """secure_env.load_split_env() can raise a ValueError quoting the
    OFFENDING .env LINE verbatim — i.e. a raw secret, if that line set
    one. Simulated (the real secure_env parsing code doesn't raise this
    way today) to prove check_config's OWN defence holds even if it ever
    does — this is the guard MUTATION-CHECKED in the fold round: remove
    _render_exception's allowlist gate (render str(exc) unconditionally)
    and this test is exactly the one that starts failing."""
    monkeypatch.setenv("SECURE_ENV_FILE", "")
    secret = "s3cr3t-that-must-never-appear-in-phase-a-output"

    def _boom():
        raise ValueError(f"malformed .env line: NEO4J_PASSWORD={secret}")

    monkeypatch.setattr(check_config.secure_env, "load_split_env", _boom)
    lines, ok = check_config.phase_a_render()
    assert ok is False
    body = "\n".join(lines)
    assert "ValueError" in body
    assert secret not in body
    assert "Traceback" not in body


def test_phase_b_secret_bearing_import_exception_shows_type_only_never_the_secret(monkeypatch):
    """Same shape as the Phase A test above, for the import-failure branch
    — also mutation-checked (see that test's docstring); together the two
    prove BOTH phases' exception paths honour the allowlist gate."""
    import builtins

    real_import = builtins.__import__
    secret = "s3cr3t-that-must-never-appear-in-phase-b-output"

    def _boom_import(name, *a, **kw):
        if name == "hive_mind_proxy":
            raise ValueError(f"LLM_BACKENDS_JSON token_env resolved to {secret}")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _boom_import)
    lines, code = check_config.phase_b_render()
    assert code == 2
    body = "\n".join(lines)
    assert "ValueError" in body
    assert secret not in body
    assert "Traceback" not in body


# ── SEC-MED (fold round, PR #347) — defensive scrub_url_credentials wrap on
#    the WOULD-REFUSE line and each role_config_errors element. Both were
#    already construction-scrubbed since v0.9.77 (scrub_url_credentials is
#    called at the point _load_llm_backends()/require_valid_llm_routing_
#    config() BUILD these strings) — this wrap is belt-and-braces for a
#    FUTURE construction-site regression, not a fix for a leak found today.
#
#    ⚠ FINDING (not a ruling — flagging for the merger): the wrap can
#    OVER-redact. Every role_config_errors message is built as
#    f"{scrub_url_credentials(url)}: ..." with NO space before the colon
#    (hive_mind_proxy.py's _parse_roles, all four call sites) — so on the
#    SECOND scrub, scrub_url_credentials' greedy `\S+` regex captures the
#    trailing colon as part of the "URL", urlsplit() chokes turning
#    "5000:" into a port, and the whole thing falls back to the function's
#    own "<url-redacted>" catch-all. Net effect: an already-clean backend
#    URL displays as "<url-redacted>" in this one spot. This is a SAFE
#    failure (over-redaction, never a leak) but a display regression —
#    fixable with a one-character space added at each of the four
#    hive_mind_proxy.py call sites, deliberately NOT done here since it is
#    outside this fold round's named scope. ──────────────────────────────

def test_would_refuse_line_and_role_errors_never_leak_a_raw_credential(tmp_path):
    """The property that actually matters: no unredacted userinfo/query
    credential reaches output, even accepting the over-redaction above.

    SEC A (R-1, 2026-09-02): this test's original fixture had the credential
    in USERINFO ("http://svc:cred@a:5000") — that shape is now refused/
    excluded at _load_llm_backends() parse time, before roles are even
    parsed, so it can no longer reach the role_config_errors path this test
    targets (userinfo-URL coverage lives in
    tests/test_llm_backend_secrets.py's SEC-A tests instead). A query-string
    credential (R-2, NOT refused by A) is the fixture that still reaches
    _parse_roles unmodified, keeping this test's actual point intact."""
    backends_json = ('[{"url":"http://a:5000?key=s3cr3t-in-url","roles":["bogus"], "n_ctx": 8192, "max_output_tokens": 2048}]')
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS_JSON": backends_json},
                tmp_path=tmp_path)
    full = proc.stdout + proc.stderr
    assert proc.returncode == 0, full
    assert "http://a:5000" in proc.stdout
    assert "WOULD REFUSE TO START" not in full
    assert "s3cr3t-in-url" not in full
    assert "?key=" not in full


def test_userinfo_credential_username_never_reaches_check_config_output(tmp_path):
    """Finding 10 (QA LOW): the retargeted query-string fixture above only
    restored the PASSWORD-never-leaks half of the original test's property.
    The original also asserted the userinfo USERNAME never reaches output
    ("svc:") -- dropped when the fixture moved to a query-string credential,
    since A now refuses userinfo upstream and no _load_llm_backends() output
    path can carry it any more. Pinned here directly, so the coverage is a
    deliberate decision (check_config's own render surface never leaks
    either half of a userinfo credential, even for an entry A excludes
    before check_config ever sees a role/refusal path for it), not an
    accidental drop."""
    backends_json = '[{"url":"http://svc:s3cr3t-userinfo-pw@a:5000", "n_ctx": 8192, "max_output_tokens": 2048}]'
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS_JSON": backends_json},
                tmp_path=tmp_path)
    full_output = proc.stdout + proc.stderr
    assert "svc:" not in full_output
    assert "s3cr3t-userinfo-pw" not in full_output
    assert "Traceback" not in full_output


# ── H1 (HYG round S2, ruling R-H: TEST ONLY, no code change) — every key
#    check_config.py's own ENV_ROW_ORDER names must carry both 'idiom' and
#    'default' in FRAMEWORK_DEFAULTS. _idiom_for()'s own docstring says a
#    KeyError on a row missing either is a genuine bug in ENV_ROW_ORDER, not
#    a condition to paper over -- this test pins that every row today
#    actually satisfies the precondition the render loop assumes, without
#    moving that loop inside the try (the module docstring says loud is
#    intended). ─────────────────────────────────────────────────────────────

def test_every_env_row_order_key_carries_an_idiom_and_a_default():
    for key in check_config.ENV_ROW_ORDER:
        row = framework_defaults.FRAMEWORK_DEFAULTS[key]
        assert "idiom" in row, f"{key} row is missing 'idiom'"
        assert "default" in row, f"{key} row is missing 'default'"


# ── QA Q1 / fold-round item 4 — PROXY_BIND's idiom now lives in the D1
#    table, not a second, hand-written authority in this script. ───────────

def test_proxy_bind_idiom_comes_from_the_framework_defaults_table():
    """SEC H (R-3, RULED 2026-09-02): idiom flipped "get" -> "or" — see
    framework_defaults.py's PROXY_BIND row note."""
    assert check_config._idiom_for("PROXY_BIND") == framework_defaults.FRAMEWORK_DEFAULTS["PROXY_BIND"]["idiom"]
    assert framework_defaults.FRAMEWORK_DEFAULTS["PROXY_BIND"]["idiom"] == "or"
    assert not hasattr(check_config, "_PROXY_BIND_IDIOM"), (
        "the fold round deleted this hand-written special case — it must not come back")


# ── QA Q3 (fold round, LOW) — the three proxy-module symbols this script
#    reads via guarded getattr() must degrade the report, never crash it,
#    on a future rename. Same defensive shape as hive_mind_proxy.py's own
#    _chmod_created_ancestors import guard (hive_mind_proxy.py:39-56). ─────

class _FakeProxyMissingGuardFunctions:
    """A stand-in 'hive_mind_proxy' module missing BOTH guard functions —
    everything else present, isolating that guard."""
    LLM_BACKENDS = []
    LLM_WEIGHTS = {}
    LLM_BACKEND_MODELS = {}
    LLM_BACKEND_ROLES = {}
    LLM_BACKEND_EXTRAS = {}
    LLM_BACKEND_NCTX = {}
    LLM_BACKEND_TOKENS = {}
    LLM_BACKEND_PRIVATE_OK = {}
    LLM_BACKEND_PRIVATE_OK_EXPLICIT = {}
    LLM_POOL_FALLBACK_REASON = None
    _LLM_BACKEND_ROLE_CONFIG_ERRORS = []
    AUTH_CONFIGURED_AT_STARTUP = True


def _patch_import_to_return(monkeypatch, fake_module):
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *a, **kw):
        if name == "hive_mind_proxy":
            return fake_module
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fake_import)


def test_missing_guard_functions_degrade_to_exit_2_not_a_crash(monkeypatch):
    _patch_import_to_return(monkeypatch, _FakeProxyMissingGuardFunctions)
    lines, code = check_config.phase_b_render()
    body = "\n".join(lines)
    assert code == 2
    assert "UNKNOWN" in body
    assert "require_auth_when_provider_keys_configured" in body
    assert "require_valid_llm_routing_config" in body


# ── Mutation check: the Phase-B except Exception wrapper actually guards ───
# Mutation-check evidence lives in PR #347's own description (not in a
# HANDOFF.md — that file dies with the builder's worktree, so it is never a
# durable reference). The two facts that matter for reading this test:
# (1) narrowing `except Exception` to `except ValueError` here makes this
# test die with a raw AttributeError traceback instead of a caught
# (lines, 2) result — the array-of-strings LLM_BACKENDS_JSON crash test
# above dies too. (2) restore the wrapper, then confirm `git status` is
# clean before moving on.

def test_phase_b_render_never_raises_on_a_broken_import(monkeypatch, tmp_path):
    """In-process guard-shape check: phase_b_render() must return a
    (lines, 2) tuple, never propagate, when the import itself is broken —
    simulated by pointing PYTHONPATH-independent sys.modules at a stub that
    raises on import. This is a narrower, faster in-process companion to
    the subprocess-based crash tests above. RuntimeError is NOT on the
    safe-message allowlist (SEC-HIGH, fold round) — only its TYPE NAME is
    asserted here, never its message text."""
    import builtins

    real_import = builtins.__import__

    def _boom_import(name, *a, **kw):
        if name == "hive_mind_proxy":
            raise RuntimeError("synthetic import failure carrying no secret")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _boom_import)
    lines, code = check_config.phase_b_render()
    assert code == 2
    assert any("UNAVAILABLE" in line for line in lines)
    assert any("RuntimeError" in line for line in lines)
    assert not any("synthetic import failure" in line for line in lines)


def _stub_systemctl(tmp_path, body):
    """A systemctl that prints one show result. The tests never call the host bus."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / "systemctl"
    exe.write_text("#!/bin/sh\n" + body)
    exe.chmod(0o755)
    return bin_dir


def test_restart_prevent_is_silent_without_systemctl(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert check_config._restart_prevent_line() is None


def test_restart_prevent_ignores_a_unit_that_is_not_loaded(monkeypatch, tmp_path):
    bindir = _stub_systemctl(
        tmp_path, "echo LoadState=not-found\necho RestartPreventExitStatus=\nexit 0\n")
    monkeypatch.setenv("PATH", str(bindir))
    assert check_config._restart_prevent_line() is None


def test_restart_prevent_matches_78_exactly(monkeypatch, tmp_path):
    loaded = "echo LoadState=loaded\necho RestartPreventExitStatus=%s\nexit 0\n"
    bindir = _stub_systemctl(tmp_path, loaded % "178")
    monkeypatch.setenv("PATH", str(bindir))
    warned = check_config._restart_prevent_line()
    assert warned is not None and "RestartPreventExitStatus=78" in warned
    bindir = _stub_systemctl(tmp_path / "exact", loaded % "178 78")
    monkeypatch.setenv("PATH", str(bindir))
    assert check_config._restart_prevent_line() is None


def test_unit_environment_file_bad_json_is_not_would_boot(tmp_path):
    """A bad EnvironmentFile= is what the gateway will load. A good shell does not hide it."""
    unit_env = tmp_path / "unit.env"
    unit_env.write_text('LLM_BACKENDS_JSON=[{"url":"http://a:5000","max_output_tokens":1}]\n')
    bindir = _stub_systemctl(
        tmp_path,
        f"printf '%s\\n' 'EnvironmentFiles={unit_env} (ignore_errors=no)'\nexit 0\n",
    )
    proc = _run(env_overrides={
        "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
        "SECURE_ENV_FILE": "",
        "LLM_BACKENDS_JSON": '[{"url":"http://a:5000","n_ctx":8192,"max_output_tokens":2048}]',
    }, tmp_path=tmp_path)
    text = proc.stdout + proc.stderr
    assert proc.returncode == 1, text
    assert "would boot" not in proc.stdout
    assert "WOULD REFUSE" in proc.stdout
    assert "field n_ctx" in proc.stdout


def test_good_shell_does_not_hide_a_bad_env_file(tmp_path):
    bad = tmp_path / "bad.env"
    bad.write_text('LLM_BACKENDS_JSON=[{"url":"http://a:5000","max_output_tokens":1}]\n')
    proc = _run(env_overrides={
        "SECURE_ENV_FILE": str(bad),
        "LLM_BACKENDS_JSON": '[{"url":"http://a:5000","n_ctx":8192,"max_output_tokens":2048}]',
    }, tmp_path=tmp_path)
    text = proc.stdout + proc.stderr
    assert proc.returncode == 1, text
    assert "would boot" not in proc.stdout
    assert "field n_ctx" in proc.stdout


def test_bad_shell_does_not_stop_a_good_env_file(tmp_path):
    good = tmp_path / "good.env"
    good.write_text('LLM_BACKENDS_JSON=[{"url":"http://a:5000","n_ctx":8192,"max_output_tokens":2048}]\n')
    proc = _run(env_overrides={
        "SECURE_ENV_FILE": str(good),
        "LLM_BACKENDS_JSON": '[{"url":"http://a:5000"}]',
    }, tmp_path=tmp_path)
    text = proc.stdout + proc.stderr
    assert proc.returncode == 0, text
    assert "would boot" in proc.stdout
    assert "WOULD REFUSE" not in proc.stdout


_GOOD_BACKEND = '[{"url":"http://127.0.0.1:9","n_ctx":8192,"max_output_tokens":2048}]'
_KEYED_BACKEND = (
    '[{"url":"http://127.0.0.1:9","token_env":"PROV_KEY",'
    '"n_ctx":8192,"max_output_tokens":2048}]'
)
_FILE_ONLY_BACKEND = (
    '[{"url":"http://127.0.0.1:9","token_env":"FILE_ONLY_TOKEN",'
    '"n_ctx":8192,"max_output_tokens":2048}]'
)
_PROVIDER_MARKER = "sk-file-only-marker"


def _show_stub(tmp_path, stdout, credential="file_only_token"):
    """systemctl and busctl that print a fixed show result. The host bus is not called."""
    bindir = tmp_path / "show-bin"
    bindir.mkdir(parents=True)
    systemctl = bindir / "systemctl"
    systemctl.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdout.write(" + repr(stdout) + ")\n")
    systemctl.chmod(0o755)
    payload = '{"type":"a(ss)","data":[["' + credential + '","/run/unused"]]}' if credential else '{"type":"a(ss)","data":[]}'
    busctl = bindir / "busctl"
    busctl.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdout.write(" + repr(payload + "\n") + ")\n")
    busctl.chmod(0o755)
    return bindir


def _show_stub_empty_bus(tmp_path, stdout):
    return _show_stub(tmp_path, stdout, credential="")


def test_file_override_is_visible_for_the_rest_of_phase_b(tmp_path):
    """Values that exist only in the env file stay visible until phase B returns."""
    env_file = tmp_path / "gateway.env"
    env_file.write_text(
        "ALLOW_UNAUTHENTICATED_PROVIDER_KEYS=1\n"
        f"LLM_BACKENDS_JSON={_KEYED_BACKEND}\n"
        f"PROV_KEY={_PROVIDER_MARKER}\n"
    )
    absent = (
        "ALLOW_UNAUTHENTICATED_PROVIDER_KEYS", "LLM_BACKENDS_JSON",
        "AGENT_TOKENS", "PROV_KEY", "EMBEDDER_URL",
    )
    allowed = _run(env_overrides={"SECURE_ENV_FILE": str(env_file)}, tmp_path=tmp_path, env_unset=absent)
    text = allowed.stdout + allowed.stderr
    assert allowed.returncode == 0, text
    assert "WOULD REFUSE TO START" not in allowed.stdout
    assert _PROVIDER_MARKER not in text

    refused_file = tmp_path / "refused.env"
    refused_file.write_text(
        f"LLM_BACKENDS_JSON={_KEYED_BACKEND}\nPROV_KEY={_PROVIDER_MARKER}\n"
    )
    refused = _run(
        env_overrides={"SECURE_ENV_FILE": str(refused_file)}, tmp_path=tmp_path, env_unset=absent,
    )
    refused_text = refused.stdout + refused.stderr
    assert refused.returncode == 1, refused_text
    assert "WOULD REFUSE TO START" in refused.stdout
    assert _PROVIDER_MARKER not in refused_text


def test_shell_override_is_visible_for_the_rest_of_phase_b(tmp_path):
    """The same auth override in the process environment, with no env file, still decides the exit."""
    absent = ("AGENT_TOKENS",)
    allowed = _run(env_overrides={
        "SECURE_ENV_FILE": "",
        "ALLOW_UNAUTHENTICATED_PROVIDER_KEYS": "1",
        "LLM_BACKENDS_JSON": _KEYED_BACKEND,
        "PROV_KEY": _PROVIDER_MARKER,
    }, tmp_path=tmp_path, env_unset=absent)
    text = allowed.stdout + allowed.stderr
    assert allowed.returncode == 0, text
    assert "WOULD REFUSE TO START" not in allowed.stdout
    assert _PROVIDER_MARKER not in text

    refused = _run(env_overrides={
        "SECURE_ENV_FILE": "",
        "LLM_BACKENDS_JSON": _KEYED_BACKEND,
        "PROV_KEY": _PROVIDER_MARKER,
    }, tmp_path=tmp_path, env_unset=("AGENT_TOKENS", "ALLOW_UNAUTHENTICATED_PROVIDER_KEYS"))
    refused_text = refused.stdout + refused.stderr
    assert refused.returncode == 1, refused_text
    assert "WOULD REFUSE TO START" in refused.stdout
    assert _PROVIDER_MARKER not in refused_text


def test_file_json_with_a_unit_token_keeps_the_hidden_credential_note(tmp_path):
    """JSON that lives only in the file still names a token the unit supplies."""
    env_file = tmp_path / "gateway.env"
    env_file.write_text(f"LLM_BACKENDS_JSON={_FILE_ONLY_BACKEND}\n")
    bindir = _show_stub(
        tmp_path,
        "Environment=\nEnvironmentFiles=\nId=hive-mind-gateway.service\n",
    )
    proc = _run(env_overrides={
        "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
        "SECURE_ENV_FILE": str(env_file),
    }, tmp_path=tmp_path, env_unset=("LLM_BACKENDS_JSON", "FILE_ONLY_TOKEN", "AGENT_TOKENS"))
    text = proc.stdout + proc.stderr
    assert proc.returncode == 0, text
    assert "note: FILE_ONLY_TOKEN is supplied by the gateway's systemd unit" in proc.stdout

    quiet = _show_stub_empty_bus(
        tmp_path / "quiet",
        "Environment=\nEnvironmentFiles=\nId=hive-mind-gateway.service\n",
    )
    proc_quiet = _run(env_overrides={
        "PATH": str(quiet) + os.pathsep + os.environ.get("PATH", ""),
        "SECURE_ENV_FILE": str(env_file),
    }, tmp_path=tmp_path, env_unset=("LLM_BACKENDS_JSON", "FILE_ONLY_TOKEN", "AGENT_TOKENS"))
    assert "supplied by the gateway's systemd unit" not in proc_quiet.stdout


def test_shell_json_with_a_unit_token_keeps_the_hidden_credential_note(monkeypatch):
    """The same note when the JSON is in the process environment and no unit is loaded."""
    import migrate_env as mig
    monkeypatch.setenv("SECURE_ENV_FILE", "")
    monkeypatch.setenv("LLM_BACKENDS_JSON", _FILE_ONLY_BACKEND)
    monkeypatch.delenv("FILE_ONLY_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    monkeypatch.setattr(
        mig, "query_gateway_unit",
        lambda unit: mig.UnitQuery("ok", credential_ids=frozenset({"file_only_token"})),
    )
    lines, code = check_config.phase_b_render()
    text = "\n".join(lines)
    assert code == 0, text
    assert "note: FILE_ONLY_TOKEN is supplied by the gateway's systemd unit" in text

    monkeypatch.setattr(
        mig, "query_gateway_unit",
        lambda unit: mig.UnitQuery("ok", credential_ids=frozenset()),
    )
    lines_quiet, code_quiet = check_config.phase_b_render()
    assert code_quiet == 0
    assert "supplied by the gateway's systemd unit" not in "\n".join(lines_quiet)


def test_empty_file_keys_are_counted_and_the_shell_is_restored(monkeypatch, tmp_path):
    """Empty LLM_BACKENDS_JSON and EMBEDDER_URL in the file count as two latents, then the shell returns."""
    env_file = tmp_path / "empty.env"
    env_file.write_text("LLM_BACKENDS_JSON=\nEMBEDDER_URL=\n")
    monkeypatch.setenv("EMBEDDER_URL", "http://keep.example:9")
    monkeypatch.setenv("LLM_BACKENDS_JSON", _GOOD_BACKEND)
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_file))
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    lines, code = check_config.phase_b_render()
    text = "\n".join(lines)
    assert code == 0, text
    assert "2 latent case(s) present on this install." in text
    assert os.environ["EMBEDDER_URL"] == "http://keep.example:9"
    assert os.environ["LLM_BACKENDS_JSON"] == _GOOD_BACKEND


def test_empty_shell_keys_count_as_two_latents(tmp_path):
    """The same two empty assignments in the process environment, with no env file, count as two."""
    proc = _run(env_overrides={
        "SECURE_ENV_FILE": "",
        "LLM_BACKENDS_JSON": "",
        "EMBEDDER_URL": "",
    }, tmp_path=tmp_path)
    text = proc.stdout + proc.stderr
    assert proc.returncode == 0, text
    assert "2 latent case(s) present on this install." in proc.stdout


def test_loaded_unit_ignores_a_shell_only_bad_json(tmp_path):
    """A loaded unit with no JSON of its own does not take a bad shell LLM_BACKENDS_JSON. --apply ignores that shell too."""
    bad = '[{"url":"http://a:5000"}]'
    bindir = _show_stub(
        tmp_path,
        "Environment=\nEnvironmentFiles=\nId=hive-mind-gateway.service\n",
    )
    proc = _run(env_overrides={
        "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
        "SECURE_ENV_FILE": "",
        "LLM_BACKENDS_JSON": bad,
    }, tmp_path=tmp_path, env_unset=("AGENT_TOKENS",))
    text = proc.stdout + proc.stderr
    assert proc.returncode == 0, text
    assert "WOULD REFUSE" not in proc.stdout
    assert "field n_ctx" not in proc.stdout

    good = '[{"url":"http://a:5000","n_ctx":8192,"max_output_tokens":2048}]'
    escaped = bad.replace("\\", "\\\\").replace('"', '\\"')
    bindir_bad = _show_stub(
        tmp_path / "unit-has-bad",
        f'Environment="LLM_BACKENDS_JSON={escaped}"\nEnvironmentFiles=\nId=hive-mind-gateway.service\n',
    )
    proc_bad = _run(env_overrides={
        "PATH": str(bindir_bad) + os.pathsep + os.environ.get("PATH", ""),
        "SECURE_ENV_FILE": "",
        "LLM_BACKENDS_JSON": good,
    }, tmp_path=tmp_path, env_unset=("AGENT_TOKENS",))
    bad_text = proc_bad.stdout + proc_bad.stderr
    assert proc_bad.returncode == 1, bad_text
    assert "field n_ctx" in proc_bad.stdout


def test_no_loaded_unit_still_sees_a_bad_shell_json(tmp_path):
    """With no loaded unit, the shell remains the picture. A bad shell JSON still refuses."""
    proc = _run(env_overrides={
        "SECURE_ENV_FILE": "",
        "LLM_BACKENDS_JSON": '[{"url":"http://a:5000"}]',
    }, tmp_path=tmp_path, env_unset=("AGENT_TOKENS",))
    text = proc.stdout + proc.stderr
    assert proc.returncode == 1, text
    assert "field n_ctx" in proc.stdout


def test_restart_warning_does_not_claim_an_excluded_roster(monkeypatch, tmp_path):
    bindir = _stub_systemctl(
        tmp_path,
        "echo LoadState=loaded\necho RestartPreventExitStatus=\nexit 0\n",
    )
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("SECURE_ENV_FILE", "")
    monkeypatch.setenv(
        "LLM_BACKENDS_JSON",
        '[{"url":"http://a:5000","n_ctx":8192,"max_output_tokens":2048}]',
    )
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import importlib
    import hive_mind_proxy
    importlib.reload(hive_mind_proxy)
    lines, code = check_config.phase_b_render()
    text = "\n".join(lines)
    assert code == 0, text
    assert "RestartPreventExitStatus=78" in text
    assert "excluded backend or an empty roster" not in text


_ACME_MARKER = "MARKER_ACME_HANDLE_LEAK"
_WIDGET_MARKER = "MARKER_WIDGET_FILE_LEAK"
_ACME_JSON = json.dumps([{
    "url": "http://127.0.0.1:9",
    "token_env": "ACME_HANDLE",
    "n_ctx": 8192,
    "max_output_tokens": 2048,
}])


def _logging_child_stub(tmp_path, show_text):
    """systemctl and busctl that append the environment they inherited."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    log_path = tmp_path / "child-env.log"
    show_path = tmp_path / "show.txt"
    show_path.write_text(show_text)
    bindir = tmp_path / "child-bin"
    bindir.mkdir()
    script = (
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "with open(os.environ['SM_CHILD_ENV_LOG'], 'a', encoding='utf-8') as fh:\n"
        "    fh.write('cmd=' + os.path.basename(sys.argv[0]) + '\\n')\n"
        "    for key in sorted(os.environ):\n"
        "        fh.write(key + '=' + os.environ[key] + '\\n')\n"
        "    fh.write('---\\n')\n"
        "if os.path.basename(sys.argv[0]) == 'busctl':\n"
        "    sys.stdout.write('{\"type\":\"a(ss)\",\"data\":[]}\\n')\n"
        "    raise SystemExit(0)\n"
        "if 'LoadState' in sys.argv:\n"
        "    sys.stdout.write('LoadState=loaded\\nRestartPreventExitStatus=78\\n')\n"
        "    raise SystemExit(0)\n"
        "with open(os.environ['SM_CHILD_SHOW'], encoding='utf-8') as fh:\n"
        "    sys.stdout.write(fh.read())\n"
    )
    for name in ("systemctl", "busctl"):
        exe = bindir / name
        exe.write_text(script)
        exe.chmod(0o755)
    return bindir, log_path


def _quoted_environment(pairs):
    lines = []
    for key, value in pairs.items():
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'Environment="{key}={escaped}"')
    return "\n".join(lines) + "\n"


def _secret_lines():
    return (
        f"LLM_BACKENDS_JSON={_ACME_JSON}\n"
        f"ACME_HANDLE={_ACME_MARKER}\n"
        "WIDGET_FILE=/tmp/widget-not-read\n"
        f"WIDGET={_WIDGET_MARKER}\n"
    )


def test_unsuffixed_token_env_is_not_in_the_child_environment(tmp_path):
    """A token_env name with no secret suffix, and a *_FILE stem, stay out of systemctl and busctl.

    The value is declared in the env file, on the unit Environment= line, or in a unit EnvironmentFile=.
    Both check_config modes are covered. The stub log is the environment the child inherited.
    """
    absent = (
        "LLM_BACKENDS_JSON", "ACME_HANDLE", "WIDGET", "WIDGET_FILE", "AGENT_TOKENS",
    )
    sources = []

    env_file = tmp_path / "gateway.env"
    env_file.write_text(_secret_lines())
    sources.append((
        "dotenv",
        env_file.parent / "dotenv-stub",
        "Environment=\nEnvironmentFiles=\nId=hive-mind-gateway.service\n",
        {"SECURE_ENV_FILE": str(env_file)},
    ))

    unit_show = tmp_path / "unit-show"
    sources.append((
        "environment",
        unit_show,
        _quoted_environment({
            "LLM_BACKENDS_JSON": _ACME_JSON,
            "ACME_HANDLE": _ACME_MARKER,
            "WIDGET_FILE": "/tmp/widget-not-read",
            "WIDGET": _WIDGET_MARKER,
        }) + "EnvironmentFiles=\nId=hive-mind-gateway.service\n",
        {"SECURE_ENV_FILE": ""},
    ))

    unit_env = tmp_path / "unit.env"
    unit_env.write_text(_secret_lines())
    sources.append((
        "environment-file",
        unit_env.parent / "file-stub",
        f"Environment=\nEnvironmentFiles={unit_env} (ignore_errors=no)\nId=hive-mind-gateway.service\n",
        {"SECURE_ENV_FILE": ""},
    ))

    for name, stub_dir, show_text, extra in sources:
        for mode in (["--phase-a-only"], []):
            bindir, log_path = _logging_child_stub(stub_dir / mode_label(mode), show_text)
            proc = _run(mode, env_overrides={
                "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
                "SM_CHILD_ENV_LOG": str(log_path),
                "SM_CHILD_SHOW": str(stub_dir / mode_label(mode) / "show.txt"),
                **extra,
            }, tmp_path=tmp_path, env_unset=absent)
            logged = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
            leaked = [line for line in logged.splitlines() if _ACME_MARKER in line or _WIDGET_MARKER in line]
            assert logged, f"{name} {mode} did not run systemctl"
            assert "cmd=systemctl" in logged
            assert "cmd=busctl" in logged
            assert leaked == [], f"{name} {mode} leaked {leaked}"
            text = proc.stdout + proc.stderr
            assert _ACME_MARKER not in text
            assert _WIDGET_MARKER not in text
            assert "Traceback" not in proc.stderr


def mode_label(mode):
    return "phase-a" if mode else "full"


def test_non_utf8_unit_environment_file_does_not_exit_0(tmp_path):
    """A unit EnvironmentFile that is not UTF-8 is named and is not a clean check."""
    marker = "MARKER_NOTUTF8_UNIT"
    unit_env = tmp_path / "unit.env"
    unit_env.write_bytes(f"{marker}=value\xff\n".encode("latin-1"))
    show = (
        "Environment=\n"
        f"EnvironmentFiles={unit_env} (ignore_errors=no)\n"
        "Id=hive-mind-gateway.service\n"
    )
    good = '[{"url":"http://127.0.0.1:9","n_ctx":8192,"max_output_tokens":2048}]'
    for mode in (["--phase-a-only"], []):
        bindir = _stub_systemctl(tmp_path / mode_label(mode), f"printf '%s\\n' { _show_as_printf(show) }\nexit 0\n")
        # The shell JSON is good. A swallowed decode used to leave that picture and exit 0.
        proc = _run(mode, env_overrides={
            "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
            "SECURE_ENV_FILE": "",
            "LLM_BACKENDS_JSON": good,
        }, tmp_path=tmp_path, env_unset=("AGENT_TOKENS",))
        text = proc.stdout + proc.stderr
        assert proc.returncode != 0, text
        assert f"{unit_env} is not UTF-8" in proc.stdout
        assert marker not in text
        assert "Traceback" not in proc.stderr


def _show_as_printf(show):
    return " ".join("'" + line.replace("'", "'\\''") + "'" for line in show.splitlines())
