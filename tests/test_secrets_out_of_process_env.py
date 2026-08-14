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
pattern) and the S-15 regression tripwires this PR must not break:
_filter_headers strips Authorization both directions, log_hygiene stays in
use, and the 401/auth-disabled paths keep failing closed.
"""
import importlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import secure_env  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_secrets_store(monkeypatch):
    """Every test in this file gets a fresh in-process secrets dict — module
    state must not leak between tests (or from whatever a prior test file in
    the same session already loaded into secure_env)."""
    monkeypatch.setattr(secure_env, "_secrets", {})
    yield


# ── SEC-09: classification is BOTH a list AND a pattern ─────────────────────

def test_known_secret_names_are_classified_secret():
    for name in ("PG_PASSWORD", "NEO4J_PASSWORD", "TAVILY_API_KEY",
                 "AGENT_TOKENS", "BACKUP_ADMIN_TOKEN"):
        assert secure_env.is_secret_key(name), name


def test_suffix_pattern_catches_a_name_not_on_the_explicit_list():
    """A new provider key nobody added to KNOWN_SECRET_NAMES must still be
    caught — that is the entire point of pairing a list with a pattern."""
    for name in ("DEEPSEEK_API_KEY", "XAI_API_KEY", "SOME_NEW_TOKEN",
                 "ANOTHER_SERVICE_PASSWORD"):
        assert secure_env.is_secret_key(name), name


def test_ordinary_config_keys_are_not_classified_secret():
    for name in ("EMBEDDER_URL", "LLM_BACKENDS", "LLM_BACKENDS_JSON",
                 "NEO4J_MAX_POOL", "AUDIT_LOG_PATH"):
        assert not secure_env.is_secret_key(name), name


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


# ── THE INVARIANT, part 2: _daemon_env() never forwards a secret ────────────

def test_daemon_env_excludes_every_secret_key_except_the_interim_agent_token(monkeypatch):
    monkeypatch.setenv("PG_PASSWORD", "super-secret-pg")
    monkeypatch.setenv("NEO4J_PASSWORD", "super-secret-neo4j")
    monkeypatch.setenv("TAVILY_API_KEY", "tv-secret")
    monkeypatch.setenv("BACKUP_ADMIN_TOKEN", "backup-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-provider-secret")
    monkeypatch.setenv("AGENT_TOKENS", "consolidation:tok_nrem,rem_daemon:tok_rem")
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)

    import hive_mind_proxy as g
    importlib.reload(g)

    env = g._daemon_env("consolidation")

    for key in ("PG_PASSWORD", "NEO4J_PASSWORD", "TAVILY_API_KEY",
                "BACKUP_ADMIN_TOKEN", "DEEPSEEK_API_KEY", "AGENT_TOKENS"):
        assert key not in env, f"{key} leaked into the daemon's child environment"

    leaked_values = {"super-secret-pg", "super-secret-neo4j", "tv-secret",
                      "backup-secret", "sk-provider-secret"}
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
