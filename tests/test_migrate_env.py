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


# ─────────────────────────────────────────────────────────────────────────
# systemctl output parsing (pure)
# ─────────────────────────────────────────────────────────────────────────

def test_parse_systemctl_environment_splits_space_separated_pairs():
    raw = "Environment=PATH=/usr/bin FOO=bar\n"
    assert m._parse_systemctl_environment(raw) == {"PATH": "/usr/bin", "FOO": "bar"}


def test_parse_systemctl_environment_empty_line_yields_nothing():
    assert m._parse_systemctl_environment("Environment=\n") == {}


def test_parse_systemctl_environment_files_extracts_bare_path():
    raw = "EnvironmentFiles=/etc/foo/bar.env (ignore_errors=no)\n"
    assert m._parse_systemctl_environment_files(raw) == ["/etc/foo/bar.env"]


def test_query_gateway_unit_no_systemctl_proceeds_normally(monkeypatch):
    monkeypatch.setattr(m.shutil, "which", lambda name: None)
    q = m.query_gateway_unit("hive-mind-gateway.service")
    assert q.status == "no_systemctl"
    assert q.owned_keys == frozenset()


def test_query_gateway_unit_ok_reports_owned_managed_keys(monkeypatch, tmp_path):
    """The unit-owned branch — stubbed systemctl show output, per the W3
    brief's own note that the env corpus cannot reach this."""
    monkeypatch.setattr(m.shutil, "which", lambda name: "/usr/bin/systemctl")

    class _Proc:
        returncode = 0
        stdout = "Environment=LLM_BACKENDS_JSON=[bogus] FOO=bar\nEnvironmentFiles=\n"
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


# ─────────────────────────────────────────────────────────────────────────
# Raw env-file parsing (pure)
# ─────────────────────────────────────────────────────────────────────────

def test_parse_managed_key_lines_finds_each_key_once():
    lines = ["FOO=1", "EMBEDDER_URL=http://a:8070", "# LLM_BACKENDS=commented", "LLM_BACKENDS=http://a:5000"]
    occ = m.parse_managed_key_lines(lines)
    assert occ["EMBEDDER_URL"] == [1]
    assert occ["LLM_BACKENDS"] == [3]  # the commented line is skipped


def test_duplicate_managed_key_detected():
    lines = ["LLM_BACKENDS=http://a:5000", "LLM_BACKENDS=http://b:5000"]
    occ = m.parse_managed_key_lines(lines)
    assert m.find_duplicate_managed_keys(occ) == ["LLM_BACKENDS"]


def test_no_duplicate_when_each_key_appears_once():
    lines = ["LLM_BACKENDS=http://a:5000", "EMBEDDER_URL=http://a:8070"]
    occ = m.parse_managed_key_lines(lines)
    assert m.find_duplicate_managed_keys(occ) == []


# ─────────────────────────────────────────────────────────────────────────
# The decision ladder — classify() (pure, all 5 cases)
# ─────────────────────────────────────────────────────────────────────────

def _image(**over):
    base = {
        "urls": [], "fallback_reason": None, "config_empty": False,
        "default_target": "http://localhost:5000",
        "private_ok_explicit": {}, "has_token": {},
    }
    base.update(over)
    return base


def test_case_0_present_but_empty_json_first_match():
    lines = ["LLM_BACKENDS_JSON="]
    occ = m.parse_managed_key_lines(lines)
    assert m.classify(_image(), occ, lines) == m.CASE_JSON_PRESENT_EMPTY


def test_case_1_json_usable():
    lines = ['LLM_BACKENDS_JSON=[{"url":"http://a:5000"}]']
    occ = m.parse_managed_key_lines(lines)
    img = _image(urls=["http://a:5000"])
    assert m.classify(img, occ, lines) == m.CASE_JSON_USABLE


def test_case_2_json_present_but_unusable_takes_precedence_over_csv():
    """Precedence + JSON-key survival (M8's own corpus item): a fallback
    reason on the JSON key means touch NOTHING, even with a live CSV
    line sitting right there."""
    lines = ['LLM_BACKENDS_JSON={not json', "LLM_BACKENDS=http://a:5000"]
    occ = m.parse_managed_key_lines(lines)
    img = _image(fallback_reason="LLM_BACKENDS_JSON produced no usable backend")
    assert m.classify(img, occ, lines) == m.CASE_JSON_UNUSABLE


def test_case_3_csv_live_no_json_key_at_all():
    lines = ["LLM_BACKENDS=http://a:5000,http://b:5000"]
    occ = m.parse_managed_key_lines(lines)
    assert m.classify(_image(), occ, lines) == m.CASE_CSV_LIVE


def test_case_4_fallback_neither_key():
    lines = ["EMBEDDER_URL=http://a:8070"]
    occ = m.parse_managed_key_lines(lines)
    img = _image(config_empty=True)
    assert m.classify(img, occ, lines) == m.CASE_FALLBACK


# ─────────────────────────────────────────────────────────────────────────
# plan_case_json_usable — the eligibility predicate + loader-shape tolerance
# ─────────────────────────────────────────────────────────────────────────

def test_plan_json_usable_adds_private_ok_only_to_eligible_entries():
    raw = json.dumps([
        {"url": "http://a:5000"},                                    # eligible
        {"url": "http://b:5000", "roles": ["extract"]},               # roles-carrying: untouched
        {"url": "https://api.example.com", "token_env": "X_KEY"},     # credentialed: untouched
        {"url": "http://c:5000", "private_ok": False},                # already explicit: untouched
    ])
    img = _image(
        private_ok_explicit={"http://a:5000": False, "http://b:5000": False,
                              "https://api.example.com": False, "http://c:5000": True},
        has_token={"http://a:5000": False, "http://b:5000": False,
                   "https://api.example.com": True, "http://c:5000": False},
    )
    new_entries, touched = m.plan_case_json_usable(img, raw)
    assert touched == ["http://a:5000"]
    by_url = {e["url"]: e for e in new_entries}
    assert by_url["http://a:5000"]["private_ok"] is True
    assert "private_ok" not in by_url["http://b:5000"]
    assert "private_ok" not in by_url["https://api.example.com"]
    assert by_url["http://c:5000"]["private_ok"] is False  # untouched, byte-value preserved


def test_plan_json_usable_already_fully_explicit_touches_nothing():
    raw = json.dumps([{"url": "http://a:5000", "private_ok": True}])
    img = _image(private_ok_explicit={"http://a:5000": True}, has_token={"http://a:5000": False})
    new_entries, touched = m.plan_case_json_usable(img, raw)
    assert touched == []


def test_plan_json_usable_array_of_strings_graceful_refusal_never_raises():
    """Loader-shape tolerance: an array of bare strings must not crash the
    migration's OWN parsing — returns (None, []), never raises."""
    raw = json.dumps(["http://a:5000", "http://b:5000"])
    new_entries, touched = m.plan_case_json_usable(_image(), raw)
    assert new_entries is None
    assert touched == []


def test_plan_json_usable_duplicate_urls_within_the_array_both_kept():
    """'urls' keeps both duplicates even though a dict-keyed image
    collapses last-wins — the migration's own list traversal must not be
    surprised by this."""
    raw = json.dumps([{"url": "http://a:5000"}, {"url": "http://a:5000", "roles": ["extract"]}])
    img = _image(private_ok_explicit={"http://a:5000": False}, has_token={"http://a:5000": False})
    new_entries, touched = m.plan_case_json_usable(img, raw)
    assert len(new_entries) == 2


def test_plan_case_csv_live_converts_to_json_with_effective_private_ok_true():
    entries = m.plan_case_csv_live(_image(), "http://a:5000@2,http://b:5000")
    assert entries == [
        {"url": "http://a:5000", "weight": 2.0, "private_ok": True},
        {"url": "http://b:5000", "weight": 1.0, "private_ok": True},
    ]


# ─────────────────────────────────────────────────────────────────────────
# Interactive confirm — y / N / EOF / deadline, probe stubbed both ways
# ─────────────────────────────────────────────────────────────────────────

def test_read_confirm_explicit_y_via_injected_reader():
    assert m.read_confirm(reader=lambda: "y\n") is True


def test_read_confirm_explicit_n_via_injected_reader():
    assert m.read_confirm(reader=lambda: "n\n") is False


def test_read_confirm_eof_via_injected_reader_is_no():
    assert m.read_confirm(reader=lambda: "") is False


def test_read_confirm_anything_else_is_no():
    assert m.read_confirm(reader=lambda: "yes please\n") is False


def test_read_confirm_deadline_with_real_select_is_no(monkeypatch):
    """No reader injected -> real select.select path; stdin never becomes
    ready within a near-zero deadline -> No, never blocks the suite."""
    monkeypatch.setattr(m.select, "select", lambda *a, **k: ([], [], []))
    assert m.read_confirm(deadline=0.01) is False


def test_build_confirm_question_carries_the_discriminating_observation_200():
    probe = {"answered": True, "status": 200, "parsed_model_list": True, "n_models": 3}
    q = m.build_confirm_question("http://a:5000", probe)
    assert "HTTP 200" in q
    assert "OpenAI model list" in q
    assert "3 models" in q


def test_build_confirm_question_carries_401_observation():
    probe = {"answered": True, "status": 401, "parsed_model_list": False, "n_models": None}
    q = m.build_confirm_question("http://a:5000", probe)
    assert "401" in q
    assert "credential the gateway does not have" in q


def test_build_confirm_question_did_not_answer():
    q = m.build_confirm_question("http://a:5000", {"answered": False, "status": None,
                                                     "parsed_model_list": False, "n_models": None})
    assert "did not answer" in q


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


def test_two_layer_compare_identical_images_ok():
    pre = _full_image()
    post = _full_image()
    ok, msg = m.two_layer_compare(pre, post, reported_writes=set())
    assert ok, msg


def test_two_layer_compare_is_order_independent_of_dict_construction():
    """Mutation check (M8: 're-ordering the corpus'): building the SAME
    logical image with dict keys/urls inserted in a different order must
    not change the verdict — the comparison keys on url identity, never
    positional/iteration order."""
    pre = _full_image(urls=["http://a:5000", "http://b:5000"],
                       weights={"http://b:5000": 1.0, "http://a:5000": 1.0},
                       has_token={"http://b:5000": False, "http://a:5000": False},
                       models={"http://b:5000": None, "http://a:5000": None},
                       roles={"http://b:5000": None, "http://a:5000": None},
                       n_ctx={"http://b:5000": None, "http://a:5000": None},
                       max_inflight={"http://b:5000": None, "http://a:5000": None},
                       extra_body={"http://b:5000": None, "http://a:5000": None},
                       private_ok={"http://b:5000": True, "http://a:5000": True},
                       private_ok_explicit={"http://b:5000": False, "http://a:5000": False})
    post = _full_image(urls=["http://b:5000", "http://a:5000"],
                        weights={"http://a:5000": 1.0, "http://b:5000": 1.0},
                        has_token={"http://a:5000": False, "http://b:5000": False},
                        models={"http://a:5000": None, "http://b:5000": None},
                        roles={"http://a:5000": None, "http://b:5000": None},
                        n_ctx={"http://a:5000": None, "http://b:5000": None},
                        max_inflight={"http://a:5000": None, "http://b:5000": None},
                        extra_body={"http://a:5000": None, "http://b:5000": None},
                        private_ok={"http://a:5000": True, "http://b:5000": True},
                        private_ok_explicit={"http://a:5000": False, "http://b:5000": False})
    ok, msg = m.two_layer_compare(pre, post, reported_writes=set())
    assert ok, msg


def test_two_layer_compare_url_set_changed_fails():
    pre = _full_image()
    post = _full_image(urls=["http://a:5000", "http://b:5000"],
                        weights={"http://a:5000": 1.0, "http://b:5000": 1.0},
                        has_token={"http://a:5000": False, "http://b:5000": False},
                        models={"http://a:5000": None, "http://b:5000": None},
                        roles={"http://a:5000": None, "http://b:5000": None},
                        n_ctx={"http://a:5000": None, "http://b:5000": None},
                        max_inflight={"http://a:5000": None, "http://b:5000": None},
                        extra_body={"http://a:5000": None, "http://b:5000": None},
                        private_ok={"http://a:5000": True, "http://b:5000": True},
                        private_ok_explicit={"http://a:5000": False, "http://b:5000": False})
    ok, msg = m.two_layer_compare(pre, post, reported_writes=set())
    assert not ok


@pytest.mark.parametrize("field", ["weights", "has_token", "models", "roles", "n_ctx",
                                    "max_inflight", "extra_body", "private_ok"])
def test_two_layer_compare_any_behavioural_field_drift_fails(field):
    """The 'make the migration write a wrong value' mutant, applied per
    Layer-1 field: a single wrong value anywhere in the behavioural set
    must be caught."""
    pre = _full_image()
    post = _full_image()
    post[field] = dict(post[field])
    post[field]["http://a:5000"] = "WRONG-VALUE-MUTANT"
    ok, msg = m.two_layer_compare(pre, post, reported_writes=set())
    assert not ok, f"field {field!r} drifted silently"


def test_two_layer_compare_fallback_reason_class_change_fails():
    pre = _full_image()
    post = _full_image(fallback_reason="something excluded everything")
    ok, _ = m.two_layer_compare(pre, post, reported_writes=set())
    assert not ok


def test_two_layer_compare_guard_verdict_change_fails():
    pre = _full_image()
    post = _full_image(guard_routing={"raised": True, "message": "FATAL: ..."})
    ok, _ = m.two_layer_compare(pre, post, reported_writes=set())
    assert not ok


def test_two_layer_compare_private_ok_explicit_moves_only_on_reported_entries():
    pre = _full_image()
    post = _full_image(private_ok_explicit={"http://a:5000": True})
    ok, msg = m.two_layer_compare(pre, post, reported_writes=set())
    assert not ok, "unreported private_ok_explicit movement must fail"
    ok2, msg2 = m.two_layer_compare(pre, post, reported_writes={"http://a:5000"})
    assert ok2, msg2


def test_two_layer_compare_private_ok_explicit_cannot_move_backward_even_if_reported():
    pre = _full_image(private_ok_explicit={"http://a:5000": True})
    post = _full_image(private_ok_explicit={"http://a:5000": False})
    ok, _ = m.two_layer_compare(pre, post, reported_writes={"http://a:5000"})
    assert not ok, "True -> False is not the planned direction"


def test_two_layer_compare_config_empty_moves_only_on_confirmed_fallback_write():
    pre = _full_image(urls=["http://x:5000"], config_empty=True,
                       weights={"http://x:5000": 1.0}, has_token={"http://x:5000": False},
                       models={"http://x:5000": None}, roles={"http://x:5000": None},
                       n_ctx={"http://x:5000": None}, max_inflight={"http://x:5000": None},
                       extra_body={"http://x:5000": None}, private_ok={"http://x:5000": True},
                       private_ok_explicit={"http://x:5000": False})
    post = _full_image(urls=["http://x:5000"], config_empty=False,
                        weights={"http://x:5000": 1.0}, has_token={"http://x:5000": False},
                        models={"http://x:5000": None}, roles={"http://x:5000": None},
                        n_ctx={"http://x:5000": None}, max_inflight={"http://x:5000": None},
                        extra_body={"http://x:5000": None}, private_ok={"http://x:5000": True},
                        private_ok_explicit={"http://x:5000": True})
    ok, _ = m.two_layer_compare(pre, post, reported_writes={"http://x:5000"})
    assert not ok, "config_empty moved without the fallback-materialised marker"
    ok2, msg2 = m.two_layer_compare(
        pre, post, reported_writes={"http://x:5000", "__fallback_materialised__"})
    assert ok2, msg2


# ─────────────────────────────────────────────────────────────────────────
# No-secret / scrub discipline
# ─────────────────────────────────────────────────────────────────────────

def test_capture_json_never_contains_a_token_env_value_only_has_token_bool(tmp_path, monkeypatch):
    """The actual secret (a token_env-resolved value) must never appear —
    only has_token: bool."""
    _force_no_unit(monkeypatch)
    secret = "s3cr3t-must-never-appear-in-the-capture-file"
    env_text = (
        'LLM_BACKENDS_JSON=[{"url":"https://api.example.com/v1","token_env":"X_KEY"}]\n'
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
    payload = json.loads(raw)
    assert payload["image"]["has_token"]["https://api.example.com/v1"] is True


def test_capture_json_keeps_a_raw_userinfo_url_by_ruled_design(tmp_path, monkeypatch):
    """RULED (W3 brief, 'The capture JSON'): raw URLs are needed for the
    equality and stay — a userinfo URL is NOT scrubbed inside the capture
    JSON itself (unlike every report/die path, which IS scrubbed — see the
    scrub test below). Fixture form only, per fact:1195.

    Uses the LLM_BACKENDS_JSON form deliberately, not the legacy CSV form:
    hive_mind_proxy._parse_backend()'s own `entry.partition("@")` (the
    url@weight split) mis-splits a userinfo URL's OWN '@' — a pre-existing
    behaviour in that function, out of W3's scope, not something to route
    a new test through."""
    _force_no_unit(monkeypatch)
    env_text = 'LLM_BACKENDS_JSON=[{"url":"http://svc:form-only-fixture-credential@a:5000"}]\n'
    env_path = _write_env(tmp_path, env_text)
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    out_path = tmp_path / "pre.json"
    rc = m.do_capture(str(out_path), "hive-mind-gateway-does-not-exist.service")
    assert rc == m.EXIT_OK
    raw = out_path.read_text()
    assert "svc:form-only-fixture-credential@a:5000" in raw


def test_report_never_leaks_a_userinfo_url_verbatim(tmp_path, monkeypatch, capsys):
    """Scrub test: a userinfo URL anywhere in a report/die path renders
    scrubbed (fact:1195-style form-only fixture — no real credential)."""
    _force_no_unit(monkeypatch)
    secret = "s3cr3t-in-url-must-be-scrubbed"
    env_path = _write_env(
        tmp_path, f"LLM_BACKENDS=http://svc:{secret}@a:5000\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    rc = m.do_apply_or_dryrun(None, False, "hive-mind-gateway-does-not-exist.service")
    out = capsys.readouterr().out
    assert secret not in out


# ─────────────────────────────────────────────────────────────────────────
# End-to-end: do_capture / do_apply_or_dryrun against a real fixture .env,
# real loader subprocess (this suite's own deps cover it).
# ─────────────────────────────────────────────────────────────────────────

_NO_UNIT = "hive-mind-gateway-does-not-exist-in-this-test.service"


def test_e2e_no_env_file_reports_and_exits_0(tmp_path, monkeypatch):
    _force_no_unit(monkeypatch)
    monkeypatch.setenv("SECURE_ENV_FILE", str(tmp_path / "does-not-exist.env"))
    rc = m.do_apply_or_dryrun(None, True, _NO_UNIT)
    assert rc == m.EXIT_OK


def test_e2e_csv_live_dry_run_then_apply_then_second_run_is_a_true_noop(tmp_path, monkeypatch, capsys):
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "LLM_BACKENDS=http://localhost:5000\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))

    rc = m.do_apply_or_dryrun(None, False, _NO_UNIT)
    assert rc == m.EXIT_OK
    dry_out = capsys.readouterr().out
    assert "DRY RUN" in dry_out
    assert env_path.read_text() == "LLM_BACKENDS=http://localhost:5000\n", "dry run wrote to the file"

    rc = m.do_apply_or_dryrun(None, True, _NO_UNIT)
    assert rc == m.EXIT_OK
    applied_text = env_path.read_text()
    assert "LLM_BACKENDS_JSON=" in applied_text
    assert "# migrated to LLM_BACKENDS_JSON by migrate_env.py" in applied_text

    mtime_after_first_apply = env_path.stat().st_mtime
    hash_after_first_apply = applied_text
    time.sleep(0.05)

    # SECOND RUN — must be a true no-op: file hash AND mtime unchanged
    # (M-D10: no planned writes -> no temp+mv at all).
    rc2 = m.do_apply_or_dryrun(None, True, _NO_UNIT)
    assert rc2 == m.EXIT_OK
    assert env_path.stat().st_mtime == mtime_after_first_apply, "mtime moved on a no-op re-run"
    assert env_path.read_text() == hash_after_first_apply, "content moved on a no-op re-run"


def test_e2e_case1_json_usable_adds_private_ok_and_is_idempotent(tmp_path, monkeypatch):
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, 'LLM_BACKENDS_JSON=[{"url":"http://a:5000"}]\n')
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))

    rc = m.do_apply_or_dryrun(None, True, _NO_UNIT)
    assert rc == m.EXIT_OK
    text = env_path.read_text()
    assert '"private_ok": true' in text

    mtime = env_path.stat().st_mtime
    rc2 = m.do_apply_or_dryrun(None, True, _NO_UNIT)
    assert rc2 == m.EXIT_OK
    assert env_path.stat().st_mtime == mtime, "already-explicit JSON re-run must not rewrite"


def test_e2e_roles_carrying_entry_left_byte_for_byte(tmp_path, monkeypatch):
    _force_no_unit(monkeypatch)
    original = ('EMBEDDER_URL=http://a:8070\nRERANKER_URL=http://a:8071\n'
                'LLM_BACKENDS_JSON=[{"url":"http://a:5000","roles":["extract"]}]\n')
    env_path = _write_env(tmp_path, original)
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    rc = m.do_apply_or_dryrun(None, True, _NO_UNIT)
    assert rc == m.EXIT_OK
    assert env_path.read_text() == original, "a roles-carrying entry must be left byte-for-byte"


def test_e2e_credentialed_neither_key_entry_untouched_and_reported(tmp_path, monkeypatch, capsys):
    _force_no_unit(monkeypatch)
    original = ('EMBEDDER_URL=http://a:8070\nRERANKER_URL=http://a:8071\n'
                'LLM_BACKENDS_JSON=[{"url":"https://api.example.com/v1","token_env":"X_KEY"}]\n'
                'X_KEY=irrelevant-for-this-test\n')
    env_path = _write_env(tmp_path, original)
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("X_KEY", "some-token-value")
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    rc = m.do_apply_or_dryrun(None, True, _NO_UNIT)
    assert rc == m.EXIT_OK
    assert env_path.read_text() == original


def test_e2e_present_but_empty_json_key_reported_never_written(tmp_path, monkeypatch, capsys):
    _force_no_unit(monkeypatch)
    original = "EMBEDDER_URL=http://a:8070\nRERANKER_URL=http://a:8071\nLLM_BACKENDS_JSON=\n"
    env_path = _write_env(tmp_path, original)
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    rc = m.do_apply_or_dryrun(None, True, _NO_UNIT)
    assert rc == m.EXIT_OK
    assert env_path.read_text() == original
    out = capsys.readouterr().out
    assert "present but EMPTY" in out
    assert "/health" in out


def test_e2e_empty_string_endpoint_keys_untouched_and_counted(tmp_path, monkeypatch, capsys):
    _force_no_unit(monkeypatch)
    original = "EMBEDDER_URL=\nRERANKER_URL=\nLLM_BACKENDS=http://a:5000\n"
    env_path = _write_env(tmp_path, original)
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    rc = m.do_apply_or_dryrun(None, False, _NO_UNIT)
    assert rc == m.EXIT_OK
    out = capsys.readouterr().out
    assert "EMBEDDER_URL present but EMPTY" in out
    assert "RERANKER_URL present but EMPTY" in out


def test_e2e_duplicate_managed_key_refused(tmp_path, monkeypatch):
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "LLM_BACKENDS=http://a:5000\nLLM_BACKENDS=http://b:5000\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    rc = m.do_apply_or_dryrun(None, True, _NO_UNIT)
    assert rc == m.EXIT_STOP


def test_e2e_crlf_file_stays_crlf_when_written(tmp_path, monkeypatch):
    _force_no_unit(monkeypatch)
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"LLM_BACKENDS=http://a:5000\r\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    rc = m.do_apply_or_dryrun(None, True, _NO_UNIT)
    assert rc == m.EXIT_OK
    raw = env_path.read_bytes()
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")


def test_e2e_crlf_host_with_no_changes_stays_crlf_unwritten(tmp_path, monkeypatch):
    """A CRLF host with NO planned changes must not be silently normalised —
    no write happens at all (M-D10), so the byte content is untouched."""
    _force_no_unit(monkeypatch)
    original = (b'EMBEDDER_URL=http://a:8070\r\nRERANKER_URL=http://a:8071\r\n'
                b'LLM_BACKENDS_JSON=[{"url":"http://a:5000","private_ok":true}]\r\n')
    env_path = tmp_path / ".env"
    env_path.write_bytes(original)
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    rc = m.do_apply_or_dryrun(None, True, _NO_UNIT)
    assert rc == m.EXIT_OK
    assert env_path.read_bytes() == original


def test_e2e_symlinked_env_file_is_resolved_and_target_is_operated_on(tmp_path, monkeypatch):
    _force_no_unit(monkeypatch)
    real_target = tmp_path / "real.env"
    real_target.write_text("LLM_BACKENDS=http://a:5000\n")
    link = tmp_path / ".env"
    link.symlink_to(real_target)
    monkeypatch.setenv("SECURE_ENV_FILE", str(link))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    rc = m.do_apply_or_dryrun(None, True, _NO_UNIT)
    assert rc == m.EXIT_OK
    assert "LLM_BACKENDS_JSON=" in real_target.read_text()
    assert link.is_symlink(), "the symlink itself must survive (operate on the target, not replace the link)"


def test_e2e_read_only_env_file_with_a_planned_write_refuses(tmp_path, monkeypatch):
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "LLM_BACKENDS=http://a:5000\n")
    env_path.chmod(0o444)
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    try:
        rc = m.do_apply_or_dryrun(None, True, _NO_UNIT)
        assert rc == m.EXIT_STOP
    finally:
        env_path.chmod(0o644)


def test_e2e_read_only_env_file_with_no_planned_write_is_a_silent_pass(tmp_path, monkeypatch):
    """A no-op run against a read-only .env (our fleet's predicted state)
    must report and exit 0 — only a PLANNED write against an unwritable
    target refuses."""
    _force_no_unit(monkeypatch)
    env_path = _write_env(
        tmp_path,
        'EMBEDDER_URL=http://a:8070\nRERANKER_URL=http://a:8071\n'
        'LLM_BACKENDS_JSON=[{"url":"http://a:5000","private_ok":true}]\n')
    env_path.chmod(0o444)
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    try:
        rc = m.do_apply_or_dryrun(None, True, _NO_UNIT)
        assert rc == m.EXIT_OK
    finally:
        env_path.chmod(0o644)


def test_e2e_fallback_case_non_interactive_writes_nothing(tmp_path, monkeypatch, capsys):
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "EMBEDDER_URL=http://a:8070\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    rc = m.do_apply_or_dryrun(None, True, _NO_UNIT, confirm_reader=None)
    assert rc == m.EXIT_OK
    assert "LLM_BACKENDS_JSON=" not in env_path.read_text()
    out = capsys.readouterr().out
    assert "Non-interactive" in out


def test_e2e_fallback_case_interactive_confirm_yes_writes_and_freezes_default_target(tmp_path, monkeypatch):
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "EMBEDDER_URL=http://a:8070\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setattr(m, "probe_backend", lambda url, timeout=3.0: {
        "answered": False, "status": None, "parsed_model_list": False, "n_models": None})
    rc = m.do_apply_or_dryrun(None, True, _NO_UNIT, confirm_reader=lambda: "y\n")
    assert rc == m.EXIT_OK
    text = env_path.read_text()
    assert "LLM_BACKENDS_JSON=" in text
    assert "LLM_DEFAULT_TARGET" not in text, "R-A: LLM_DEFAULT_TARGET is never written"


def test_e2e_fallback_case_interactive_confirm_no_writes_nothing(tmp_path, monkeypatch):
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "EMBEDDER_URL=http://a:8070\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setattr(m, "probe_backend", lambda url, timeout=3.0: {
        "answered": False, "status": None, "parsed_model_list": False, "n_models": None})
    rc = m.do_apply_or_dryrun(None, True, _NO_UNIT, confirm_reader=lambda: "n\n")
    assert rc == m.EXIT_OK
    assert "LLM_BACKENDS_JSON=" not in env_path.read_text()


def test_e2e_fallback_case_empty_default_target_never_probed_never_materialised(tmp_path, monkeypatch):
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "EMBEDDER_URL=http://a:8070\nLLM_DEFAULT_TARGET=\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    probed = []
    monkeypatch.setattr(m, "probe_backend", lambda url, timeout=3.0: probed.append(url) or {
        "answered": True, "status": 200, "parsed_model_list": True, "n_models": 1})
    rc = m.do_apply_or_dryrun(None, True, _NO_UNIT, confirm_reader=lambda: "y\n")
    assert rc == m.EXIT_OK
    assert probed == [], "an empty effective target must never be probed"
    assert "LLM_BACKENDS_JSON=" not in env_path.read_text()


# ─────────────────────────────────────────────────────────────────────────
# Capture-schema check
# ─────────────────────────────────────────────────────────────────────────

def test_newer_capture_schema_refused_with_remedy(tmp_path, monkeypatch):
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "LLM_BACKENDS=http://a:5000\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    preimage = tmp_path / "pre.json"
    preimage.write_text(json.dumps({"capture_schema": m.CAPTURE_SCHEMA + 1, "image": {}}))
    rc = m.do_apply_or_dryrun(str(preimage), False, _NO_UNIT)
    assert rc == m.EXIT_STOP


def test_older_capture_schema_accepted(tmp_path, monkeypatch):
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "LLM_BACKENDS=http://a:5000\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    preimage = tmp_path / "pre.json"
    real_image = json.loads(
        json.dumps(_full_image_from_capture(tmp_path, monkeypatch)))
    preimage.write_text(json.dumps({"capture_schema": m.CAPTURE_SCHEMA, "image": real_image}))
    rc = m.do_apply_or_dryrun(str(preimage), False, _NO_UNIT)
    assert rc == m.EXIT_OK


def _full_image_from_capture(tmp_path, monkeypatch):
    """Helper: a real, complete image via the actual capture path, reused
    by the schema test above so its fixture stays honest (a real loader
    output) rather than a hand-typed partial dict."""
    out = tmp_path / "helper-pre.json"
    m.do_capture(str(out), _NO_UNIT)
    return json.loads(out.read_text())["image"]


def test_malformed_preimage_json_is_refused(tmp_path, monkeypatch):
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "LLM_BACKENDS=http://a:5000\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    preimage = tmp_path / "pre.json"
    preimage.write_text("{not json")
    rc = m.do_apply_or_dryrun(str(preimage), False, _NO_UNIT)
    assert rc == m.EXIT_STOP


# ─────────────────────────────────────────────────────────────────────────
# Backup + restore mechanics
# ─────────────────────────────────────────────────────────────────────────

def test_backup_is_written_outside_the_tree_mode_600_content_matches(tmp_path, monkeypatch):
    fake_home = tmp_path / "fake-home"
    monkeypatch.setenv("HOME", str(fake_home))
    target = tmp_path / ".env"
    target.write_text("LLM_BACKENDS=http://a:5000\n")
    backup = m.backup_env_file(target)
    assert backup.read_text() == target.read_text()
    assert str(backup).startswith(str(fake_home))
    import stat as _stat
    assert _stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_restore_from_backup_byte_verifies(tmp_path, monkeypatch):
    fake_home = tmp_path / "fake-home"
    monkeypatch.setenv("HOME", str(fake_home))
    target = tmp_path / ".env"
    target.write_text("ORIGINAL\n")
    backup = m.backup_env_file(target)
    target.write_text("CORRUPTED\n")
    m.restore_from_backup(target, backup)
    assert target.read_text() == "ORIGINAL\n"


def test_glob_no_stray_temp_file_left_after_a_write(tmp_path, monkeypatch):
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "LLM_BACKENDS=http://a:5000\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    rc = m.do_apply_or_dryrun(None, True, _NO_UNIT)
    assert rc == m.EXIT_OK
    leftovers = list(tmp_path.glob(".env.migrate_env.*"))
    assert leftovers == [], f"stray temp file(s) left in-tree: {leftovers}"
