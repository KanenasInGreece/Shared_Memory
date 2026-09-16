"""Tests for check_config.py encoder window Phase A rows and Phase B note (decision:2540).

Verifies:
1. Phase A renders rows for:
   - EMBED_MAX_CONTEXT_TOKENS
   - EMBED_CHARS_PER_TOKEN
   - EMBED_SPECIAL_TOKEN_RESERVE
   - EMBED_MAX_CHARS
   - OVERFLOW_TOKEN_SLACK
2. Phase B renders encoder context window information as a note, never a boot refusal.
   Even if the advertised window is short, exit code remains 0.
"""
import os
import sys
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = REPO_ROOT / "shared-memory" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import check_config
import encoder_window
import framework_defaults


@pytest.fixture(autouse=True)
def clean_encoder_window_cache():
    encoder_window.reset_encoder_window_cache()
    yield
    encoder_window.reset_encoder_window_cache()



def test_phase_a_renders_all_five_encoder_window_rows(monkeypatch):
    monkeypatch.setenv("SECURE_ENV_FILE", "")
    lines, ok = check_config.phase_a_render()
    assert ok, f"phase_a_render failed: {lines}"
    body = "\n".join(lines)

    required_keys = [
        "EMBED_MAX_CONTEXT_TOKENS",
        "EMBED_CHARS_PER_TOKEN",
        "EMBED_SPECIAL_TOKEN_RESERVE",
        "EMBED_MAX_CHARS",
        "OVERFLOW_TOKEN_SLACK",
    ]
    for key in required_keys:
        assert key in body, f"{key} missing from Phase A output"
        assert key in check_config.ENV_ROW_ORDER, f"{key} missing from check_config.ENV_ROW_ORDER"
        assert key in framework_defaults.FRAMEWORK_DEFAULTS, f"{key} missing from FRAMEWORK_DEFAULTS"


def test_phase_b_renders_encoder_window_note_never_refusal(monkeypatch):
    monkeypatch.setenv("SECURE_ENV_FILE", "")
    # Emulate short advertised window in encoder_window cache
    import encoder_window
    encoder_window._window_cache["embedder"] = {
        "advertised_tokens": 512,
        "source": "v1_models",
        "full_payload_ok": False,
    }
    encoder_window._window_cache["reranker"] = {
        "advertised_tokens": 512,
        "source": "v1_models",
        "full_payload_ok": False,
    }

    try:
        lines, exit_code = check_config.phase_b_render()
        body = "\n".join(lines)

        assert "Encoder context window" in body
        assert "required: 8192 tokens" in body
        assert "short of required" in body
        # CRITICAL: Probed window vs required is a note, NOT a startup refusal!
        assert exit_code == 0, f"Expected exit_code 0 (note only), got {exit_code}"
        assert "none — the gateway would boot" in body
    finally:
        encoder_window._window_cache.clear()


def test_overflow_token_slack_default_is_16_across_contract():
    from pathlib import Path
    import encoder_window
    import framework_defaults

    # 1. encoder_window runtime default is 16
    assert encoder_window.OVERFLOW_TOKEN_SLACK == 16

    # 2. framework_defaults.py default is 16
    row = framework_defaults.FRAMEWORK_DEFAULTS["OVERFLOW_TOKEN_SLACK"]
    assert row["default"] == 16, f"framework_defaults OVERFLOW_TOKEN_SLACK default is {row['default']}, expected 16"
    assert "snap" in row["note"].lower()

    # 3. .env.example documents OVERFLOW_TOKEN_SLACK=16
    env_example = (Path(__file__).parent.parent / "shared-memory" / ".env.example").read_text()
    assert "# OVERFLOW_TOKEN_SLACK=16" in env_example
    assert "Default: 16 tokens" in env_example
    assert "slack * chars_per_token" not in env_example
