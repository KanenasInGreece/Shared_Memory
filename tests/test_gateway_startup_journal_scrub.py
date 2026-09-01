"""W0 item ② — the startup journal must never render a URL's userinfo/query
unscrubbed.

Before this, the gateway's own startup routing announcement
("### /v1/embeddings->%s | /v1/reranking->%s | default->LLM pool") rendered
EMBEDDER_URL/RERANKER_URL verbatim -- the same class of leak
scrub_url_credentials (log_hygiene.py) already exists to close everywhere
else a URL might carry a credential (userinfo or a `?key=...` query
parameter) into a log file. This test targets the routing line specifically:
the render call is split into a pure helper, `_encoder_routing_log_line()`
(hive_mind_proxy.py), precisely so a test can assert on the exact string
without capturing logging output -- same shape as
tests/test_coordinator_encoder_urls.py's `_reloaded()` pattern, reused here.

M3 (fix round, QA review) adds a test pinning that main()'s actual source
calls _encoder_routing_log_line() -- so a revert cannot leave a passing dead
twin (the helper existing and being individually correct, but main() quietly
reverted to inlining the unscrubbed render again).

M2-partial (fix round, merger ruling) adds ONE parametrized test covering
the rest of the closed scrub set from H3: the four role_config_errors
construction sites (_parse_roles, driven via a real LLM_BACKENDS_JSON reload
-- same reload pattern as tests/test_provider_key_startup_refusal.py) and
the five S-05/M-5/P-5 guard-message renders (two log.warning calls captured
via caplog, three SystemExit messages captured via pytest.raises).
"""
import importlib
import inspect
import json
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def _reloaded(monkeypatch, **env):
    for k in ("EMBEDDER_URL", "RERANKER_URL", "LLM_BACKENDS_JSON", "LLM_BACKENDS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    mod = importlib.import_module("hive_mind_proxy")
    return importlib.reload(mod)


def test_userinfo_absent_scrubbed_form_present(monkeypatch):
    mod = _reloaded(
        monkeypatch,
        EMBEDDER_URL="http://user:s3cr3t-embed-key@embed.example:8070",
        RERANKER_URL="http://user:s3cr3t-rerank-key@rerank.example:8071",
    )
    line = mod._encoder_routing_log_line()
    assert "s3cr3t-embed-key" not in line
    assert "s3cr3t-rerank-key" not in line
    assert "user:" not in line
    assert "http://embed.example:8070" in line
    assert "http://rerank.example:8071" in line


def test_default_urls_render_unchanged(monkeypatch):
    mod = _reloaded(monkeypatch)
    line = mod._encoder_routing_log_line()
    assert "http://localhost:8070" in line
    assert "http://localhost:8071" in line


def test_line_shape_unchanged_apart_from_scrubbing(monkeypatch):
    mod = _reloaded(monkeypatch)
    line = mod._encoder_routing_log_line()
    assert line.startswith("### /v1/embeddings->")
    assert " | /v1/reranking->" in line
    assert line.endswith(" | default->LLM pool")


def test_main_actually_calls_the_helper(monkeypatch):
    """M3 fix round: a helper existing and being individually correct
    proves nothing about whether main() still calls it -- a revert could
    leave _encoder_routing_log_line() defined and tested (passing) while
    main() quietly went back to inlining the unscrubbed two-arg log.info()
    call. Pin the call site itself."""
    mod = _reloaded(monkeypatch)
    source = inspect.getsource(mod.main)
    assert "_encoder_routing_log_line()" in source
    # And the two-arg inline form must be gone from main() specifically --
    # not merely present somewhere else in the (huge) module.
    assert "EMBEDDER_URL, RERANKER_URL" not in source


# ---------------------------------------------------------------------------
# M2-partial (fix round, merger ruling): no raw userinfo secret in the rest
# of the closed scrub set -- the four role_config_errors construction sites
# and the five S-05/M-5/P-5 guard-message renders.
# ---------------------------------------------------------------------------

SECRET = "sm-test-must-never-leak-9f3ac2"
USERINFO_URL = f"https://leakuser:{SECRET}@backend.example.test/v1"


def _reload_with_backends(monkeypatch, *, agent_tokens="", backends_json=None,
                           allow_unauth=None):
    """Same reload pattern as tests/test_provider_key_startup_refusal.py:
    coordinator first (so AUTH_CONFIGURED_AT_STARTUP reflects the env this
    call just set), then hive_mind_proxy (so LLM_BACKEND_TOKENS/_LLM_BACKEND_
    ROLE_CONFIG_ERRORS reflect LLM_BACKENDS_JSON)."""
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    for key in ("AGENT_TOKENS", "LLM_BACKENDS_JSON", "LLM_BACKENDS",
                "ALLOW_UNAUTHENTICATED_PROVIDER_KEYS"):
        monkeypatch.delenv(key, raising=False)
    if agent_tokens:
        monkeypatch.setenv("AGENT_TOKENS", agent_tokens)
    if backends_json is not None:
        monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(backends_json))
    else:
        monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    if allow_unauth is not None:
        monkeypatch.setenv("ALLOW_UNAUTHENTICATED_PROVIDER_KEYS", allow_unauth)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    return g


def _role_config_errors_text(monkeypatch, caplog):
    g = _reload_with_backends(monkeypatch, agent_tokens="claude:tok_abc", backends_json=[
        {"url": USERINFO_URL, "roles": "not-a-list"},
    ])
    assert g._LLM_BACKEND_ROLE_CONFIG_ERRORS, "fixture didn't even reach role_config_errors"
    return " ".join(g._LLM_BACKEND_ROLE_CONFIG_ERRORS)


def _s05_warning_text(monkeypatch, caplog):
    monkeypatch.setenv("SM_TEST_TOKEN_S05_WARN", "resolved-token-value")
    g = _reload_with_backends(monkeypatch, agent_tokens="", backends_json=[
        {"url": USERINFO_URL, "token_env": "SM_TEST_TOKEN_S05_WARN", "private_ok": True},
    ], allow_unauth="1")
    with caplog.at_level(logging.WARNING, logger="hive-proxy"):
        g.require_auth_when_provider_keys_configured()
    return caplog.text


def _s05_systemexit_text(monkeypatch, caplog):
    monkeypatch.setenv("SM_TEST_TOKEN_S05_EXIT", "resolved-token-value")
    g = _reload_with_backends(monkeypatch, agent_tokens="", backends_json=[
        {"url": USERINFO_URL, "token_env": "SM_TEST_TOKEN_S05_EXIT", "private_ok": True},
    ])
    with pytest.raises(SystemExit) as exc_info:
        g.require_auth_when_provider_keys_configured()
    return str(exc_info.value)


def _m5_warning_text(monkeypatch, caplog):
    """M-5′ (W4, decision:1824): a credentialed backend with neither roles
    nor an explicit private_ok is a loud WARNING now, never a SystemExit —
    was _m5_systemexit_text."""
    monkeypatch.setenv("SM_TEST_TOKEN_M5", "resolved-token-value")
    g = _reload_with_backends(monkeypatch, agent_tokens="claude:tok_abc", backends_json=[
        {"url": USERINFO_URL, "token_env": "SM_TEST_TOKEN_M5"},  # neither roles nor private_ok
    ])
    with caplog.at_level(logging.WARNING, logger="hive-proxy"):
        g.require_valid_llm_routing_config()   # must NOT raise
    return caplog.text


def _p5_warning_text(monkeypatch, caplog):
    g = _reload_with_backends(monkeypatch, agent_tokens="", backends_json=[
        {"url": USERINFO_URL, "private_ok": False},  # uncredentialed -- avoids S-05/M-5 entirely
    ], allow_unauth="1")
    with caplog.at_level(logging.WARNING, logger="hive-proxy"):
        g.require_valid_llm_routing_config()
    return caplog.text


def _p5_no_override_warning_text(monkeypatch, caplog):
    """P-5′ (W4, decision:1824): auth-off + an EXPLICIT private_ok=false
    backend is a loud WARNING now, never a SystemExit, and no longer needs
    ALLOW_UNAUTHENTICATED_PROVIDER_KEYS to avoid one — that override branch
    is deleted with the exit it existed to bypass. Was
    _p5_systemexit_text; _p5_warning_text above covers the same predicate
    with the (now-inert) override env var still set, kept for its own
    scrub coverage."""
    g = _reload_with_backends(monkeypatch, agent_tokens="", backends_json=[
        {"url": USERINFO_URL, "private_ok": False},
    ])
    with caplog.at_level(logging.WARNING, logger="hive-proxy"):
        g.require_valid_llm_routing_config()   # must NOT raise
    return caplog.text


_LEAK_SCENARIOS = {
    "role_config_errors": _role_config_errors_text,
    "s05_warning": _s05_warning_text,
    "s05_systemexit": _s05_systemexit_text,
    "m5_warning": _m5_warning_text,
    "p5_warning": _p5_warning_text,
    "p5_no_override_warning": _p5_no_override_warning_text,
}


@pytest.mark.parametrize("scenario", sorted(_LEAK_SCENARIOS))
def test_no_raw_userinfo_secret_in_journal_renders(scenario, monkeypatch, caplog):
    text = _LEAK_SCENARIOS[scenario](monkeypatch, caplog)
    assert SECRET not in text, f"{scenario} leaked the raw secret into its journal render"
    assert "backend.example.test" in text, (
        f"{scenario} never even reached the scrubbed host -- the fixture didn't exercise the target render"
    )


@pytest.fixture(autouse=True)
def _restore_module(monkeypatch):
    """Reloading hive_mind_proxy rebinds the module object process-wide --
    restore a clean-env reload after every test so later test files that
    import it fresh do not inherit this file's env (same convention as
    tests/test_coordinator_encoder_urls.py).

    Handback fix: teardown is LIFO, so this code runs BEFORE monkeypatch's
    own undo -- the M2-partial scenarios' LLM_BACKENDS_JSON/AGENT_TOKENS/
    ALLOW_UNAUTHENTICATED_PROVIDER_KEYS monkeypatches were still live at
    reload time, and coordinator (whose AUTH_CONFIGURED_AT_STARTUP those
    scenarios also mutate) was never reloaded at all here. Clear the env
    ourselves (immediate, not dependent on monkeypatch's later undo) and
    reload coordinator alongside hive_mind_proxy, so a later test can't
    inherit an auth-off coordinator state and pass for the wrong reason."""
    yield
    for k in ("EMBEDDER_URL", "RERANKER_URL", "LLM_BACKENDS_JSON", "LLM_BACKENDS",
              "AGENT_TOKENS", "ALLOW_UNAUTHENTICATED_PROVIDER_KEYS"):
        monkeypatch.delenv(k, raising=False)
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    import coordinator
    importlib.reload(coordinator)
    importlib.reload(importlib.import_module("hive_mind_proxy"))
