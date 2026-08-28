"""S-05 (Required, RULED — decision:1303, Credential_Custody_Plan PR A5):
AGENT_TOKENS unset disables auth AND the in-flight cap AND the audit path
(coordinator.auth_middleware's AUTH_CONFIGURED_AT_STARTUP early-return)
while a configured provider key stays attached to its backend. The gateway
now refuses to START in that combination — hive_mind_proxy.
require_auth_when_provider_keys_configured(), called from main() only.

Reload pattern mirrors tests/test_llm_fault_origin.py: coordinator first
(so AUTH_CONFIGURED_AT_STARTUP reflects the env this test just set), then
hive_mind_proxy (so LLM_BACKEND_TOKENS reflects LLM_BACKENDS_JSON)."""
import importlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def _load(monkeypatch, *, agent_tokens: str = "", backends_json: list | None = None,
          allow_unauth: str | None = None):
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)  # see test_auth.load_coordinator's docstring
    for key in ("AGENT_TOKENS", "LLM_BACKENDS_JSON", "LLM_BACKENDS",
                "ALLOW_UNAUTHENTICATED_PROVIDER_KEYS"):
        monkeypatch.delenv(key, raising=False)
    if agent_tokens:
        monkeypatch.setenv("AGENT_TOKENS", agent_tokens)
    if backends_json is not None:
        monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(backends_json))
    else:
        monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")  # a plain, uncredentialed default
    if allow_unauth is not None:
        monkeypatch.setenv("ALLOW_UNAUTHENTICATED_PROVIDER_KEYS", allow_unauth)

    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    return g


def test_refuses_to_start_auth_off_with_credentialed_backend(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-refusal-test")
    g = _load(monkeypatch, agent_tokens="", backends_json=[
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY"},
    ])
    assert g.AUTH_CONFIGURED_AT_STARTUP is False
    assert g.LLM_BACKEND_TOKENS["https://api.deepseek.com/v1"] == "sk-refusal-test"

    with pytest.raises(SystemExit, match="AGENT_TOKENS is unset"):
        g.require_auth_when_provider_keys_configured()


def test_refusal_names_both_fixes(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-refusal-test")
    g = _load(monkeypatch, agent_tokens="", backends_json=[
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY"},
    ])
    with pytest.raises(SystemExit) as exc_info:
        g.require_auth_when_provider_keys_configured()
    msg = str(exc_info.value)
    assert "AGENT_TOKENS" in msg
    assert "ALLOW_UNAUTHENTICATED_PROVIDER_KEYS" in msg
    assert "sk-refusal-test" not in msg


def test_refusal_message_never_leaks_the_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-must-never-appear-anywhere")
    g = _load(monkeypatch, agent_tokens="", backends_json=[
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY"},
    ])
    with pytest.raises(SystemExit) as exc_info:
        g.require_auth_when_provider_keys_configured()
    assert "sk-must-never-appear-anywhere" not in str(exc_info.value)


def test_allow_unauthenticated_override_permits_start(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-refusal-test")
    g = _load(monkeypatch, agent_tokens="", backends_json=[
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY"},
    ], allow_unauth="1")
    g.require_auth_when_provider_keys_configured()  # must not raise


# ── SEC-A5-02 (PR A5 fix round): the override must be LOUD ──────────────────

def test_override_logs_a_warning_naming_the_exposed_backends(monkeypatch, caplog):
    """MUTATION TARGET: the override used to be a bare `return` -- no log
    line at all. Now it must name the backend(s) it's exposing."""
    import logging
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-refusal-test")
    g = _load(monkeypatch, agent_tokens="", backends_json=[
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY"},
    ], allow_unauth="1")
    with caplog.at_level(logging.WARNING, logger="hive-proxy"):
        g.require_auth_when_provider_keys_configured()
    assert "ALLOW_UNAUTHENTICATED_PROVIDER_KEYS" in caplog.text
    assert "https://api.deepseek.com/v1" in caplog.text
    assert "sk-refusal-test" not in caplog.text


def test_no_warning_when_override_unset_and_no_credentialed_backend(monkeypatch, caplog):
    """Sanity: the warning is conditional on the override actually mattering
    -- an install with no provider keys never logs it."""
    import logging
    g = _load(monkeypatch, agent_tokens="", backends_json=None, allow_unauth="1")
    with caplog.at_level(logging.WARNING, logger="hive-proxy"):
        g.require_auth_when_provider_keys_configured()
    assert "ALLOW_UNAUTHENTICATED_PROVIDER_KEYS" not in caplog.text


def test_override_active_helper_true_only_when_all_three_conditions_hold(monkeypatch):
    """Direct unit coverage of _unauthenticated_provider_keys_override_
    active, the helper shared between the startup warning and the /health
    field (SEC-A5-02)."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-refusal-test")

    # All three: auth off + credentialed backend + override set.
    g = _load(monkeypatch, agent_tokens="", backends_json=[
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY"},
    ], allow_unauth="1")
    assert g._unauthenticated_provider_keys_override_active() is True

    # Auth configured -> False regardless of the override.
    g = _load(monkeypatch, agent_tokens="claude:tok_abc", backends_json=[
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY"},
    ], allow_unauth="1")
    assert g._unauthenticated_provider_keys_override_active() is False

    # No credentialed backend -> False even with the override set.
    g = _load(monkeypatch, agent_tokens="", backends_json=None, allow_unauth="1")
    assert g._unauthenticated_provider_keys_override_active() is False

    # Override not set -> False (this is the refusal path, not the override path).
    g = _load(monkeypatch, agent_tokens="", backends_json=[
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY"},
    ])
    assert g._unauthenticated_provider_keys_override_active() is False


def test_auth_configured_permits_start_regardless_of_provider_keys(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-refusal-test")
    g = _load(monkeypatch, agent_tokens="claude:tok_abc", backends_json=[
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY"},
    ])
    assert g.AUTH_CONFIGURED_AT_STARTUP is True
    g.require_auth_when_provider_keys_configured()  # must not raise


def test_refined_invariant_auth_off_no_provider_keys_still_unaffected(monkeypatch):
    """The original backward-compat population (auth-unset, no remote keys
    at all -- e.g. a bare local llama-server) must behave exactly as
    before: this is a REFINEMENT of that invariant, not a narrowing of it."""
    g = _load(monkeypatch, agent_tokens="", backends_json=None)  # plain LLM_BACKENDS, no token_env
    assert g.AUTH_CONFIGURED_AT_STARTUP is False
    assert all(t is None for t in g.LLM_BACKEND_TOKENS.values())
    g.require_auth_when_provider_keys_configured()  # must not raise


def test_auth_off_credentialed_embedder_reranker_never_trigger_this_gate(monkeypatch):
    """EMBEDDER_URL/RERANKER_URL are fixed local targets with no token_env
    concept at all in this codebase -- confirms the gate is scoped to
    LLM_BACKEND_TOKENS (the only place a provider key ever attaches) and
    doesn't misfire on an install with no LLM_BACKENDS_JSON at all."""
    g = _load(monkeypatch, agent_tokens="", backends_json=None)
    g.require_auth_when_provider_keys_configured()  # must not raise


# ── SEC-A5-02: the /health config field mirrors the override state ──────────

class _HealthProbeResp:
    status = 200


class _HealthProbeCm:
    async def __aenter__(self):
        return _HealthProbeResp()

    async def __aexit__(self, *a):
        return False


class _HealthProbeSession:
    def get(self, url, timeout=None, headers=None):
        return _HealthProbeCm()


def test_health_config_field_present_only_while_override_active(monkeypatch):
    import asyncio
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-refusal-test")
    g = _load(monkeypatch, agent_tokens="", backends_json=[
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY"},
    ], allow_unauth="1")

    class _Proxy:
        session = _HealthProbeSession()

    checks = asyncio.run(g._build_health_checks(_Proxy(), None))
    assert checks["config"]["allow_unauthenticated_provider_keys"] is True


def test_health_config_field_absent_when_auth_configured(monkeypatch):
    """Additive: an install with auth on never carries the field at all
    (not even `false`) -- a monitor that doesn't know it renders exactly
    as before."""
    import asyncio
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-refusal-test")
    g = _load(monkeypatch, agent_tokens="claude:tok_abc", backends_json=[
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY"},
    ])

    class _Proxy:
        session = _HealthProbeSession()

    checks = asyncio.run(g._build_health_checks(_Proxy(), None))
    assert "allow_unauthenticated_provider_keys" not in checks["config"]


def test_health_config_field_absent_when_no_credentialed_backend(monkeypatch):
    import asyncio
    g = _load(monkeypatch, agent_tokens="", backends_json=None, allow_unauth="1")

    class _Proxy:
        session = _HealthProbeSession()

    checks = asyncio.run(g._build_health_checks(_Proxy(), None))
    assert "allow_unauthenticated_provider_keys" not in checks["config"]


# ── Mutation check target ────────────────────────────────────────────────────
# See A5_HANDOFF.md's mutation-check table: making the `if not credentialed:
# return` unconditional (or the whole function a no-op) makes
# test_refuses_to_start_auth_off_with_credentialed_backend fail (SystemExit
# stops being raised). Removing the SEC-A5-02 log.warning call makes
# test_override_logs_a_warning_naming_the_exposed_backends fail. Making
# _unauthenticated_provider_keys_override_active always return False makes
# test_health_config_field_present_only_while_override_active fail.
