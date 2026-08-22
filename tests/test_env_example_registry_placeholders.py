""".env.example — fresh-host finding D20 + its S-05 side-effect check.

D20: .env.example used to ship a LIVE, empty `AGENT_TOKENS=` assignment;
bootstrap_tokens.sh then APPENDED a real one below it, leaving TWO live
AGENT_TOKENS= lines in the resulting .env. It worked only because the
parser happens to take the last one — a parser-dependent arrangement, not
a stable one, and the SAME file is passed to `docker compose --env-file`.
Fix: comment the placeholder out (this file's first test) and have
bootstrap_tokens.sh replace it in place instead of appending (see
tests/test_bootstrap_tokens_registry.py for that half).

The build brief's item 5 required checking, BEFORE commenting the line
out, whether the gateway's S-05 auth-off behaviour (decision:1303) keys on
AGENT_TOKENS being UNSET vs merely PRESENT-BUT-EMPTY — commenting a line
out and leaving it entirely absent are indistinguishable to
secure_env.load_split_env()'s parser (both mean "no live assignment"), but
a present-but-empty line is technically a THIRD parse state, and this
file's second test proves all three collapse to the identical result
through the actual coordinator/secure_env code (not just read off it) —
so the S-05 semantics documented two paragraphs below in .env.example are
unaffected by this change.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import coordinator  # noqa: E402
import secure_env  # noqa: E402

ENV_EXAMPLE = Path(__file__).parent.parent / "shared-memory" / ".env.example"


def test_env_example_ships_no_live_agent_tokens_roles_or_installs_assignment():
    text = ENV_EXAMPLE.read_text()
    for line in text.splitlines():
        stripped = line.strip()
        for key in ("AGENT_TOKENS=", "AGENT_ROLES=", "AGENT_INSTALLS="):
            assert not stripped.startswith(key), (
                f"{ENV_EXAMPLE} ships a LIVE {key} assignment (D20 fresh-host "
                "finding) -- comment it out; bootstrap_tokens.sh writes the "
                "real one."
            )


def _agent_tokens_for(tmp_path, content: str, monkeypatch) -> dict:
    """Point secure_env at a synthetic .env via SECURE_ENV_FILE (the one
    override _select_env_file() honours -- see its docstring and
    tests/conftest.py, which pins SECURE_ENV_FILE="" for hermeticity by
    default), reload it fresh, and return coordinator._load_agent_tokens()'s
    result -- the ACTUAL function the gateway calls at startup, not a
    reimplementation of its parsing."""
    env_file = tmp_path / ".env"
    env_file.write_text(content)
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_file))
    monkeypatch.delenv("AGENT_TOKENS", raising=False)  # no operator export shadowing it
    monkeypatch.setattr(secure_env, "_secrets", {})
    monkeypatch.setattr(secure_env, "_dynamic_secret_names", set())

    secure_env.load_split_env()
    return coordinator._load_agent_tokens()


def test_absent_commented_and_live_empty_agent_tokens_all_resolve_identically(
    tmp_path, monkeypatch,
):
    """S-05 finding (build brief item 5): an absent AGENT_TOKENS= line, a
    commented-out one, and a live-but-empty one must all resolve to the
    SAME empty registry (auth disabled) -- proving .env.example's switch
    from live-empty to commented changes nothing about gateway auth-off
    semantics."""
    no_line_at_all = _agent_tokens_for(tmp_path, "OTHER=1\n", monkeypatch)
    commented = _agent_tokens_for(tmp_path, "OTHER=1\n# AGENT_TOKENS=\n", monkeypatch)
    live_empty = _agent_tokens_for(tmp_path, "OTHER=1\nAGENT_TOKENS=\n", monkeypatch)

    assert no_line_at_all == {}
    assert commented == {}
    assert live_empty == {}
    assert no_line_at_all == commented == live_empty


def test_auth_configured_at_startup_flag_is_false_for_all_three_forms(tmp_path, monkeypatch):
    """The gateway's actual startup gate is AUTH_CONFIGURED_AT_STARTUP
    (coordinator.py: `bool(_AGENT_TOKENS)`, captured once at import) -- this
    pins the same equivalence one level up, at the boolean the S-05 refusal
    path (require_no_plaintext_agent_tokens / the ALLOW_UNAUTHENTICATED_
    PROVIDER_KEYS gate) actually reads."""
    for content in ("OTHER=1\n", "OTHER=1\n# AGENT_TOKENS=\n", "OTHER=1\nAGENT_TOKENS=\n"):
        tokens = _agent_tokens_for(tmp_path, content, monkeypatch)
        assert bool(tokens) is False
