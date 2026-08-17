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


# ── Mutation check target ────────────────────────────────────────────────────
# See A5_HANDOFF.md's mutation-check table: making the `if not credentialed:
# return` unconditional (or the whole function a no-op) makes
# test_refuses_to_start_auth_off_with_credentialed_backend fail (SystemExit
# stops being raised).
