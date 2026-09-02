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
     AGENT_TOKEN was PR A1's one deliberate, interim exception; PR A2
     (SEC-10) closed it — AGENT_TOKEN no longer crosses via the child env
     at all, delivery moved to a pipe fd (see test_token_registry_digests_
     and_daemon_fd.py for that PR's own coverage). The test below now
     asserts the ABSENCE this PR's exception used to require.

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
    # The suite-wide conftest pins SECURE_ENV_FILE="" (hermeticity: never
    # read the deployer's live .env). THIS file tests the loader itself
    # against env files it constructs via the faked-__file__ candidate walk,
    # so the pin must come off here — the fake walk IS the subject.
    monkeypatch.delenv("SECURE_ENV_FILE", raising=False)
    monkeypatch.setattr(secure_env, "_secrets", {})
    monkeypatch.setattr(secure_env, "_dynamic_secret_names", set())
    monkeypatch.setattr(secure_env, "_advised_exec_env_names", set())
    monkeypatch.setattr(secure_env, "_advised_ignored_file_pointer_names", set())
    monkeypatch.setattr(secure_env, "_llm_backends_json_parse_failed", False)
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
    exclusion list only in the sense that it's NOT there. As of PR A2 it
    never crosses via a daemon's child env at all (pipe-fd delivery,
    hive_mind_proxy._daemon_env_and_token_fd()) — staying classified secret
    is what keeps it out of os.environ (and _daemon_env()'s output) in the
    first place."""
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

def test_daemon_env_never_includes_agent_token(monkeypatch):
    """PR A2 (SEC-10): the interim exception PR A1 carved out for AGENT_TOKEN
    is now closed — _daemon_env()'s output never carries it, under any
    AGENT_TOKENS configuration. Delivery moved to a pipe fd (see
    hive_mind_proxy._daemon_env_and_token_fd(), covered in
    test_token_registry_digests_and_daemon_fd.py)."""
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
                "BACKUP_ADMIN_TOKEN", "DEEPSEEK_API_KEY", "AGENT_TOKENS", "PG_CONN",
                "AGENT_TOKEN"):
        assert key not in env, f"{key} leaked into the daemon's child environment"

    leaked_values = {"super-secret-pg", "super-secret-neo4j", "tv-secret",
                      "backup-secret", "sk-provider-secret", "tok_nrem", "tok_rem",
                      "postgresql://postgres:embedded-secret@localhost:5432/agent_data"}
    assert not (leaked_values & set(env.values())), \
        "a secret VALUE (not just its key name) reached the daemon's child environment"


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


# ── D.2: case-fold classification, both sides (ADV1-3) ──────────────────────

def test_is_secret_key_case_folds_a_known_secret_name():
    """AGENT_TOKENS does not even match the suffix pattern (it ends in the
    PLURAL '_TOKENS', not '_TOKEN') -- before D.2 a lowercase spelling fell
    through every check and was misclassified as ordinary config."""
    assert secure_env.is_secret_key("agent_tokens")
    assert secure_env.is_secret_key("Agent_Tokens")
    assert secure_env.is_secret_key("AGENT_TOKENS")


def test_is_secret_key_case_folds_a_suffix_matched_name():
    assert secure_env.is_secret_key("openrouter_key")
    assert secure_env.is_secret_key("deepseek_api_key")


def test_is_secret_key_strips_a_leading_bom():
    assert secure_env.is_secret_key("﻿AGENT_TOKENS")
    assert secure_env.is_secret_key("﻿pg_password")


def test_is_secret_key_known_config_name_case_fold_still_excluded():
    """KNOWN_CONFIG_NAMES must keep winning regardless of case -- the
    allowlist is checked first and must not itself regress under the
    normalisation."""
    assert not secure_env.is_secret_key("embed_chars_per_token")
    assert not secure_env.is_secret_key("Embed_Chars_Per_Token")


def test_token_env_names_normalises_before_storing(monkeypatch):
    """A lowercase token_env value must classify secret via
    _dynamic_secret_names regardless of the case is_secret_key() is later
    asked about -- both sides of the ADV1-3 inversion fixed together."""
    monkeypatch.setattr(secure_env, "_dynamic_secret_names", set())
    raw = json.dumps([{"url": "https://x", "token_env": "openrouter_cred"}])
    names = secure_env._token_env_names(raw)
    assert names == {"OPENROUTER_CRED"}
    secure_env._dynamic_secret_names.update(names)
    assert secure_env.is_secret_key("openrouter_cred")
    assert secure_env.is_secret_key("OPENROUTER_CRED")


def test_load_split_env_classifies_lowercase_agent_tokens_line_as_secret(monkeypatch, tmp_path):
    """The exact ADV1-3 scenario: a lowercase agent_tokens= line must never
    reach os.environ, and must remain reachable via get_secret("AGENT_TOKENS")
    -- 'auth silently turns off on that install' is the failure this pins."""
    fake_file = _write_env_file(tmp_path, "agent_tokens=claude:tok_abc\n")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    monkeypatch.delenv("agent_tokens", raising=False)

    secure_env.load_split_env()

    assert "agent_tokens" not in os.environ
    assert "AGENT_TOKENS" not in os.environ
    assert secure_env.get_secret("AGENT_TOKENS") == "claude:tok_abc"


def test_get_secret_canonical_fallback_reaches_a_canonically_stored_key_via_a_lowercase_lookup(monkeypatch):
    """Direct unit test of get_secret()'s own fallback, independent of the
    loader. load_split_env() always stores under the CANONICAL (upper-cased)
    key (see its own storage-key-canonicalisation fix) -- this is what lets
    a caller that looks a value up via a non-canonical spelling (a dynamic
    token_env name used exactly as declared, lowercase and all) still reach
    it."""
    # os.environ is checked BEFORE the in-process store (by design — an
    # operator export always wins) at BOTH the exact and the canonical step,
    # so a real PG_PASSWORD left in this session's os.environ by an
    # unrelated test (test_env_loading.py's verifiers set it directly,
    # bypassing monkeypatch's auto-revert) would shadow the value this test
    # means to exercise. Clear both spellings explicitly rather than relying
    # on suite ordering.
    monkeypatch.delenv("PG_PASSWORD", raising=False)
    monkeypatch.delenv("pg_password", raising=False)
    secure_env._secrets["PG_PASSWORD"] = "from-canonical-key"
    assert secure_env.get_secret("pg_password") == "from-canonical-key"


def test_get_secret_exact_match_still_wins_over_canonical_fallback(monkeypatch):
    """The canonical fallback must never shadow an exact match -- an exact
    hit (however it got there) is returned first, unchanged from before."""
    monkeypatch.delenv("PG_PASSWORD", raising=False)
    monkeypatch.delenv("pg_password", raising=False)
    secure_env._secrets["PG_PASSWORD"] = "exact"
    secure_env._secrets["pg_password"] = "canonical-collision"
    assert secure_env.get_secret("PG_PASSWORD") == "exact"


def test_get_secret_operator_environment_always_beats_the_file_store(monkeypatch):
    """Fix round F3 (SEC1 HIGH-1): the whole lookup order is os.environ
    EXACT -> os.environ CANONICAL -> _secrets EXACT -> _secrets CANONICAL.
    Prove-failing-first: on unmodified code, `_secrets` (exact) was checked
    BEFORE the canonical form of os.environ, so a case-VARIANT operator
    export lost to a value the file store resolved under the exact name a
    caller happens to use -- probed exactly as SEC1 measured it:
    _secrets["AGENT_TOKENS"]="from-file" + os.environ["agent_tokens"]=
    "from-operator" must still return "from-operator", never "from-file"."""
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    monkeypatch.delenv("agent_tokens", raising=False)
    secure_env._secrets["AGENT_TOKENS"] = "from-file"
    monkeypatch.setenv("agent_tokens", "from-operator")
    assert secure_env.get_secret("AGENT_TOKENS") == "from-operator"


def test_load_split_env_dynamic_token_env_still_reachable_under_its_own_case(monkeypatch, tmp_path):
    """A dynamically-discovered token_env name, looked up by
    _load_llm_backends() using ITS OWN raw-case spelling (get_secret(token_env)),
    must still resolve even though storage now canonicalises -- the
    canonical fallback in get_secret() is what keeps this reachable."""
    fake_file = _write_env_file(
        tmp_path,
        "openrouter_cred=sk-provider-secret\n"
        'LLM_BACKENDS_JSON=[{"url":"https://x","token_env":"openrouter_cred"}]\n'
    )
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    for key in ("openrouter_cred", "OPENROUTER_CRED", "LLM_BACKENDS_JSON"):
        monkeypatch.delenv(key, raising=False)

    secure_env.load_split_env()

    assert "openrouter_cred" not in os.environ
    assert "OPENROUTER_CRED" not in os.environ
    assert secure_env.get_secret("openrouter_cred") == "sk-provider-secret"


# ── D.1: malformed LLM_BACKENDS_JSON refusal placement (ADV1-2) ─────────────

def test_token_env_names_flags_invalid_json_syntax():
    assert secure_env._token_env_names("{not json") == set()
    assert secure_env.llm_backends_json_parse_failed() is True


def test_token_env_names_flags_valid_json_that_is_not_an_array():
    assert secure_env._token_env_names('{"url": "https://x"}') == set()
    assert secure_env.llm_backends_json_parse_failed() is True


def test_token_env_names_clean_array_does_not_flag():
    secure_env._llm_backends_json_parse_failed = True  # start dirty
    secure_env._token_env_names(json.dumps([{"url": "https://x"}]))
    assert secure_env.llm_backends_json_parse_failed() is False


def test_token_env_names_absent_raw_json_does_not_flag():
    secure_env._llm_backends_json_parse_failed = True  # start dirty
    secure_env._token_env_names("")
    assert secure_env.llm_backends_json_parse_failed() is False


def test_require_llm_backends_json_parses_raises_when_flagged(monkeypatch):
    monkeypatch.setattr(secure_env, "_llm_backends_json_parse_failed", True)
    with pytest.raises(SystemExit, match="LLM_BACKENDS_JSON"):
        secure_env.require_llm_backends_json_parses("test_daemon")


def test_require_llm_backends_json_parses_silent_when_clean(monkeypatch):
    monkeypatch.setattr(secure_env, "_llm_backends_json_parse_failed", False)
    secure_env.require_llm_backends_json_parses("test_daemon")  # no raise


def test_malformed_llm_backends_json_reaching_daemon_env_would_leak_a_key_the_refusal_now_prevents(monkeypatch):
    """Prove-failing-first evidence for D.1, composed: a malformed
    LLM_BACKENDS_JSON means _token_env_names() can discover NOTHING (by
    design -- it must never raise), so a provider key whose name doesn't
    happen to match the suffix pattern (OPENROUTER_CRED does not: it ends
    in 'CRED', not '_CREDENTIAL') stays classified as ordinary config and
    DOES reach _daemon_env()'s copy set -- exactly the fail-open D.1's
    startup refusal exists to prevent an operator from ever reaching in a
    real boot (require_llm_backends_json_parses() raises first, in main()/
    the daemon __main__ guard, before _daemon_env() is ever called)."""
    monkeypatch.setenv("OPENROUTER_CRED", "sk-should-never-leak")
    monkeypatch.setenv("LLM_BACKENDS_JSON", "{not json")
    monkeypatch.delenv("AGENT_TOKENS", raising=False)

    import hive_mind_proxy as g
    importlib.reload(g)

    # The classification gap still exists at the _daemon_env() level (D.1
    # does not change is_secret_key's suffix pattern) -- this is exactly
    # why the fatal refusal below is the fix, not a change to
    # _daemon_env() itself.
    env = g._daemon_env("consolidation")
    assert env.get("OPENROUTER_CRED") == "sk-should-never-leak"

    # But the real boot path now refuses before ever reaching that call.
    assert secure_env.llm_backends_json_parse_failed() is True
    with pytest.raises(SystemExit, match="LLM_BACKENDS_JSON"):
        secure_env.require_llm_backends_json_parses("hive_mind_proxy")


def test_hive_mind_proxy_imports_cleanly_with_malformed_llm_backends_json(monkeypatch):
    """Import must stay clean regardless of env (invariant 6) -- the
    refusal lives in main() only."""
    monkeypatch.setenv("LLM_BACKENDS_JSON", "{not json")
    import hive_mind_proxy as g
    importlib.reload(g)  # no exception


def test_daemons_import_cleanly_with_malformed_llm_backends_json(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", "{not json")
    import rem_loop
    import consolidation_loop
    importlib.reload(rem_loop)
    importlib.reload(consolidation_loop)  # no exception


def test_rem_loop_startup_check_fails_loud_with_malformed_llm_backends_json(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", "{not json")
    import rem_loop
    importlib.reload(rem_loop)
    with pytest.raises(SystemExit, match="LLM_BACKENDS_JSON"):
        secure_env.require_llm_backends_json_parses("rem_loop")


def test_consolidation_loop_startup_check_fails_loud_with_malformed_llm_backends_json(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", "{not json")
    import consolidation_loop as cl
    importlib.reload(cl)
    with pytest.raises(SystemExit, match="LLM_BACKENDS_JSON"):
        secure_env.require_llm_backends_json_parses("consolidation_loop")


# ── Fix round F4 (SEC1 HIGH-2): LLM_BACKENDS_JSON config-key read must SEE
#    a case-variant / BOM-prefixed spelling, not treat it as absent ─────────

def test_load_split_env_sees_lowercase_llm_backends_json_key(monkeypatch, tmp_path):
    """Prove-failing-first: on unmodified code, `file_values.get(
    "LLM_BACKENDS_JSON")` is exact-case, so a lowercase
    `llm_backends_json=` line reads as ABSENT (parse_failed stays False,
    not True) rather than malformed — bypassing D.1's refusal entirely and
    letting the token_env-named provider key it declares reach os.environ
    unclassified. Probed exactly per SEC1 finding 2."""
    fake_file = _write_env_file(
        tmp_path,
        'llm_backends_json=[{"url":"https://x","token_env":"OPENROUTER_CRED"}]\n'
        "OPENROUTER_CRED=sk-should-be-secret\n"
    )
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    for key in ("llm_backends_json", "LLM_BACKENDS_JSON", "OPENROUTER_CRED", "openrouter_cred"):
        monkeypatch.delenv(key, raising=False)

    secure_env.load_split_env()

    assert secure_env.is_secret_key("OPENROUTER_CRED")
    assert "OPENROUTER_CRED" not in os.environ
    assert "openrouter_cred" not in os.environ
    assert secure_env.get_secret("OPENROUTER_CRED") == "sk-should-be-secret"


def test_load_split_env_sees_bom_prefixed_llm_backends_json_key(monkeypatch, tmp_path):
    """Same defect, BOM-prefixed spelling of the correct case (the BOM
    lands on the file's FIRST key when saved as UTF-8-with-BOM)."""
    fake_file = _write_env_file(
        tmp_path,
        '﻿LLM_BACKENDS_JSON=[{"url":"https://x","token_env":"OPENROUTER_CRED"}]\n'
        "OPENROUTER_CRED=sk-should-be-secret\n"
    )
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    for key in ("LLM_BACKENDS_JSON", "OPENROUTER_CRED"):
        monkeypatch.delenv(key, raising=False)

    secure_env.load_split_env()

    assert secure_env.is_secret_key("OPENROUTER_CRED")
    assert "OPENROUTER_CRED" not in os.environ


# ── Fix round F5 (SEC1 HIGH-3 + MED-5): "export KEY=" is stripped at parse
#    time, case-insensitively, before classification ─────────────────────

def test_load_split_env_strips_lowercase_export_prefix(monkeypatch, tmp_path):
    """Prove-failing-first: on unmodified code, the stored key is the
    literal string "export AGENT_TOKENS" — not in KNOWN_SECRET_NAMES
    (exact-match) and not matching the "_TOKEN" suffix (it ends in
    "TOKENS" with "export " still attached) — so this line's registry was
    exported straight into os.environ."""
    fake_file = _write_env_file(tmp_path, "export AGENT_TOKENS=claude:tok_via_export\n")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    for key in ("AGENT_TOKENS", "export AGENT_TOKENS"):
        monkeypatch.delenv(key, raising=False)

    secure_env.load_split_env()

    assert "export AGENT_TOKENS" not in os.environ
    assert "AGENT_TOKENS" not in os.environ
    assert secure_env.get_secret("AGENT_TOKENS") == "claude:tok_via_export"


def test_load_split_env_strips_uppercase_export_prefix(monkeypatch, tmp_path):
    fake_file = _write_env_file(tmp_path, "EXPORT AGENT_TOKENS=claude:tok_via_EXPORT\n")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    for key in ("AGENT_TOKENS", "EXPORT AGENT_TOKENS"):
        monkeypatch.delenv(key, raising=False)

    secure_env.load_split_env()

    assert "EXPORT AGENT_TOKENS" not in os.environ
    assert "AGENT_TOKENS" not in os.environ
    assert secure_env.get_secret("AGENT_TOKENS") == "claude:tok_via_EXPORT"


# ── Fix round F6 (SEC1 HIGH-4): file_values collapses to canonical keys
#    ONCE, deterministically LAST-DEFINITION-WINS ──────────────────────────

def test_load_split_env_collapse_last_definition_wins_stale_first(monkeypatch, tmp_path):
    """Prove-failing-first: on unmodified code, which spelling wins the
    storage slot depended on SET ITERATION ORDER (candidate_secret_keys is
    a set), not file order — probed by SEC1 as non-deterministic and, in
    this exact ordering, landing on the STALE (first) line instead of the
    corrected (second, later) one."""
    fake_file = _write_env_file(tmp_path, "agent_tokens=stale-first\nAGENT_TOKENS=current-second\n")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    for key in ("agent_tokens", "AGENT_TOKENS"):
        monkeypatch.delenv(key, raising=False)

    secure_env.load_split_env()

    assert secure_env.get_secret("AGENT_TOKENS") == "current-second"


def test_load_split_env_collapse_last_definition_wins_reversed_order(monkeypatch, tmp_path):
    """The other ordering from the report — deterministic means the FILE's
    own last line always wins, regardless of which spelling looks
    "newer" by name; this pins the direction, not a value judgement."""
    fake_file = _write_env_file(tmp_path, "AGENT_TOKENS=current-first\nagent_tokens=stale-second\n")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    for key in ("agent_tokens", "AGENT_TOKENS"):
        monkeypatch.delenv(key, raising=False)

    secure_env.load_split_env()

    assert secure_env.get_secret("AGENT_TOKENS") == "stale-second"


# ── Fix round F11 (SEC1 MED-7 + LOW-8): one shared normaliser, robust to
#    BOM+whitespace in EITHER interleaving order ───────────────────────────

def test_is_secret_key_normalizer_handles_bom_then_space():
    """Prove-failing-first: a single strip()-then-lstrip(BOM) pass leaves a
    stray leading space when the BOM precedes it — probed by SEC1 as
    classifying secret on one side (client order) and config on the other
    (server's pre-fix order)."""
    assert secure_env.is_secret_key("﻿ AGENT_TOKENS")


def test_is_secret_key_normalizer_handles_space_then_bom():
    """The other ordering from the report — the reverse fixed order alone
    fails THIS one instead."""
    assert secure_env.is_secret_key(" ﻿AGENT_TOKENS")


# ── Fix round F12 (SEC1 LOW-9): case-fold the <K>_FILE pointer suffix ──────

def test_load_split_env_resolves_lowercase_file_pointer_suffix(monkeypatch, tmp_path):
    """Prove-failing-first: on unmodified code the exact-case `name.endswith
    ("_FILE")` check never matches a lowercase `agent_tokens_file=` line's
    actual suffix ("_file") — the pointer is silently ignored (no
    candidate derived, no warning either, since that branch is also
    suffix-gated) and get_secret("AGENT_TOKENS") stays None even with the
    file present, readable and correctly formatted."""
    secret_file = tmp_path / "agent_tokens_secret"
    secret_file.write_text("claude:tok_from_lowercase_pointer")
    fake_file = _write_env_file(tmp_path, f"agent_tokens_file={secret_file}\n")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    for key in ("AGENT_TOKENS", "agent_tokens", "AGENT_TOKENS_FILE", "agent_tokens_file"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    secure_env.load_split_env()

    assert secure_env.get_secret("AGENT_TOKENS") == "claude:tok_from_lowercase_pointer"
    # The _FILE pointer's own VALUE (a path) is ordinary config, not itself
    # sensitive — only the derived SECRET must stay out of os.environ.
    assert "AGENT_TOKENS" not in os.environ
    assert "agent_tokens" not in os.environ


def test_derive_file_pointer_candidates_case_folds_an_os_environ_pointer_name(monkeypatch, tmp_path):
    """F12's remaining exposure once F6 already canonicalises FILE-sourced
    keys: an OS.ENVIRON pointer (never touched by the .env-file collapse)
    spelled lowercase must still be detected as a `<K>_FILE` pointer."""
    secret_file = tmp_path / "agent_tokens_secret_env"
    secret_file.write_text("claude:tok_from_lowercase_environ_pointer")
    monkeypatch.setenv("agent_tokens_file", str(secret_file))
    fake_file = _write_env_file(tmp_path, "")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    for key in ("AGENT_TOKENS", "AGENT_TOKENS_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    candidates = secure_env._derive_file_pointer_candidates({})
    assert "AGENT_TOKENS" in candidates
