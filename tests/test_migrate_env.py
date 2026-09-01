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


@pytest.fixture(autouse=True)
def _home_never_defaults_to_the_real_one(tmp_path, monkeypatch):
    """QA LOW (fix round): every test in this file gets a throwaway HOME by
    default — a test that calls do_apply_or_dryrun()/do_capture() without
    itself overriding HOME (every no-write/dry-run/refusal path; none of
    them currently reach backup_env_file(), the only HOME consumer) used
    to inherit the AMBIENT real HOME for the loader subprocess's own env.
    Currently harmless (importing hive_mind_proxy does no $HOME filesystem
    work) but this closes the gap at the harness level rather than resting
    on that property. A test that needs a SPECIFIC HOME still overrides it
    explicitly afterward (monkeypatch's last-write-wins)."""
    monkeypatch.setenv("HOME", str(tmp_path / "default-fixture-home"))


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
    assert m._parse_systemctl_environment_files(raw) == ["/etc/foo/bar.env"]


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
# verify_only_planned_keys_changed — the FILE-LEVEL check (SEC M-4 / QA MED-9)
# ─────────────────────────────────────────────────────────────────────────

def test_verify_only_planned_keys_changed_ok_when_nothing_moved():
    lines = ["EMBEDDER_URL=http://a:8070", "LLM_BACKENDS_JSON=[{\"url\":\"http://a:5000\"}]"]
    ok, msg = m.verify_only_planned_keys_changed(lines, list(lines), planned_keys=set())
    assert ok, msg


def test_verify_only_planned_keys_changed_ok_when_only_planned_key_moved():
    pre = ["EMBEDDER_URL=http://a:8070"]
    post = ["EMBEDDER_URL=http://a:9090"]
    ok, msg = m.verify_only_planned_keys_changed(pre, post, planned_keys={"EMBEDDER_URL"})
    assert ok, msg


def test_verify_only_planned_keys_changed_catches_an_unplanned_move():
    """The property this check exists for: a managed key changing that
    was NOT in this run's planned moves — exactly what SEC M-4 / QA MED-9
    named (a wrong EMBEDDER_URL/RERANKER_URL write passing the semantic
    two_layer_compare() green, because that check never carries either
    field)."""
    pre = ["EMBEDDER_URL=http://a:8070", "LLM_BACKENDS_JSON=[{\"url\":\"http://a:5000\"}]"]
    post = ["EMBEDDER_URL=http://SOMETHING-ELSE:9999", "LLM_BACKENDS_JSON=[{\"url\":\"http://a:5000\"}]"]
    ok, msg = m.verify_only_planned_keys_changed(pre, post, planned_keys={"LLM_BACKENDS_JSON"})
    assert not ok
    assert "EMBEDDER_URL" in msg


def test_e2e_post_write_file_level_check_is_wired_to_restore_on_divergence(
        tmp_path, monkeypatch, capsys):
    """SEC M-4 / QA MED-9 wiring test: force verify_only_planned_keys_changed
    to report a divergence and confirm do_apply_or_dryrun restores from
    backup and returns EXIT_STOP rather than reporting success — proving
    this SECOND check is actually consulted, not merely defined."""
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "LLM_BACKENDS=http://a:5000\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setattr(m, "verify_only_planned_keys_changed",
                         lambda pre, post, planned: (False, "synthetic unplanned move for this test"))
    rc = m.do_apply_or_dryrun(None, True, _NO_UNIT)
    out = capsys.readouterr().out
    assert rc == m.EXIT_STOP
    assert "POST-WRITE FILE DIVERGED" in out
    assert "synthetic unplanned move for this test" in out
    # And the restore actually happened — the file is back to its original content.
    assert env_path.read_text() == "LLM_BACKENDS=http://a:5000\n"


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


def test_classify_case_2_requires_the_json_key_to_actually_be_present():
    """QA H2 (fix round): `fallback_reason` truthy is NOT sufficient for
    case 2 on its own — the KEY must be present in the file too. Without
    the `json_idxs and` guard, a stale/inconsistent image (fallback_reason
    set with no JSON key at all) would still misreport case 2 instead of
    falling through to whatever the CSV/fallback state actually is. This
    is what makes the classify() outcome track the FIXTURE rather than a
    hardcoded field: deleting the JSON key from `lines`/`occurrences`
    (simulated here directly, no JSON line at all) with a live CSV present
    now correctly lands in CASE_CSV_LIVE even if `fallback_reason` were
    (incorrectly) still set on the image — pre-fix this returned
    CASE_JSON_UNUSABLE regardless."""
    lines = ["LLM_BACKENDS=http://a:5000"]  # no LLM_BACKENDS_JSON line at all
    occ = m.parse_managed_key_lines(lines)
    img = _image(fallback_reason="stale/inconsistent — no JSON key backs this")
    assert m.classify(img, occ, lines) == m.CASE_CSV_LIVE


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


def test_plan_json_usable_mixed_array_untouched_entry_is_semantically_preserved():
    """SEC M-6 (fix round): the population the contract is actually about
    — a roles/token_env-carrying entry sharing an ARRAY with an eligible
    one. The whole line is re-serialised via json.dumps() when ANY entry
    in it is touched, so operator whitespace/number formatting on the
    UNTOUCHED entry can move (e.g. `1.0` staying `1.0` is not guaranteed
    byte-for-byte against a hand-typed `1.00`) — the claim this test pins
    is SEMANTIC identity (every key/value the untouched entry carried
    survives), which is what two_layer_compare() actually depends on."""
    raw = json.dumps([
        {"url": "http://a:5000"},                                       # eligible
        {"url": "http://b:5000", "roles": ["extract"], "n_ctx": 8192,   # untouched
         "extra_body": {"thinking": {"type": "disabled"}}},
    ])
    img = _image(
        private_ok_explicit={"http://a:5000": False, "http://b:5000": False},
        has_token={"http://a:5000": False, "http://b:5000": False},
    )
    new_entries, touched = m.plan_case_json_usable(img, raw)
    assert touched == ["http://a:5000"]
    round_tripped = json.loads(json.dumps(new_entries))
    by_url = {e["url"]: e for e in round_tripped}
    untouched = by_url["http://b:5000"]
    assert untouched["roles"] == ["extract"]
    assert untouched["n_ctx"] == 8192
    assert untouched["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "private_ok" not in untouched


def test_e2e_mixed_array_untouched_entry_survives_semantically(tmp_path, monkeypatch):
    """End-to-end companion (SEC M-6): the real apply path, real file.
    W4 (§6.4): the touched entry's private_ok addition is a
    boundary-crossing materialisation (the ORIGINAL claim this test pins —
    the array's OTHER entry survives byte-for-semantic-identity regardless
    of the boundary), so this now runs against an aged --preimage."""
    _force_no_unit(monkeypatch)
    original = (
        'EMBEDDER_URL=http://a:8070\nRERANKER_URL=http://a:8071\n'
        'LLM_BACKENDS_JSON=[{"url":"http://a:5000"},'
        '{"url":"http://b:5000","roles":["judge"],"n_ctx":4096}]\n'
    )
    env_path = _write_env(tmp_path, original)
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    preimage_path = _aged_preimage(tmp_path)
    rc = m.do_apply_or_dryrun(preimage_path, True, _NO_UNIT)
    assert rc == m.EXIT_OK
    text = env_path.read_text()
    json_line = next(l for l in text.splitlines() if l.startswith("LLM_BACKENDS_JSON="))
    entries = json.loads(json_line[len("LLM_BACKENDS_JSON="):])
    by_url = {e["url"]: e for e in entries}
    assert by_url["http://a:5000"]["private_ok"] is True
    assert by_url["http://b:5000"] == {"url": "http://b:5000", "roles": ["judge"], "n_ctx": 4096}


def test_plan_case_csv_live_converts_to_json_with_effective_private_ok_true():
    entries = m.plan_case_csv_live(_image(), "http://a:5000@2,http://b:5000")
    assert entries == [
        {"url": "http://a:5000", "weight": 2.0, "private_ok": True},
        {"url": "http://b:5000", "weight": 1.0, "private_ok": True},
    ]


# ─────────────────────────────────────────────────────────────────────────
# probe_backend — the wall-clock deadline itself (QA H3 / SEC M-5 follow-up,
# NEW-1 micro-round: the fix landed with no test proving the deadline is
# what actually bounds a blocked probe).
# ─────────────────────────────────────────────────────────────────────────

def test_probe_backend_wall_clock_deadline_returns_promptly_on_a_blocked_probe(monkeypatch):
    """NEW-1 (micro-round): stubs the opener so `_do_probe`'s
    `opener.open(...)` call blocks far past a tiny deadline (0.05s) —
    proving `probe_backend()` still returns promptly, reporting
    answered=False, because `Thread.join(timeout)` is what actually bounds
    it, not the underlying network call finishing.

    MUTATION-PROVEN: with `t.join(timeout)` reverted to a bare `t.join()`
    (no deadline — the exact regression NEW-1 named: mutating join(timeout)
    to join() kills zero tests without this one), this test FAILS on the
    elapsed-time assertion below (the join blocks for the full ~2s the
    stub sleeps, instead of the 0.05s deadline) — a fast, deterministic
    fail rather than a true hang, because the stub's own sleep is itself
    bounded. Recorded in the fix-round report: reverting the join() call
    alone made this test fail on `assert elapsed < 1.0` (elapsed ~= 2.0s).
    """
    def _slow_open(req, timeout=None):
        time.sleep(2.0)  # far longer than the 0.05s deadline below
        raise AssertionError("must never be reached before the deadline returns")

    class _FakeOpener:
        def open(self, req, timeout=None):
            return _slow_open(req, timeout=timeout)

    monkeypatch.setattr(m.urllib.request, "build_opener", lambda *a, **k: _FakeOpener())

    started = time.monotonic()
    result = m.probe_backend("http://a:5000", timeout=0.05)
    elapsed = time.monotonic() - started

    assert result == {"answered": False, "status": None, "parsed_model_list": False, "n_models": None}
    assert elapsed < 1.0, f"probe_backend did not return within its deadline: {elapsed:.2f}s"


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


def test_build_confirm_question_not_probed_has_distinct_wording_from_did_not_answer():
    """QA MED-4 (fix round): `probe is None` (never probed — a
    non-interactive dry-run preview) and a real probe that got no
    response are DIFFERENT observations. Conflating them stated an
    observation ('did not answer') that was never actually made."""
    q_not_probed = m.build_confirm_question("http://a:5000", None)
    q_no_answer = m.build_confirm_question("http://a:5000", {
        "answered": False, "status": None, "parsed_model_list": False, "n_models": None})
    assert "not probed" in q_not_probed
    assert "non-interactive" in q_not_probed
    assert "did not answer" not in q_not_probed
    assert "did not answer" in q_no_answer
    assert "not probed" not in q_no_answer


def test_e2e_fallback_case_non_interactive_dry_run_says_not_probed_not_did_not_answer(
        tmp_path, monkeypatch, capsys):
    """End-to-end companion: a non-interactive DRY RUN (no TTY, no
    injected reader) must render the 'not probed' wording in its preview
    of the question a real run would ask, never 'did not answer'.

    W4 (§6.4): aged --preimage so the preview path is actually reached
    (same-generation self-capture would short-circuit before it)."""
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "EMBEDDER_URL=http://a:8070\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    preimage_path = _aged_preimage(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    rc = m.do_apply_or_dryrun(preimage_path, False, _NO_UNIT, confirm_reader=None)
    out = capsys.readouterr().out
    assert rc == m.EXIT_OK
    assert "not probed" in out
    assert "did not answer" not in out


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


def test_capture_refuses_a_preplanted_symlink_never_writes_through_it(tmp_path, monkeypatch):
    """SEC M-1 (fix round): a symlink pre-planted at the capture target
    (by another actor, on a standalone operator-chosen path) must never
    be followed and written through — the config image may hold raw
    userinfo URLs by ruled design, so writing through an attacker's
    symlink would hand them an arbitrary file's worth of that content.
    `_open_fresh_secure_file` unlinks the pre-existing entry (removing
    only the LINK, never the target) and creates a fresh regular file at
    the same path with O_EXCL|O_NOFOLLOW."""
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "LLM_BACKENDS=http://a:5000\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))

    elsewhere = tmp_path / "attacker_controlled_elsewhere.json"
    elsewhere.write_text("PRE-EXISTING CONTENT — must never be touched")
    out_path = tmp_path / "pre.json"
    out_path.symlink_to(elsewhere)

    rc = m.do_capture(str(out_path), "hive-mind-gateway-does-not-exist.service")
    assert rc == m.EXIT_OK
    assert not out_path.is_symlink(), "the symlink must be replaced, never written through"
    assert '"capture_schema"' in out_path.read_text()
    assert elsewhere.read_text() == "PRE-EXISTING CONTENT — must never be touched", (
        "the symlink's TARGET was written to — this is the exact hazard M-1 closes")


def test_open_fresh_secure_file_sets_exact_mode_via_fchmod(tmp_path):
    path = tmp_path / "out.json"
    fd = m._open_fresh_secure_file(path, 0o600)
    os.close(fd)
    import stat as _stat
    assert _stat.S_IMODE(path.stat().st_mode) == 0o600


def test_report_never_leaks_a_userinfo_url_verbatim(tmp_path, monkeypatch, capsys):
    """Scrub test (SEC H-3, fix round — REWRITTEN): the ORIGINAL fixture
    (a userinfo URL only in a live CSV line) classified as CASE_CSV_LIVE,
    whose only report line renders a COUNT, never a URL — the assertion
    passed whether or not `_scrub()` did anything at all (mutation-
    confirmed pre-fix: neutering `_scrub` left this test's output
    byte-identical). This version uses LLM_DEFAULT_TARGET carrying the
    userinfo credential with NOTHING else declared — CASE_FALLBACK,
    non-interactive, whose report line at `:1054-1059`-ish DOES render
    the URL, via `_scrub(default_target)`, twice. Positive assertion: the
    SCRUBBED form must actually appear, not just the absence of the raw
    one — and `rc` is asserted (the original never checked it either)."""
    _force_no_unit(monkeypatch)
    secret = "s3cr3t-in-url-must-be-scrubbed"
    env_path = _write_env(
        tmp_path, f"LLM_DEFAULT_TARGET=http://svc:{secret}@a:5000\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    rc = m.do_apply_or_dryrun(None, False, "hive-mind-gateway-does-not-exist.service")
    out = capsys.readouterr().out
    assert rc == m.EXIT_OK, out
    assert secret not in out
    assert "svc:" not in out
    assert "http://a:5000" in out, (
        f"the scrubbed URL never appeared at all — the report line this test "
        f"targets may have moved:\n{out}")


def test_scrub_call_is_load_bearing_mutation_check(tmp_path, monkeypatch, capsys):
    """Mutation check for the test above (SEC H-3's own instruction):
    neutering `_scrub` to the identity function must make
    test_report_never_leaks_a_userinfo_url_verbatim's positive assertion
    fail (the scrubbed form would never appear; the raw one would)."""
    _force_no_unit(monkeypatch)
    secret = "s3cr3t-in-url-must-be-scrubbed"
    env_path = _write_env(
        tmp_path, f"LLM_DEFAULT_TARGET=http://svc:{secret}@a:5000\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setattr(m, "_scrub", lambda text: str(text))  # neuter
    rc = m.do_apply_or_dryrun(None, False, "hive-mind-gateway-does-not-exist.service")
    out = capsys.readouterr().out
    assert rc == m.EXIT_OK, out
    # With _scrub neutered, the secret DOES leak — proving the real _scrub
    # call is what the test above actually depends on.
    assert secret in out, "expected the neutered _scrub to leak the secret (mutation check)"


# ─────────────────────────────────────────────────────────────────────────
# End-to-end: do_capture / do_apply_or_dryrun against a real fixture .env,
# real loader subprocess (this suite's own deps cover it).
# ─────────────────────────────────────────────────────────────────────────

_NO_UNIT = "hive-mind-gateway-does-not-exist-in-this-test.service"


def test_e2e_no_env_file_reports_and_exits_0(tmp_path, monkeypatch, capsys):
    _force_no_unit(monkeypatch)
    monkeypatch.setenv("SECURE_ENV_FILE", str(tmp_path / "does-not-exist.env"))
    rc = m.do_apply_or_dryrun(None, True, _NO_UNIT)
    assert rc == m.EXIT_OK
    # QA MED-8: rc == EXIT_OK alone is returned by ~8 different branches —
    # pin the actual report line this specific path takes.
    out = capsys.readouterr().out
    assert "no shared-memory/.env found" in out
    assert "environment-only" in out


def test_e2e_non_utf8_env_file_refuses_cleanly_never_a_traceback(tmp_path, monkeypatch, capsys):
    """QA LOW (fix round): a non-UTF-8 byte in .env must not traceback out
    of read_raw_lines() — refused cleanly, named, EXIT_STOP. An explicit
    --preimage bypasses self-capture (which fails on the SAME file even
    earlier, inside the loader subprocess's own secure_env.load_split_env()
    — also a clean refusal, just a different message) so this test isolates
    THIS module's own read_raw_lines() guard specifically."""
    _force_no_unit(monkeypatch)
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"LLM_BACKENDS=http://a:5000\n\xff\xfe garbage\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    preimage = tmp_path / "pre.json"
    preimage.write_text(json.dumps({
        "capture_schema": m.CAPTURE_SCHEMA,
        "image": {"urls": ["http://a:5000"], "fallback_reason": None},
    }))
    rc = m.do_apply_or_dryrun(str(preimage), True, _NO_UNIT)
    out = capsys.readouterr().out
    assert rc == m.EXIT_STOP
    assert "REFUSING" in out
    assert "could not be decoded" in out
    assert "Traceback" not in out


def test_e2e_csv_live_dry_run_then_apply_then_second_run_is_a_true_noop(tmp_path, monkeypatch, capsys):
    """W4 (§6.4 V2): a live CSV's private_ok materialisation is a
    boundary-crossing act now — self-capture alone (same generation) would
    correctly no-op (that IS the true-noop half of this test's name, now
    structural rather than incidental). The dry-run PREVIEW and the first
    APPLY exercise the genuine version-jump path via an aged --preimage
    (`_aged_preimage`); the SECOND apply reverts to plain self-capture,
    proving the re-run is a true no-op for its OWN (V2 same-generation)
    reason, not merely "already explicit"."""
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "LLM_BACKENDS=http://localhost:5000\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    preimage_path = _aged_preimage(tmp_path)

    rc = m.do_apply_or_dryrun(preimage_path, False, _NO_UNIT)
    assert rc == m.EXIT_OK
    dry_out = capsys.readouterr().out
    assert "DRY RUN" in dry_out
    assert env_path.read_text() == "LLM_BACKENDS=http://localhost:5000\n", "dry run wrote to the file"

    rc = m.do_apply_or_dryrun(preimage_path, True, _NO_UNIT)
    assert rc == m.EXIT_OK
    applied_text = env_path.read_text()
    assert "LLM_BACKENDS_JSON=" in applied_text
    assert "# migrated to LLM_BACKENDS_JSON by migrate_env.py" in applied_text

    mtime_after_first_apply = env_path.stat().st_mtime
    hash_after_first_apply = applied_text
    time.sleep(0.05)

    # SECOND RUN — self-capture (no --preimage): now the SAME generation as
    # what it just wrote, so V2's gate alone guarantees a true no-op — file
    # hash AND mtime unchanged (M-D10: no planned writes -> no temp+mv at all).
    rc2 = m.do_apply_or_dryrun(None, True, _NO_UNIT)
    assert rc2 == m.EXIT_OK
    assert env_path.stat().st_mtime == mtime_after_first_apply, "mtime moved on a no-op re-run"
    assert env_path.read_text() == hash_after_first_apply, "content moved on a no-op re-run"


def test_e2e_case1_json_usable_adds_private_ok_and_is_idempotent(tmp_path, monkeypatch):
    """W4 (§6.4 V2): adding private_ok to a role-less, credential-less entry
    is a boundary-crossing materialisation now (that entry would otherwise
    correctly stay ineligible under the CURRENT generation) — exercised via
    an aged --preimage. The second, self-capture re-run proves idempotency
    for the SAME reason as the CSV-live case above: same generation, V2
    gate, true no-op."""
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, 'LLM_BACKENDS_JSON=[{"url":"http://a:5000"}]\n')
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    preimage_path = _aged_preimage(tmp_path)

    rc = m.do_apply_or_dryrun(preimage_path, True, _NO_UNIT)
    assert rc == m.EXIT_OK
    text = env_path.read_text()
    assert '"private_ok": true' in text

    mtime = env_path.stat().st_mtime
    rc2 = m.do_apply_or_dryrun(None, True, _NO_UNIT)
    assert rc2 == m.EXIT_OK
    assert env_path.stat().st_mtime == mtime, "already-explicit JSON re-run must not rewrite"


def test_e2e_case1_unparseable_json_leaves_a_live_csv_untouched_report_matches_action(
        tmp_path, monkeypatch):
    """QA MED-3 (fix round): an unparseable LLM_BACKENDS_JSON (an array of
    bare strings) landing in CASE_JSON_USABLE must report 'touching
    nothing' AND ACTUALLY touch nothing — pre-fix, the CSV cleanup block
    ran unconditionally after this branch and commented out the live CSV
    anyway, contradicting the very report line it had just printed.

    QA's own trace: the REAL loader cannot actually produce this state
    today (an array-of-strings LLM_BACKENDS_JSON makes `import
    hive_mind_proxy` itself crash — AttributeError — so self-capture never
    reaches classify() at all; confirmed empirically writing this test).
    This is deliberately exercised via an explicit --preimage instead (a
    fabricated but schema-valid image with a non-empty `urls`), which is
    exactly the 'one loader change away from being real' case QA flagged —
    defensive code in `plan_case_json_usable`/the CSV-skip guard, proven
    with the tool's own supported --preimage mode rather than left
    untested because the live path can't reach it today."""
    _force_no_unit(monkeypatch)
    original = (
        'EMBEDDER_URL=http://a:8070\nRERANKER_URL=http://a:8071\n'
        'LLM_BACKENDS_JSON=["http://a:5000","http://b:5000"]\n'
        'LLM_BACKENDS=http://c:5000\n'
    )
    env_path = _write_env(tmp_path, original)
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))

    preimage_path = tmp_path / "pre.json"
    preimage_path.write_text(json.dumps({
        "capture_schema": m.CAPTURE_SCHEMA,
        "image": {"urls": ["http://a:5000"], "fallback_reason": None},
    }))

    rc = m.do_apply_or_dryrun(str(preimage_path), True, _NO_UNIT)
    assert rc == m.EXIT_OK
    text = env_path.read_text()
    assert 'LLM_BACKENDS=http://c:5000' in text, "the live CSV line must survive verbatim"
    assert "# migrated to LLM_BACKENDS_JSON" not in text, (
        "report said 'touching nothing' but the CSV was commented out anyway")


def test_e2e_case2_unusable_json_with_live_csv_is_byte_identical_no_write(tmp_path, monkeypatch):
    """QA H2 (fix round) — the end-to-end guard for the review chain's top
    invariant ('an existing LLM_BACKENDS_JSON key is NEVER overwritten by
    any branch'), which previously had NO file-level test at all. A
    token_env that never resolves makes the REAL loader compute
    LLM_POOL_FALLBACK_REASON (case 2), with a live CSV sitting right
    there — both must survive verbatim, nothing written at all (file hash
    AND mtime unchanged), and the report names both facts."""
    _force_no_unit(monkeypatch)
    original = (
        'EMBEDDER_URL=http://a:8070\nRERANKER_URL=http://a:8071\n'
        'LLM_BACKENDS_JSON=[{"url":"https://api.example.com/v1",'
        '"token_env":"X_KEY_THAT_NEVER_RESOLVES"}]\n'
        'LLM_BACKENDS=http://c:5000\n'
    )
    env_path = _write_env(tmp_path, original)
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.delenv("X_KEY_THAT_NEVER_RESOLVES", raising=False)

    mtime_before = env_path.stat().st_mtime
    rc = m.do_apply_or_dryrun(None, True, _NO_UNIT)
    assert rc == m.EXIT_OK
    assert env_path.read_text() == original, "case 2 must never write, to either key"
    assert env_path.stat().st_mtime == mtime_before, "mtime moved on a case-2 no-op"


def test_e2e_case2_deleting_the_json_line_from_the_fixture_kills_the_survival_claim(
        tmp_path, monkeypatch):
    """The mutation-kill proof QA H2 asked for, as a live assertion rather
    than a manual exercise: with the SAME live CSV but NO
    LLM_BACKENDS_JSON key at all, the real loader can no longer produce a
    fallback_reason (nothing to parse), classify() lands in CASE_CSV_LIVE
    instead of CASE_JSON_UNUSABLE, and the CSV DOES get converted — proving
    the case-2 test above is actually pinned to the JSON key's presence,
    not merely to 'nothing changes for some other reason'.

    W4 (§6.4): the conversion is boundary-crossing now — aged --preimage."""
    _force_no_unit(monkeypatch)
    mutated = (
        'EMBEDDER_URL=http://a:8070\nRERANKER_URL=http://a:8071\n'
        'LLM_BACKENDS=http://c:5000\n'
    )
    env_path = _write_env(tmp_path, mutated)
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    preimage_path = _aged_preimage(tmp_path)
    rc = m.do_apply_or_dryrun(preimage_path, True, _NO_UNIT)
    assert rc == m.EXIT_OK
    text = env_path.read_text()
    assert "LLM_BACKENDS_JSON=" in text, (
        "deleting the JSON line should make the CSV convert instead of surviving untouched — "
        "if this fails, the case-2 test above is not actually watching the JSON key's presence")


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
    """W4 (§6.4): aged --preimage so the plan_case_json_usable eligibility
    check actually runs (a same-generation run would short-circuit before
    it with a DIFFERENT, also-true message) — this entry is excluded by
    its own token_env regardless of the boundary, so "already fully
    explicit" is still the right wording once the boundary is open."""
    _force_no_unit(monkeypatch)
    original = ('EMBEDDER_URL=http://a:8070\nRERANKER_URL=http://a:8071\n'
                'LLM_BACKENDS_JSON=[{"url":"https://api.example.com/v1","token_env":"X_KEY"}]\n'
                'X_KEY=irrelevant-for-this-test\n')
    env_path = _write_env(tmp_path, original)
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("X_KEY", "some-token-value")
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    preimage_path = _aged_preimage(tmp_path)
    rc = m.do_apply_or_dryrun(preimage_path, True, _NO_UNIT)
    assert rc == m.EXIT_OK
    assert env_path.read_text() == original
    out = capsys.readouterr().out  # QA MED-8: the capsys fixture was never read
    assert "already fully explicit" in out


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
    """W4 (§6.4): the CSV->JSON conversion this test observes is a
    boundary-crossing materialisation now — aged --preimage, same as the
    other case-3/4 mechanics tests above."""
    _force_no_unit(monkeypatch)
    real_target = tmp_path / "real.env"
    real_target.write_text("LLM_BACKENDS=http://a:5000\n")
    link = tmp_path / ".env"
    link.symlink_to(real_target)
    monkeypatch.setenv("SECURE_ENV_FILE", str(link))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    preimage_path = _aged_preimage(tmp_path)
    rc = m.do_apply_or_dryrun(preimage_path, True, _NO_UNIT)
    assert rc == m.EXIT_OK
    assert "LLM_BACKENDS_JSON=" in real_target.read_text()
    assert link.is_symlink(), "the symlink itself must survive (operate on the target, not replace the link)"


@pytest.mark.skipif(os.geteuid() == 0, reason="os.access(W_OK) is always True for root")
def test_e2e_read_only_directory_with_a_planned_write_refuses(tmp_path, monkeypatch, capsys):
    """SEC M-3 (fix round): the precheck targets the PARENT DIRECTORY —
    mkstemp(dir=parent) + os.replace() is a RENAME, which needs write+exec
    permission on the directory, not the file's own inode."""
    _force_no_unit(monkeypatch)
    work_dir = tmp_path / "workdir"
    work_dir.mkdir()
    env_path = work_dir / ".env"
    env_path.write_text("LLM_BACKENDS=http://a:5000\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    work_dir.chmod(0o555)
    try:
        rc = m.do_apply_or_dryrun(None, True, _NO_UNIT)
        assert rc == m.EXIT_STOP
        out = capsys.readouterr().out
        assert "directory" in out
    finally:
        work_dir.chmod(0o755)


def test_e2e_read_only_file_in_a_writable_directory_still_applies(tmp_path, monkeypatch):
    """The MIRROR of the directory test above, and the specific regression
    the M-3 fix corrected: a read-only FILE in an otherwise-writable
    directory must NOT be refused — the write is a rename via a fresh
    temp file in the same directory, which needs directory permission,
    never the target inode's own write bit. The old (file-level) precheck
    would have refused this case; the fix must not.

    W4 (§6.4): the CSV->JSON conversion is a boundary-crossing
    materialisation now — aged --preimage, captured (read-only) BEFORE the
    chmod below."""
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "LLM_BACKENDS=http://a:5000\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    preimage_path = _aged_preimage(tmp_path)
    env_path.chmod(0o444)
    try:
        rc = m.do_apply_or_dryrun(preimage_path, True, _NO_UNIT)
        assert rc == m.EXIT_OK, "a read-only FILE in a writable directory must not refuse"
        assert "LLM_BACKENDS_JSON=" in env_path.read_text()
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
    """W4 (§6.4 V2): SAME generation (self-capture, no --preimage) — the
    whole case-4 body short-circuits to the "nothing to materialise"
    no-op before it even asks whether a human is present. The genuinely
    NEW non-interactive behaviour (boundary crossing + no human = STOP) is
    covered by test_e2e_fallback_case_boundary_crossing_non_interactive_stops
    below."""
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "EMBEDDER_URL=http://a:8070\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    rc = m.do_apply_or_dryrun(None, True, _NO_UNIT, confirm_reader=None)
    assert rc == m.EXIT_OK
    assert "LLM_BACKENDS_JSON=" not in env_path.read_text()
    out = capsys.readouterr().out
    assert "already the CURRENT loader generation" in out


def test_e2e_fallback_case_boundary_crossing_non_interactive_stops(tmp_path, monkeypatch, capsys):
    """Ruling D(a) V1 (§6.4), the NEW invariant this wave adds: a
    version-jump install (aged --preimage — the fallback WAS serving
    role-less traffic under the old default) with no human present must
    STOP (EXIT_STOP), never quietly exit 0 into a gateway that now serves
    nothing. Mutation-checked in HANDOFF.md (M-kill: reverting this
    `return EXIT_STOP` to the old "report and fall through to EXIT_OK"
    behaviour must make this test fail)."""
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "EMBEDDER_URL=http://a:8070\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    preimage_path = _aged_preimage(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    rc = m.do_apply_or_dryrun(preimage_path, True, _NO_UNIT, confirm_reader=None)
    assert rc == m.EXIT_STOP
    assert "LLM_BACKENDS_JSON=" not in env_path.read_text(), "R-A: no write without a human"
    out = capsys.readouterr().out
    assert "no human is present" in out
    assert "--skip-env-migration" in out


def test_e2e_fallback_case_interactive_confirm_yes_writes_and_freezes_default_target(tmp_path, monkeypatch):
    """W4 (§6.4): materialising the fallback is a boundary-crossing act —
    aged --preimage opens it; R-A's interactive confirm is otherwise
    unchanged."""
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "EMBEDDER_URL=http://a:8070\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    preimage_path = _aged_preimage(tmp_path)
    monkeypatch.setattr(m, "probe_backend", lambda url, timeout=3.0: {
        "answered": False, "status": None, "parsed_model_list": False, "n_models": None})
    rc = m.do_apply_or_dryrun(preimage_path, True, _NO_UNIT, confirm_reader=lambda: "y\n")
    assert rc == m.EXIT_OK
    text = env_path.read_text()
    assert "LLM_BACKENDS_JSON=" in text
    assert "LLM_DEFAULT_TARGET" not in text, "R-A: LLM_DEFAULT_TARGET is never written"


def test_e2e_fallback_case_trailing_slash_confirm_yes_does_not_abort(tmp_path, monkeypatch):
    """SEC H-4 (fix round): hive_mind_proxy's fallback substitution
    (`_raw_backends = [(DEFAULT_TARGET, 1.0)]`) never strips a trailing
    slash, while its LLM_BACKENDS_JSON parsing branch always does — a
    bare LLM_DEFAULT_TARGET carrying a trailing slash made a correctly
    CONFIRMED 'y' abort the whole upgrade with POST-IMAGE DIVERGED,
    reproduced live pre-fix. Must now go through clean: rc == EXIT_OK, no
    'DIVERGED' anywhere in the report.

    W4 (§6.4): materialisation is boundary-crossing now — aged --preimage."""
    _force_no_unit(monkeypatch)
    env_path = _write_env(
        tmp_path, "EMBEDDER_URL=http://a:8070\nLLM_DEFAULT_TARGET=http://localhost:5000/\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    preimage_path = _aged_preimage(tmp_path)
    monkeypatch.setattr(m, "probe_backend", lambda url, timeout=3.0: {
        "answered": False, "status": None, "parsed_model_list": False, "n_models": None})
    rc = m.do_apply_or_dryrun(preimage_path, True, _NO_UNIT, confirm_reader=lambda: "y\n")
    assert rc == m.EXIT_OK, "a trailing slash on LLM_DEFAULT_TARGET must not abort a confirmed write"
    text = env_path.read_text()
    assert "LLM_BACKENDS_JSON=" in text


def test_e2e_fallback_case_interactive_confirm_no_writes_nothing(tmp_path, monkeypatch):
    """W4 (§6.4): aged --preimage so the interactive confirm path is
    actually reached (a same-generation run would short-circuit before it,
    which is not what this test is about)."""
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "EMBEDDER_URL=http://a:8070\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    preimage_path = _aged_preimage(tmp_path)
    monkeypatch.setattr(m, "probe_backend", lambda url, timeout=3.0: {
        "answered": False, "status": None, "parsed_model_list": False, "n_models": None})
    rc = m.do_apply_or_dryrun(preimage_path, True, _NO_UNIT, confirm_reader=lambda: "n\n")
    assert rc == m.EXIT_OK
    assert "LLM_BACKENDS_JSON=" not in env_path.read_text()


def test_e2e_fallback_case_empty_default_target_never_probed_never_materialised(tmp_path, monkeypatch):
    """W4 (§6.4): aged --preimage so the probeable-target check is actually
    reached."""
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "EMBEDDER_URL=http://a:8070\nLLM_DEFAULT_TARGET=\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    preimage_path = _aged_preimage(tmp_path)
    probed = []
    monkeypatch.setattr(m, "probe_backend", lambda url, timeout=3.0: probed.append(url) or {
        "answered": True, "status": 200, "parsed_model_list": True, "n_models": 1})
    rc = m.do_apply_or_dryrun(preimage_path, True, _NO_UNIT, confirm_reader=lambda: "y\n")
    assert rc == m.EXIT_OK
    assert probed == [], "an empty effective target must never be probed"
    assert "LLM_BACKENDS_JSON=" not in env_path.read_text()


def test_e2e_unparseable_default_target_with_userinfo_credential_is_scrubbed(
        tmp_path, monkeypatch, capsys):
    """SEC H-2 (fix round), the EXACT reproduction the finding cited:
    `LLM_DEFAULT_TARGET=https://u:<credential>@` — scheme present,
    hostname EMPTY (there is nothing after the '@') — so
    _effective_url_probeable() is False and this takes the 'empty or
    unparseable' branch specifically, which built its report from the raw
    `shown = default_target if default_target else "(empty)"` value with
    NO scrub call — the one URL-rendering branch in the whole file that
    was not routed through _scrub(). Every OTHER branch (including the
    non-interactive CASE_FALLBACK report a sibling test exercises) already
    called _scrub() correctly even pre-fix; this test is the one that
    actually pins THIS line."""
    _force_no_unit(monkeypatch)
    secret = "s3cr3t-userinfo-credential-must-be-scrubbed"
    env_path = _write_env(
        tmp_path, f"EMBEDDER_URL=http://a:8070\nLLM_DEFAULT_TARGET=https://u:{secret}@\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    rc = m.do_apply_or_dryrun(None, True, _NO_UNIT)
    out = capsys.readouterr().out
    assert rc == m.EXIT_OK
    assert secret not in out
    assert "u:" not in out


# ─────────────────────────────────────────────────────────────────────────
# Capture-schema check
# ─────────────────────────────────────────────────────────────────────────

def test_newer_capture_schema_refused_with_remedy(tmp_path, monkeypatch, capsys):
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "LLM_BACKENDS=http://a:5000\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    preimage = tmp_path / "pre.json"
    preimage.write_text(json.dumps({"capture_schema": m.CAPTURE_SCHEMA + 1, "image": {}}))
    rc = m.do_apply_or_dryrun(str(preimage), False, _NO_UNIT)
    assert rc == m.EXIT_STOP
    out = capsys.readouterr().out  # QA MED-8: the remedy text itself, not just rc
    assert "NEWER than this" in out
    assert "self-capture" in out
    assert "--apply" in out


def test_older_capture_schema_accepted(tmp_path, monkeypatch):
    """QA MED-1 (fix round): the ORIGINAL cut of this test wrote
    `capture_schema: CAPTURE_SCHEMA` — the CURRENT value, not an older
    one — so the N-4 forward-compatibility half ('the reader accepts
    capture_schema <= its own') was never actually exercised. Fixed to
    `CAPTURE_SCHEMA - 1`, which is < CAPTURE_SCHEMA and must still be
    accepted."""
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "LLM_BACKENDS=http://a:5000\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    preimage = tmp_path / "pre.json"
    real_image = json.loads(
        json.dumps(_full_image_from_capture(tmp_path, monkeypatch)))
    older_schema = m.CAPTURE_SCHEMA - 1
    preimage.write_text(json.dumps({"capture_schema": older_schema, "image": real_image}))
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


def test_restore_from_backup_raises_restore_error_on_failed_verify(tmp_path, monkeypatch):
    """The red path SEC M-2 / QA MED-5 named: restore_from_backup() itself
    must raise the dedicated RestoreError (never a bare LoaderError
    misnomer) when the post-restore byte-compare fails."""
    fake_home = tmp_path / "fake-home"
    monkeypatch.setenv("HOME", str(fake_home))
    target = tmp_path / ".env"
    target.write_text("ORIGINAL\n")
    backup = m.backup_env_file(target)

    # Simulate an ENOSPC/EIO-class corruption during the restore write
    # itself: patch Path.read_bytes so the POST-restore re-read (the
    # verification step) returns something that does not match what was
    # just written.
    real_read_bytes = m.Path.read_bytes
    calls = {"n": 0}

    def _flaky_read_bytes(self):
        calls["n"] += 1
        if self == target and calls["n"] > 1:
            return b"CORRUPTED-DURING-RESTORE"
        return real_read_bytes(self)

    monkeypatch.setattr(m.Path, "read_bytes", _flaky_read_bytes)
    with pytest.raises(m.RestoreError):
        m.restore_from_backup(target, backup)


def test_restore_or_die_prints_pending_lines_before_a_restore_failure_never_a_traceback(
        tmp_path, monkeypatch, capsys):
    """SEC M-2 / QA MED-5 (fix round) end-to-end: `_restore_or_die()` must
    (1) print the PENDING lines — the divergence diagnosis computed so
    far — even when the restore itself then fails, (2) never let a raw
    exception escape as a traceback, (3) always return EXIT_STOP."""
    fake_home = tmp_path / "fake-home"
    monkeypatch.setenv("HOME", str(fake_home))
    target = tmp_path / ".env"
    target.write_text("ORIGINAL\n")
    backup = m.backup_env_file(target)

    def _boom(target_arg, backup_arg):
        raise m.RestoreError("synthetic restore failure for this test")

    monkeypatch.setattr(m, "restore_from_backup", _boom)
    lines = ["a pending diagnosis line that must survive"]
    result = m._restore_or_die(lines, target, backup, "PRE-EXISTING DIAGNOSIS LINE")
    out = capsys.readouterr().out
    assert result == m.EXIT_STOP
    assert "a pending diagnosis line that must survive" in out
    assert "PRE-EXISTING DIAGNOSIS LINE" in out
    assert "RESTORE FAILED" in out
    assert "Traceback" not in out


_H9_ENDPOINTS = "EMBEDDER_URL=http://a:8070\nRERANKER_URL=http://a:8071\n"


@pytest.mark.parametrize("fixture_text", [
    _H9_ENDPOINTS + 'LLM_BACKENDS_JSON=[{"url":"http://a:5000"}]\n',    # CASE_JSON_USABLE
    _H9_ENDPOINTS + "LLM_BACKENDS=http://a:5000\n",                    # CASE_CSV_LIVE
    _H9_ENDPOINTS,                                                     # CASE_FALLBACK
], ids=["case1_json_usable", "case3_csv_live", "case4_fallback"])
def test_h9_same_generation_reapply_is_a_true_noop_property(tmp_path, monkeypatch, fixture_text):
    """§6.4 obligation 4 / H9 (the whole point of the V2 same-generation
    gate): a post-W4 `--apply` self-capture re-run on EVERY W3 case fixture
    (case 1 JSON-usable, case 3 CSV-live, case 4 fallback) asserts THREE
    things — rc == EXIT_OK, the file is BYTE-IDENTICAL, and NO NEW BACKUP
    FILE appears (glob before/after) — because there is no boundary to
    cross and therefore nothing effective to preserve. Mutation-checked
    against the un-gated tool in HANDOFF.md (M-kill: removing the
    `if not boundary_open` gate on cases 1/3/4 makes this go red for the
    first two fixtures — case 4's non-interactive/no-preimage path already
    happened to no-op pre-gate too, so it alone would not have caught the
    regression; the property is asserted over the WHOLE set for that
    reason)."""
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, fixture_text)
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    home = tmp_path / "fake-home"
    monkeypatch.setenv("HOME", str(home))
    backup_dir = home / ".shared-memory" / "env-backups"
    before = set(backup_dir.glob("*")) if backup_dir.exists() else set()

    rc = m.do_apply_or_dryrun(None, True, _NO_UNIT)

    assert rc == m.EXIT_OK
    assert env_path.read_text() == fixture_text, "same-generation re-run must be byte-identical"
    after = set(backup_dir.glob("*")) if backup_dir.exists() else set()
    assert after == before, f"a same-generation no-op must never create a backup file: {after - before}"


def test_glob_no_stray_temp_file_left_after_a_write(tmp_path, monkeypatch):
    _force_no_unit(monkeypatch)
    env_path = _write_env(tmp_path, "LLM_BACKENDS=http://a:5000\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    rc = m.do_apply_or_dryrun(None, True, _NO_UNIT)
    assert rc == m.EXIT_OK
    leftovers = list(tmp_path.glob(".env.migrate_env.*"))
    assert leftovers == [], f"stray temp file(s) left in-tree: {leftovers}"
