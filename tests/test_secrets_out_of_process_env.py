"""Credential_Custody_Plan_2026-08-14, PR A1 — secrets out of the process
environment.

Covers the NEW invariant the plan's 'Invariants' section states and requires
a mutation-checked failing test for:

    No framework process — gateway, coordinator, or any daemon — ever
    exports a secret value into os.environ or passes one into a child
    process environment.

Two places could violate it, and each gets its own assertion so a
regression in either is caught directly rather than only through a
composed symptom:

  1. secure_env.load_split_env() must never put a secret-classified key
     into os.environ (it goes into the in-process store instead).
  2. hive_mind_proxy._daemon_env() must never include a secret-classified
     key in the dict handed to a spawned daemon's exec environment.
     AGENT_TOKEN is the one deliberate, interim exception (SEC-10 says
     delivery moves to a pipe fd / $XDG_RUNTIME_DIR file in PR A2; A1 still
     hands it across via the child env).

Also covers SEC-09 (classification is BOTH a known-name list and a suffix
pattern), the S-15 regression tripwires this PR must not break
(_filter_headers strips Authorization both directions, log_hygiene stays in
use, the 401/auth-disabled paths keep failing closed), and a same-day
security-review fix round (2026-08-14) on the first cut of this PR:

  #1 CRITICAL — get_secret() precedence was inverted (checked the in-process
     store before os.environ). Fixed; regression test below.
  #2 REQUIRED — a token_env name that doesn't match the suffix pattern
     (e.g. OPENROUTER_CREDENTIAL, supplied only via exec env) was dead code
     for classification purposes. Fixed; regression test below.
  #3 REQUIRED — PG_CONN (a DSN embedding the PG password) was config-
     classified. Fixed; regression tests below.
  #4 REQUIRED — daemons must fail LOUDLY, not with a bare fe_sendauth-class
     error, when they resolve no DB credential. Fixed; tests below.
  #5 REQUIRED — EMBED_CHARS_PER_TOKEN (and two advisory-lock KEY config
     knobs found by the same grep) matched the suffix pattern despite being
     ordinary config. Fixed via KNOWN_CONFIG_NAMES; regression test below.
  #6 OPTIONAL (done) — suffix set widened to _SECRET/_KEY/_CREDENTIAL(S),
     matched case-insensitively.
  #7 OPTIONAL (done) — rem_loop.py/consolidation_loop.py read AGENT_TOKEN
     via get_secret(), not os.environ directly.
  #8 NIT (done) — this file's fixtures now also snapshot/restore os.environ,
     since load_split_env() can os.environ.setdefault() config keys that
     monkeypatch never learns to revert.
"""
import importlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import secure_env  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_secure_env_state(monkeypatch):
    """Every test in this file gets fresh secure_env module state — the
    in-process secrets store AND the dynamically-discovered token_env name
    set are both process-lifetime globals that must not leak between tests
    (or bleed in from whatever a prior test file in the same session already
    loaded into secure_env)."""
    monkeypatch.setattr(secure_env, "_secrets", {})
    monkeypatch.setattr(secure_env, "_dynamic_secret_names", set())
    yield


@pytest.fixture(autouse=True)
def _isolated_process_env():
    """Review fix #8: load_split_env() can os.environ.setdefault() config
    keys (EMBEDDER_URL, LLM_BACKENDS_JSON, ...) — monkeypatch only reverts
    variables IT explicitly set, so a setdefault from inside the loader would
    otherwise leak into later tests in the same session. Snapshot and restore
    the whole environment around every test in this file, regardless of what
    monkeypatch itself also does."""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


# ── SEC-09: classification is BOTH a list AND a pattern ─────────────────────

def test_known_secret_names_are_classified_secret():
    for name in ("PG_PASSWORD", "NEO4J_PASSWORD", "TAVILY_API_KEY",
                 "AGENT_TOKENS", "BACKUP_ADMIN_TOKEN", "PG_CONN"):
        assert secure_env.is_secret_key(name), name


def test_suffix_pattern_catches_a_name_not_on_the_explicit_list():
    """A new provider key nobody added to KNOWN_SECRET_NAMES must still be
    caught — that is the entire point of pairing a list with a pattern.
    Review fix #6: the widened, case-insensitive suffix set."""
    for name in ("DEEPSEEK_API_KEY", "XAI_API_KEY", "SOME_NEW_TOKEN",
                 "ANOTHER_SERVICE_PASSWORD", "SOME_SECRET", "openrouter_key",
                 "VENDOR_CREDENTIAL", "VENDOR_CREDENTIALS"):
        assert secure_env.is_secret_key(name), name


def test_ordinary_config_keys_are_not_classified_secret():
    for name in ("EMBEDDER_URL", "LLM_BACKENDS", "LLM_BACKENDS_JSON",
                 "NEO4J_MAX_POOL", "AUDIT_LOG_PATH"):
        assert not secure_env.is_secret_key(name), name


def test_known_config_names_are_not_misclassified_by_the_suffix_pattern():
    """Review fix #5: these three genuinely match a secret suffix
    (EMBED_CHARS_PER_TOKEN ends in _TOKEN; the two advisory-lock names end
    in _KEY) but are ordinary operator config, found by grepping
    .env.example + every script in this process family."""
    for name in ("EMBED_CHARS_PER_TOKEN", "BACKUP_ADVISORY_LOCK_KEY",
                 "NREM_PRIORITY_ADVISORY_LOCK_KEY"):
        assert not secure_env.is_secret_key(name), name


def test_agent_token_singular_is_still_classified_secret_not_allowlisted():
    """AGENT_TOKEN (singular) also matches the _TOKEN suffix, but unlike the
    item-5 names it IS a real secret — it's on KNOWN_CONFIG_NAMES's sibling
    exclusion list only in the sense that it's NOT there. Delivered via a
    daemon's child env as one deliberate, named exception (_daemon_env)."""
    assert secure_env.is_secret_key("AGENT_TOKEN")
    assert "AGENT_TOKEN" not in secure_env.KNOWN_CONFIG_NAMES


def test_token_env_name_referenced_by_llm_backends_json_is_classified_secret():
    """SEC-09's third clause: 'every token_env name from backend config' —
    covers a token_env name that would NOT match the suffix pattern on its
    own (unlike DEEPSEEK_API_KEY, which already matches *_API_KEY)."""
    raw = json.dumps([{"url": "https://x", "token_env": "MY_CUSTOM_SECRET_NAME"}])
    names = secure_env._token_env_names(raw)
    assert names == {"MY_CUSTOM_SECRET_NAME"}


# ── THE INVARIANT, part 1: the loader never exports a secret ────────────────

def _write_env_file(tmp_path, contents: str):
    """Lay out shared-memory/.env under tmp_path and point secure_env's
    __file__ at shared-memory/scripts/secure_env.py inside it, matching the
    real candidate resolution (here.parent.parent / '.env')."""
    (tmp_path / "shared-memory").mkdir()
    (tmp_path / "shared-memory" / ".env").write_text(contents)
    return tmp_path / "shared-memory" / "scripts" / "secure_env.py"


_SECRET_KEYS = (
    "PG_PASSWORD", "NEO4J_PASSWORD", "AGENT_TOKENS", "TAVILY_API_KEY",
    "BACKUP_ADMIN_TOKEN", "DEEPSEEK_API_KEY", "MY_CUSTOM_SECRET_NAME",
    "PG_CONN",
)


def test_load_split_env_never_exports_a_secret_key_to_os_environ(monkeypatch, tmp_path):
    fake_file = _write_env_file(
        tmp_path,
        "PG_PASSWORD=super-secret-pg\n"
        "NEO4J_PASSWORD=super-secret-neo4j\n"
        "AGENT_TOKENS=claude:tok_abc\n"
        "TAVILY_API_KEY=tv-secret\n"
        "BACKUP_ADMIN_TOKEN=backup-secret\n"
        "DEEPSEEK_API_KEY=sk-provider-secret\n"
        "MY_CUSTOM_SECRET_NAME=custom-secret-value\n"
        "PG_CONN=postgresql://postgres:super-secret-pg@localhost:5432/agent_data\n"
        'LLM_BACKENDS_JSON=[{"url":"https://x","token_env":"MY_CUSTOM_SECRET_NAME"}]\n'
        "EMBEDDER_URL=http://localhost:8070\n"
    )
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    for key in (*_SECRET_KEYS, "LLM_BACKENDS_JSON", "EMBEDDER_URL"):
        monkeypatch.delenv(key, raising=False)

    secure_env.load_split_env()

    for key in _SECRET_KEYS:
        assert key not in os.environ, f"{key} leaked into os.environ"
        assert secure_env.get_secret(key) is not None, f"{key} not retrievable via get_secret"

    # config keys still flow to os.environ, unchanged behaviour
    assert os.environ.get("EMBEDDER_URL") == "http://localhost:8070"
    assert os.environ.get("LLM_BACKENDS_JSON")  # the JSON itself names a secret, isn't one


def test_get_secret_falls_back_to_os_environ_for_an_externally_exported_value(monkeypatch):
    """A value the deployer/test supplies via the process's own exec-time
    environment (never through our .env parse) must still be reachable —
    the accessor doesn't silently break that path (SEC-06 names it as an
    anti-pattern for PR A4 to close, not something A1 should regress)."""
    monkeypatch.setenv("PG_PASSWORD", "exported-directly")
    assert secure_env.get_secret("PG_PASSWORD") == "exported-directly"


def test_get_secret_prefers_process_environment_over_the_env_file_store(monkeypatch):
    """Review fix #1, CRITICAL. When the SAME key holds a value in both the
    in-process store (as if load_split_env() had read it from the framework
    .env) and os.environ (an operator export / a test's monkeypatch.setenv),
    the exported value must win — matching load_split_env()'s own setdefault
    semantics ('an operator-exported value always wins') and the accessor's
    own docstring. Getting this backwards is what made os.environ-based test
    setup unreachable on any checkout that also has a real .env (36 failures
    the reviewer reproduced on a checkout with shared-memory/.env present)."""
    secure_env._secrets["PG_PASSWORD"] = "from-the-env-file"
    monkeypatch.setenv("PG_PASSWORD", "exported-by-the-operator")
    assert secure_env.get_secret("PG_PASSWORD") == "exported-by-the-operator"


def test_pg_conn_never_reaches_os_environ_from_the_file(monkeypatch, tmp_path):
    """Review fix #3, focused: PG_CONN specifically, since a DSN embeds the
    Postgres password verbatim — classifying it as config would leak the
    password through a different key name."""
    fake_file = _write_env_file(
        tmp_path,
        "PG_CONN=postgresql://postgres:embedded-secret@localhost:5432/agent_data\n"
    )
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.delenv("PG_CONN", raising=False)

    secure_env.load_split_env()

    assert "PG_CONN" not in os.environ
    assert secure_env.get_secret("PG_CONN") == \
        "postgresql://postgres:embedded-secret@localhost:5432/agent_data"


# ── THE INVARIANT, part 2: _daemon_env() never forwards a secret ────────────

def test_daemon_env_excludes_every_secret_key_except_the_interim_agent_token(monkeypatch):
    monkeypatch.setenv("PG_PASSWORD", "super-secret-pg")
    monkeypatch.setenv("NEO4J_PASSWORD", "super-secret-neo4j")
    monkeypatch.setenv("TAVILY_API_KEY", "tv-secret")
    monkeypatch.setenv("BACKUP_ADMIN_TOKEN", "backup-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-provider-secret")
    monkeypatch.setenv("PG_CONN", "postgresql://postgres:embedded-secret@localhost:5432/agent_data")
    monkeypatch.setenv("AGENT_TOKENS", "consolidation:tok_nrem,rem_daemon:tok_rem")
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)

    import hive_mind_proxy as g
    importlib.reload(g)

    env = g._daemon_env("consolidation")

    for key in ("PG_PASSWORD", "NEO4J_PASSWORD", "TAVILY_API_KEY",
                "BACKUP_ADMIN_TOKEN", "DEEPSEEK_API_KEY", "AGENT_TOKENS", "PG_CONN"):
        assert key not in env, f"{key} leaked into the daemon's child environment"

    leaked_values = {"super-secret-pg", "super-secret-neo4j", "tv-secret",
                      "backup-secret", "sk-provider-secret",
                      "postgresql://postgres:embedded-secret@localhost:5432/agent_data"}
    assert not (leaked_values & set(env.values())), \
        "a secret VALUE (not just its key name) reached the daemon's child environment"

    # the one deliberate, interim exception: this daemon's own AGENT_TOKEN
    assert env.get("AGENT_TOKEN") == "tok_nrem"


def test_daemon_env_still_forwards_non_secret_config(monkeypatch):
    """The split preserves today's behaviour for config — only secrets are
    withheld. A daemon that reads e.g. NEO4J_MAX_POOL from its own inherited
    environment (rather than re-parsing .env) must not lose that."""
    monkeypatch.setenv("NEO4J_MAX_POOL", "77")
    monkeypatch.delenv("AGENT_TOKENS", raising=False)

    import hive_mind_proxy as g
    importlib.reload(g)

    env = g._daemon_env("consolidation")
    assert env.get("NEO4J_MAX_POOL") == "77"


def test_daemon_env_excludes_dynamically_classified_token_env_key(monkeypatch):
    """Review fix #2, REQUIRED. LLM_BACKENDS_JSON is SET (not deleted, unlike
    test_daemon_env_still_forwards_non_secret_config above) and names a
    token_env whose own name matches no suffix — exactly AGENTS.md/
    ops/README.md's documented exec-env provider-key delivery path, so this
    is the mainline case, not an edge one. Before the fix, _daemon_env's
    filter had no way to know this name was a secret and forwarded it (and
    its value) into both daemons' child environments."""
    monkeypatch.setenv("OPENROUTER_CREDENTIAL", "sk-custom-provider-secret")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://openrouter.example/v1", "token_env": "OPENROUTER_CREDENTIAL"},
    ]))
    monkeypatch.delenv("AGENT_TOKENS", raising=False)

    import hive_mind_proxy as g
    importlib.reload(g)

    env = g._daemon_env("consolidation")
    assert "OPENROUTER_CREDENTIAL" not in env
    assert "sk-custom-provider-secret" not in env.values()


# ── Review fix #4: daemons fail LOUDLY when they resolve no DB credential ───

def test_require_db_credentials_fails_loud_with_no_pg_credential():
    with pytest.raises(SystemExit, match="Postgres credential"):
        secure_env.require_db_credentials(
            pg_password="", pg_conn="", neo4j_password="some-neo4j-pw",
            daemon_name="test_daemon",
        )


def test_require_db_credentials_fails_loud_with_no_neo4j_credential():
    with pytest.raises(SystemExit, match="Neo4j credential"):
        secure_env.require_db_credentials(
            pg_password="some-pg-pw", pg_conn="", neo4j_password="",
            daemon_name="test_daemon",
        )


def test_require_db_credentials_passes_with_pg_conn_only():
    """An explicit PG_CONN (a full DSN, password and all) is sufficient even
    with PG_PASSWORD itself empty — no exception."""
    secure_env.require_db_credentials(
        pg_password="", pg_conn="postgresql://postgres:pw@host/db",
        neo4j_password="some-neo4j-pw", daemon_name="test_daemon",
    )


def test_require_db_credentials_passes_with_both_resolved():
    secure_env.require_db_credentials(
        pg_password="pw", pg_conn="", neo4j_password="pw2",
        daemon_name="test_daemon",
    )


def test_rem_loop_startup_check_fails_loud_without_pg_credential(monkeypatch):
    import rem_loop
    monkeypatch.setattr(rem_loop, "_pg_pass", "")
    monkeypatch.setattr(rem_loop, "_pg_conn_explicit", "")
    monkeypatch.setattr(rem_loop, "NEO4J_PASS", "some-neo4j-pw")
    with pytest.raises(SystemExit, match="Postgres credential"):
        rem_loop._require_db_credentials()


def test_rem_loop_startup_check_fails_loud_without_neo4j_credential(monkeypatch):
    import rem_loop
    monkeypatch.setattr(rem_loop, "_pg_pass", "some-pg-pw")
    monkeypatch.setattr(rem_loop, "_pg_conn_explicit", "")
    monkeypatch.setattr(rem_loop, "NEO4J_PASS", "")
    with pytest.raises(SystemExit, match="Neo4j credential"):
        rem_loop._require_db_credentials()


def test_consolidation_loop_startup_check_fails_loud_without_pg_credential(monkeypatch):
    import consolidation_loop as cl
    monkeypatch.setattr(cl, "_pg_pass", "")
    monkeypatch.setattr(cl, "_pg_conn_explicit", "")
    monkeypatch.setattr(cl, "NEO4J_PASS", "some-neo4j-pw")
    with pytest.raises(SystemExit, match="Postgres credential"):
        cl._require_db_credentials()


def test_consolidation_loop_startup_check_fails_loud_without_neo4j_credential(monkeypatch):
    import consolidation_loop as cl
    monkeypatch.setattr(cl, "_pg_pass", "some-pg-pw")
    monkeypatch.setattr(cl, "_pg_conn_explicit", "")
    monkeypatch.setattr(cl, "NEO4J_PASS", "")
    with pytest.raises(SystemExit, match="Neo4j credential"):
        cl._require_db_credentials()


def test_daemons_never_check_credentials_at_bare_import_time():
    """The check must live behind __main__, not run at module import — every
    test in this file (and this whole repo) imports these modules without a
    real DB credential present, relying on the fact that all SQL/Cypher is
    stubbed. If either daemon regressed to an unconditional check, importing
    it here (with no credential fixture) would already have raised
    SystemExit before this test body even runs."""
    import rem_loop          # noqa: F401
    import consolidation_loop  # noqa: F401
    # no exception -> the check did not fire at import time


# ── Review fix #7: daemons read AGENT_TOKEN via get_secret(), not os.environ

def test_rem_loop_agent_token_reachable_via_the_env_file_store(monkeypatch):
    """With fix #1's corrected precedence, an AGENT_TOKEN set only in the
    in-process store (as if load_split_env() had read it from
    shared-memory/.env) must still populate _AGENT_TOKEN — a standalone
    debug run with the token only in .env must not silently 401."""
    monkeypatch.delenv("AGENT_TOKEN", raising=False)
    secure_env._secrets["AGENT_TOKEN"] = "tok_from_file"

    import rem_loop
    importlib.reload(rem_loop)
    try:
        assert rem_loop._AGENT_TOKEN == "tok_from_file"
    finally:
        importlib.reload(rem_loop)  # restore normal module state for later tests


def test_consolidation_loop_agent_token_reachable_via_the_env_file_store(monkeypatch):
    monkeypatch.delenv("AGENT_TOKEN", raising=False)
    secure_env._secrets["AGENT_TOKEN"] = "tok_from_file"

    import consolidation_loop as cl
    importlib.reload(cl)
    try:
        assert cl._AGENT_TOKEN == "tok_from_file"
    finally:
        importlib.reload(cl)  # restore normal module state for later tests


# ── S-15 regression tripwires (adopted as this PR's must-not-break list) ────

def test_filter_headers_still_strips_authorization_inbound(monkeypatch):
    import hive_mind_proxy as g
    importlib.reload(g)
    proxy = g.AsyncHiveMindProxy()
    headers = {"Authorization": "Bearer client-gateway-token", "X-Other": "kept"}
    filtered = proxy._filter_headers(headers)
    assert "Authorization" not in filtered
    assert "authorization" not in {k.lower() for k in filtered}
    assert filtered.get("X-Other") == "kept"


def test_log_hygiene_append_secure_still_importable_and_used():
    """log_hygiene.append_secure must remain the redaction surface REM/NREM
    import — a regression here would mean secrets could reach a log file
    unredacted."""
    import log_hygiene
    assert hasattr(log_hygiene, "append_secure")
    import rem_loop
    assert rem_loop.append_secure is log_hygiene.append_secure


def test_auth_disabled_backward_compat_when_agent_tokens_unset(monkeypatch):
    """AGENT_TOKENS unset -> auth disabled, unchanged by the A1 split."""
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import coordinator
    importlib.reload(coordinator)
    assert coordinator._load_agent_tokens() == {}
