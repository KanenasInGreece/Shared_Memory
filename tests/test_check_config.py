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
    this, never let it surface as a raw traceback. SEC-HIGH (fold round,
    PR #347): AttributeError is NOT on the safe-message allowlist, so only
    its TYPE NAME is shown — its own message ('has no attribute') never
    is, even though this particular message happens to carry no secret;
    the policy is type-based, not content-sniffed."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS_JSON": '["http://a:5000"]'},
                tmp_path=tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "UNAVAILABLE" in proc.stdout
    assert "AttributeError" in proc.stdout
    assert "has no attribute" not in proc.stdout
    assert "Traceback" not in proc.stderr
    assert "Phase A" in proc.stdout  # Phase A output still printed


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
    """Ruled: exit STAYS 0 — the gateway genuinely DOES boot on the legacy
    fallback, and exit 1 must keep its one meaning ('would refuse to
    start'), never acquire a second one ('boots on a fleet you probably
    didn't intend'). This also corrects the original D3 wording gap: the
    build brief said this input exits 2 — it does not; this warning is the
    honest report."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS_JSON": "{not json"},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "DECLARED FLEET NOT USABLE" in proc.stdout
    assert "legacy fallback" in proc.stdout


def test_a_clean_llm_backends_json_shows_no_fallback_warning(tmp_path):
    """Negative case for the above: a genuinely usable declared fleet gets
    no fallback warning at all."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS_JSON": '[{"url":"http://a:5000"}]'},
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
    """The existing FALLBACK_REASON rendering is unchanged, and a genuinely
    declared, usable fleet shows NEITHER flagged line."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS": "http://a:5000"},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "NO BACKEND DECLARED" not in proc.stdout
    assert "DECLARED FLEET NOT USABLE" not in proc.stdout


def test_config_empty_and_fallback_reason_lines_are_mutually_exclusive(tmp_path):
    """D1: a declared-but-excluded fleet (FALLBACK_REASON) is never ALSO
    reported as CONFIG_EMPTY (nothing declared at all) — the two states
    are mutually exclusive by construction, and this is the existing
    FALLBACK_REASON line's rendering left unchanged by the W3 addition."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS_JSON": "{not json"},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "DECLARED FLEET NOT USABLE" in proc.stdout
    assert "NO BACKEND DECLARED" not in proc.stdout


# ── §6.2 (M11 + case-0 census, W4/decision:1824) — present-but-empty /
#    composed latents, report-only, rendered in Phase B as one scannable
#    block. ────────────────────────────────────────────────────────────────

def test_w4_census_zero_latents_on_a_clean_declared_fleet(tmp_path):
    proc = _run(env_overrides={"SECURE_ENV_FILE": "",
                                "LLM_BACKENDS_JSON": '[{"url":"http://a:5000","private_ok":true}]'},
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
    """The known latent framework_defaults.py documents: LLM_DEFAULT_TARGET
    present-but-empty, with neither LLM_BACKENDS nor LLM_BACKENDS_JSON
    declared, composes to `LLM_BACKENDS == ['']`."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_DEFAULT_TARGET": ""},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "LLM_BACKENDS composed to ['']" in proc.stdout


# ── §6.3 (R-B announce, W4/decision:1824) — a roles-carrying entry with no
#    explicit private_ok no longer serves role-less traffic. ───────────────

def test_r_b_announce_on_a_roles_only_entry_with_no_explicit_private_ok(tmp_path):
    proc = _run(env_overrides={"SECURE_ENV_FILE": "",
                                "LLM_BACKENDS_JSON": '[{"url":"http://a:5000","roles":["extract"]}]'},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "R-B (W4): role-less traffic no longer reaches this backend" in proc.stdout


def test_r_b_announce_absent_when_private_ok_is_explicit(tmp_path):
    proc = _run(env_overrides={"SECURE_ENV_FILE": "",
                                "LLM_BACKENDS_JSON":
                                    '[{"url":"http://a:5000","roles":["extract"],"private_ok":true}]'},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "R-B (W4)" not in proc.stdout


# ── QA HIGH-1 (fix round) — check_config renders M-5'/P-5' per-entry, the
#    SECOND of the two nominated instruments (the startup log is the
#    first). The closing "Gateway startup refusals ... none" line must
#    also stop reading as an unqualified all-clear once either fires. ──────

def test_m5_announce_on_a_credentialed_backend_with_neither_roles_nor_private_ok(tmp_path):
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "AGENT_TOKENS": "claude:tok_abc",
                                "DEEPSEEK_API_KEY": "sk-test",
                                "LLM_BACKENDS_JSON":
                                    '[{"url":"https://api.deepseek.com/v1","token_env":"DEEPSEEK_API_KEY"}]'},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "M-5' (W4): configured but will NEVER be selected" in proc.stdout
    assert "SEC M-1" in proc.stdout
    assert "none — the gateway would boot with this configuration." in proc.stdout
    assert "Degraded, not clean" in proc.stdout


def test_m5_announce_absent_when_roles_are_declared(tmp_path):
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "AGENT_TOKENS": "claude:tok_abc",
                                "DEEPSEEK_API_KEY": "sk-test",
                                "LLM_BACKENDS_JSON":
                                    '[{"url":"https://api.deepseek.com/v1","token_env":"DEEPSEEK_API_KEY",'
                                    '"roles":["extract"]}]'},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "M-5' (W4)" not in proc.stdout
    assert "Degraded, not clean" not in proc.stdout


def test_p5_announce_roles_absent_says_safe_by_construction(tmp_path):
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "AGENT_TOKENS": "",
                                "LLM_BACKENDS_JSON":
                                    '[{"url":"http://a:5000","private_ok":false}]'},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "P-5' (W4)" in proc.stdout
    assert "safe by construction" in proc.stdout
    assert "NOT safe by construction" not in proc.stdout
    assert "Degraded, not clean" in proc.stdout


def test_p5_announce_roles_carrying_says_not_safe_by_construction(tmp_path):
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "AGENT_TOKENS": "",
                                "LLM_BACKENDS_JSON":
                                    '[{"url":"http://a:5000","private_ok":false,"roles":["extract"]}]'},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "P-5' (W4)" in proc.stdout
    assert "NOT safe by construction" in proc.stdout


def test_p5_announce_absent_on_an_undeclared_backend(tmp_path):
    """The narrowed predicate (`private_ok_explicit AND not private_ok`)
    must NOT fire on the pervasive, unremarkable default -- an undeclared
    backend never states private_ok at all."""
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "AGENT_TOKENS": "",
                                "LLM_BACKENDS_JSON": '[{"url":"http://a:5000","roles":["extract"]}]'},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "P-5' (W4)" not in proc.stdout


def test_no_degraded_warning_qualifier_on_a_fully_explicit_clean_fleet(tmp_path):
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "AGENT_TOKENS": "claude:tok_abc",
                                "LLM_BACKENDS_JSON": '[{"url":"http://a:5000","private_ok":true}]'},
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
    proc = _run(env_overrides={"SECURE_ENV_FILE": "",
                                "LLM_BACKENDS_JSON":
                                    '[{"url":"http://a:5000",'
                                    '"extra_body":{"thinking":{"type":"disabled"}}}]'},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "thinking suppression declared" in proc.stdout
    assert "DeepSeek" in proc.stdout
    # Never the raw dict contents alongside the recognised-shape line.
    assert '"type": "disabled"' not in proc.stdout
    assert '"type":"disabled"' not in proc.stdout


def test_extra_body_absent_renders_none_declared(tmp_path):
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS_JSON": '[{"url":"http://a:5000"}]'},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "extra_body: none declared" in proc.stdout


def test_extra_body_unrecognised_shape_renders_present_unrecognised_never_raw(tmp_path):
    # N2: the unrecognised case must render its OWN fixed phrase, never the
    # dict's contents -- extra_body is operator-supplied and could carry
    # anything, including a key.
    proc = _run(env_overrides={"SECURE_ENV_FILE": "",
                                "LLM_BACKENDS_JSON":
                                    '[{"url":"http://a:5000",'
                                    '"extra_body":{"some_future_key":"sk-should-never-appear"}}]'},
                tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "present, unrecognised shape" in proc.stdout
    assert "sk-should-never-appear" not in proc.stdout
    assert "some_future_key" not in proc.stdout


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
    backends_json = ('[{"url":"http://a:5000?key=s3cr3t-in-url","roles":["bogus"]}]')
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS_JSON": backends_json},
                tmp_path=tmp_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "WOULD REFUSE TO START" in proc.stdout
    assert "s3cr3t-in-url" not in proc.stdout


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
    backends_json = '[{"url":"http://svc:s3cr3t-userinfo-pw@a:5000"}]'
    proc = _run(env_overrides={"SECURE_ENV_FILE": "", "LLM_BACKENDS_JSON": backends_json},
                tmp_path=tmp_path)
    full_output = proc.stdout + proc.stderr
    assert "svc:" not in full_output
    assert "s3cr3t-userinfo-pw" not in full_output
    assert "Traceback" not in full_output


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

class _FakeProxyMissingRoleErrors:
    """A stand-in 'hive_mind_proxy' module missing ONLY the private
    _LLM_BACKEND_ROLE_CONFIG_ERRORS symbol — everything else phase_b_
    render() reads is present, so this isolates that one guard."""
    LLM_BACKENDS = ["http://a:5000"]
    LLM_WEIGHTS = {"http://a:5000": 1.0}
    LLM_BACKEND_MODELS = {"http://a:5000": None}
    LLM_BACKEND_ROLES = {"http://a:5000": None}
    LLM_BACKEND_EXTRAS = {"http://a:5000": None}
    LLM_BACKEND_NCTX = {"http://a:5000": None}
    LLM_BACKEND_TOKENS = {"http://a:5000": None}
    LLM_BACKEND_PRIVATE_OK = {"http://a:5000": True}
    LLM_BACKEND_PRIVATE_OK_EXPLICIT = {"http://a:5000": False}
    LLM_POOL_FALLBACK_REASON = None
    AUTH_CONFIGURED_AT_STARTUP = True  # QA HIGH-1 (fix round): now read for P-5' rendering

    @staticmethod
    def require_auth_when_provider_keys_configured():
        return None

    @staticmethod
    def require_valid_llm_routing_config():
        return None


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


def test_missing_role_config_errors_symbol_degrades_honestly_not_a_crash(monkeypatch):
    _patch_import_to_return(monkeypatch, _FakeProxyMissingRoleErrors)
    lines, code = check_config.phase_b_render()
    body = "\n".join(lines)
    assert "UNKNOWN" in body
    assert "_LLM_BACKEND_ROLE_CONFIG_ERRORS" in body
    assert code == 0  # the missing role-errors symbol alone doesn't block the guard-function path


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
