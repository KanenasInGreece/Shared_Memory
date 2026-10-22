"""check_config Phase A reports what the gateway really resolves (fix round, batch B).

Each case sets only the env a scenario needs; every test first clears every key the
embed, slack and bind rows read, so the developer's own environment cannot leak in.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import check_config  # noqa: E402
from framework_defaults import FRAMEWORK_DEFAULTS  # noqa: E402

_CLEARED = (
    "EMBED_MAX_CONTEXT_TOKENS", "EMBED_CHARS_PER_TOKEN", "EMBED_SPECIAL_TOKEN_RESERVE",
    "EMBED_MAX_CHARS", "OVERFLOW_TOKEN_SLACK", "EMBED_SAFETY_FACTOR",
    "PROXY_BIND", "EMBEDDER_URL", "RERANKER_URL",
)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SECURE_ENV_FILE", "")
    for k in _CLEARED:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _row(lines, key):
    prefix = f"  {key:<28} "
    return next(line for line in lines if line.startswith(prefix))


# 1. fd-1: EMBED_MAX_CHARS follows the gateway's derivation, not the table default.

def test_embed_max_chars_derived_from_context(env):
    env.setenv("EMBED_MAX_CONTEXT_TOKENS", "16384")
    v = check_config._verdict("EMBED_MAX_CHARS")
    assert v["effective"] == 49146 and v["state"] == "derived"
    assert "EMBED_CHARS_PER_TOKEN" in v["note"]


def test_embed_max_chars_all_absent_is_derived_default(env):
    v = check_config._verdict("EMBED_MAX_CHARS")
    assert v["effective"] == 24570 and v["state"] == "derived"


# 2. fd-2: PROXY_BIND and encoder URLs as the gateway resolves them.

def test_proxy_bind_blank_is_loopback(env):
    env.setenv("PROXY_BIND", "   ")
    assert check_config._verdict("PROXY_BIND")["effective"] == "127.0.0.1"


def test_proxy_bind_is_stripped(env):
    env.setenv("PROXY_BIND", "  10.0.0.1  ")
    assert check_config._verdict("PROXY_BIND")["effective"] == "10.0.0.1"


@pytest.mark.parametrize("key", ["EMBEDDER_URL", "RERANKER_URL"])
@pytest.mark.parametrize("value", ["   ", "/", "host:8070", "ftp://h"])
def test_encoder_url_that_would_not_start_is_noted(env, key, value):
    env.setenv(key, value)
    assert "would not start" in check_config._verdict(key)["note"]


def test_encoder_url_valid_has_no_note(env):
    env.setenv("EMBEDDER_URL", "http://h:8070/v1/")
    assert check_config._verdict("EMBEDDER_URL")["note"] == ""


# 3. fd-3: an unusable reserve falls back exactly as dream_telemetry does.

@pytest.mark.parametrize("raw", ["-1", "8192", "abc", ""])
def test_invalid_reserve_shows_gateway_fallback(env, raw):
    env.setenv("EMBED_MAX_CONTEXT_TOKENS", "8192")
    env.setenv("EMBED_SPECIAL_TOKEN_RESERVE", raw)
    v = check_config._verdict("EMBED_SPECIAL_TOKEN_RESERVE")
    assert v["effective"] == 2 and "invalid" in v["note"]


@pytest.mark.parametrize("raw", ["4", "4 ", "04"])
def test_accepted_reserve_spellings_have_no_note(env, raw):
    env.setenv("EMBED_MAX_CONTEXT_TOKENS", "8192")
    env.setenv("EMBED_SPECIAL_TOKEN_RESERVE", raw)
    v = check_config._verdict("EMBED_SPECIAL_TOKEN_RESERVE")
    assert v["note"] == "" and v["effective"] == raw


def test_reserve_valid_against_a_larger_context(env):
    env.setenv("EMBED_MAX_CONTEXT_TOKENS", "16384")
    env.setenv("EMBED_SPECIAL_TOKEN_RESERVE", "8192")
    v = check_config._verdict("EMBED_SPECIAL_TOKEN_RESERVE")
    assert v["note"] == "" and int(v["effective"]) == 8192


# 4. Values the gateway cannot parse are named, never printed as if used.

def test_non_number_context_is_noted_on_its_row_only(env):
    env.setenv("EMBED_MAX_CONTEXT_TOKENS", "abc")
    lines, ok = check_config.phase_a_render()
    assert ok
    assert "would not start" in _row(lines, "EMBED_MAX_CONTEXT_TOKENS")
    for key in ("EMBED_CHARS_PER_TOKEN", "EMBED_SPECIAL_TOKEN_RESERVE", "EMBED_MAX_CHARS"):
        assert "would not start" not in _row(lines, key)
    assert not any("Traceback" in line for line in lines)


def test_unlisted_bad_variable_gets_an_extra_line(env):
    env.setenv("EMBED_SAFETY_FACTOR", "abc")
    lines, ok = check_config.phase_a_render()
    assert ok
    assert any(line.strip().startswith("EMBED_SAFETY_FACTOR:") and "would not start" in line
               for line in lines)
    assert "derived" not in _row(lines, "EMBED_MAX_CHARS")


@pytest.mark.parametrize("raw", ["x", ""])
def test_bad_overflow_slack_is_noted(env, raw):
    env.setenv("OVERFLOW_TOKEN_SLACK", raw)
    assert "would not start" in check_config._verdict("OVERFLOW_TOKEN_SLACK")["note"]


# 5. Accepted spellings keep their raw text; a default environment renders as before.

def test_accepted_spellings_keep_raw_text(env):
    env.setenv("EMBED_CHARS_PER_TOKEN", "3")
    env.setenv("EMBED_MAX_CONTEXT_TOKENS", "  8192")
    env.setenv("OVERFLOW_TOKEN_SLACK", " 5 ")
    lines, _ = check_config.phase_a_render()
    assert "-> '3'" in _row(lines, "EMBED_CHARS_PER_TOKEN")
    assert "-> '  8192'" in _row(lines, "EMBED_MAX_CONTEXT_TOKENS")
    assert "-> ' 5 '" in _row(lines, "OVERFLOW_TOKEN_SLACK")


def test_default_environment_rows_match_the_old_renderer(env):
    lines, _ = check_config.phase_a_render()
    for key in check_config.ENV_ROW_ORDER:
        if key == "EMBED_MAX_CHARS":
            continue
        default = FRAMEWORK_DEFAULTS[key]["default"]
        if key in ("EMBEDDER_URL", "RERANKER_URL"):
            default = default.strip().rstrip("/")
        idiom = FRAMEWORK_DEFAULTS[key]["idiom"]
        assert _row(lines, key) == (
            f"  {key:<28} {'inherited default':<20} -> {str(default)!r}  [idiom={idiom}]")


# 6. The shared dream_telemetry module is never reloaded or registered.

def test_phase_a_leaves_the_shared_module_untouched(env):
    import dream_telemetry  # the shared module, loaded by the normal import
    before_value = dream_telemetry.EMBED_MAX_CONTEXT_TOKENS
    env.setenv("EMBED_MAX_CONTEXT_TOKENS", "16384")
    lines, _ = check_config.phase_a_render()
    assert sys.modules.get("dream_telemetry") is dream_telemetry
    assert dream_telemetry.EMBED_MAX_CONTEXT_TOKENS == before_value
    assert "_check_config_dream_telemetry" not in sys.modules
    assert check_config._verdict("EMBED_MAX_CHARS")["effective"] == 49146


def test_a_failing_private_import_leaves_the_shared_module_alone(env):
    import dream_telemetry
    before = dream_telemetry.EMBED_SAFETY_FACTOR
    env.setenv("EMBED_SAFETY_FACTOR", "abc")
    check_config.phase_a_render()
    assert sys.modules.get("dream_telemetry") is dream_telemetry
    assert dream_telemetry.EMBED_SAFETY_FACTOR == before


# 7. A non-finite ratio passes float() but crashes the derived int(); it must be reported.

@pytest.mark.parametrize("raw", ["nan", "inf", "1e309"])
def test_non_finite_chars_per_token_is_noted_on_its_row(env, raw):
    env.setenv("EMBED_CHARS_PER_TOKEN", raw)
    lines, ok = check_config.phase_a_render()
    assert ok
    assert "would not start" in _row(lines, "EMBED_CHARS_PER_TOKEN")


def test_unattributable_import_failure_still_prints_a_line(env):
    env.setenv("EMBED_CHARS_PER_TOKEN", "nan")
    env.setattr(check_config, "_DREAM_TELEMETRY_PARSES", ())
    lines, ok = check_config.phase_a_render()
    assert ok
    assert any(line.strip().startswith("dream_telemetry:") and "would not start" in line
               for line in lines)


# 8. The derived note is wrapped once by the renderer, so it must not start with its own "(".

def test_derived_note_renders_with_single_parentheses(env):
    lines, _ok = check_config.phase_a_render()
    row = _row(lines, "EMBED_MAX_CHARS")
    assert "((" not in row
    assert row.endswith("(from (EMBED_MAX_CONTEXT_TOKENS - EMBED_SPECIAL_TOKEN_RESERVE) * EMBED_CHARS_PER_TOKEN)")


# 9. A harmless non-finite float must not take the blame for the read that really fails.

def test_a_nan_floor_does_not_hide_a_bad_context(env):
    env.setenv("LLM_CEILING_FLOOR", "nan")
    env.setenv("EMBED_MAX_CONTEXT_TOKENS", "abc")
    lines, _ok = check_config.phase_a_render()
    assert "would not start" in _row(lines, "EMBED_MAX_CONTEXT_TOKENS")
    assert not any(line.strip().startswith("LLM_CEILING_FLOOR:") for line in lines)


def test_a_nan_min_tok_s_does_not_hide_a_bad_ratio(env):
    env.setenv("EMBED_MIN_TOK_S", "nan")
    env.setenv("EMBED_CHARS_PER_TOKEN", "nan")
    lines, _ok = check_config.phase_a_render()
    assert "would not start" in _row(lines, "EMBED_CHARS_PER_TOKEN")
    assert not any(line.strip().startswith("EMBED_MIN_TOK_S:") for line in lines)
