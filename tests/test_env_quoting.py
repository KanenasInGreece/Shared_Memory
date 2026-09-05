"""S16g (HYG round, R-G'): the SAME balanced-pair quote rule, independently
duplicated in three parsers that must never import from one another (Group
1: the client/server surface split) — the gateway parser (secure_env.py),
the CLI client's parser (memory_bridge.py's `_read_env_file`), and the MCP
client's parser (mcp/vector-skill.py's `_load_env_manually`).

Rule under test: a value wrapped in ONE balanced pair of surrounding quotes
— `"v"` or `'v'` — has that pair stripped. Everything else is kept
VERBATIM: `K=v`, `K=`, `K="unbalanced` (kept as-is, quote and all), a `"`
embedded inside the value with no surrounding pair (kept).

Each parser is exercised through its own file-path knob against a `tmp_path`
file — never a real `.env` (fact:1499 / this round's common rules) — using
plain, non-secret-classified key names so the value lands somewhere directly
inspectable (os.environ) rather than behind secret-resolution machinery this
file has no need to re-test.

The CLI (memory_bridge.py) and the MCP client (mcp/vector-skill.py) are both
loaded FRESH per case via importlib-by-path (the same idiom
tests/test_client_secret_mirror_parity.py already uses for vector-skill.py's
dashed filename) rather than `import`/`reload` of the single shared module
object every other test file in this suite also imports — this keeps every
case isolated and leaves no mutated module state for a test that runs later
in the same session to trip over.
"""
import importlib.util
import os

import pytest

_HERE = os.path.dirname(__file__)
_MEMORY_BRIDGE_PATH = os.path.join(_HERE, "..", "shared-memory", "scripts", "memory_bridge.py")
_VECTOR_SKILL_PATH = os.path.join(_HERE, "..", "mcp", "vector-skill.py")

import sys  # noqa: E402
sys.path.insert(0, os.path.join(_HERE, "..", "shared-memory", "scripts"))
import secure_env  # noqa: E402

_load_counter = 0


def _fresh_module(path: str):
    """A brand-new module object each call, never registered in
    sys.modules, so repeated loads with a different env-path knob never
    collide and never leak into any other test file's own import of the
    same module by name."""
    global _load_counter
    _load_counter += 1
    spec = importlib.util.spec_from_file_location(
        f"_env_quoting_probe_{_load_counter}", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The corpus every parser is checked against. `K=v` / `K=` are the baseline
# (nothing to strip); `K_UNBALANCED` and `K_INNER` prove the negative (kept
# VERBATIM); `K_DQUOTE`/`K_SQUOTE` are the positive (one balanced pair, of
# each quote character, stripped).
_ENV_FILE_BODY = (
    "K_PLAIN=hello\n"
    "K_EMPTY=\n"
    'K_UNBALANCED="unbalanced\n'
    'K_DQUOTE="v with spaces"\n'
    "K_SQUOTE='v with spaces'\n"
    'K_INNER=a"b\n'
)

_EXPECTED = {
    "K_PLAIN": "hello",
    "K_EMPTY": "",
    "K_UNBALANCED": '"unbalanced',
    "K_DQUOTE": "v with spaces",
    "K_SQUOTE": "v with spaces",
    "K_INNER": 'a"b',
}


def _write_env_file(tmp_path) -> str:
    env_file = tmp_path / "probe.env"
    env_file.write_text(_ENV_FILE_BODY)
    return str(env_file)


# ── secure_env.py — the gateway parser ───────────────────────────────────────

def test_secure_env_strips_balanced_quote_pairs(tmp_path, monkeypatch, capsys):
    env_path = _write_env_file(tmp_path)
    for key in _EXPECTED:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SECURE_ENV_FILE", env_path)
    try:
        secure_env.load_split_env()
        for key, expected in _EXPECTED.items():
            assert os.environ.get(key) == expected, (
                f"secure_env.py: {key} got {os.environ.get(key)!r}, "
                f"expected {expected!r}"
            )
    finally:
        for key in _EXPECTED:
            os.environ.pop(key, None)


def test_secure_env_warns_by_key_name_only_when_a_pair_was_stripped(
    tmp_path, monkeypatch, capsys
):
    """The warning names the KEY, never the value — and fires only for the
    two cases that actually had a pair stripped."""
    env_path = _write_env_file(tmp_path)
    for key in _EXPECTED:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SECURE_ENV_FILE", env_path)
    try:
        secure_env.load_split_env()
        err = capsys.readouterr().err
        assert "K_DQUOTE" in err
        assert "K_SQUOTE" in err
        assert "K_PLAIN" not in err
        assert "K_EMPTY" not in err
        assert "K_UNBALANCED" not in err
        assert "K_INNER" not in err
        assert "v with spaces" not in err, "the warning must never carry the value"
    finally:
        for key in _EXPECTED:
            os.environ.pop(key, None)


# Each client has ONE parser, and it is the parser these cases exercise —
# nothing is poisoned, nothing is forced. python-dotenv is installed in this
# suite (fastmcp depends on it), so a client that reached for it would be
# taken at its word here, and the pins below would read that library's
# behaviour instead.

# ── memory_bridge.py — the CLI client's parser ──────────────────────────────

def test_cli_manual_parser_strips_balanced_quote_pairs(tmp_path, monkeypatch):
    # Both `module.os.environ` and this process's `os.environ` are the SAME
    # dict (the `os` module is a process-wide singleton) — the module under
    # probe exports into it as a side effect of being loaded, so it must be
    # cleaned up explicitly, the same as the secure_env.py cases above.
    env_path = _write_env_file(tmp_path)
    for key in _EXPECTED:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SECURE_ENV_FILE", env_path)
    try:
        module = _fresh_module(_MEMORY_BRIDGE_PATH)
        for key, expected in _EXPECTED.items():
            assert module.os.environ.get(key) == expected, (
                f"memory_bridge.py: {key} got "
                f"{module.os.environ.get(key)!r}, expected {expected!r}"
            )
    finally:
        for key in _EXPECTED:
            os.environ.pop(key, None)


# ── mcp/vector-skill.py — the MCP client's parser ───────────────────────────

def test_mcp_manual_parser_strips_balanced_quote_pairs(tmp_path, monkeypatch):
    env_path = _write_env_file(tmp_path)
    for key in _EXPECTED:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VECTOR_SKILL_ENV", env_path)
    try:
        module = _fresh_module(_VECTOR_SKILL_PATH)
        for key, expected in _EXPECTED.items():
            assert module.os.environ.get(key) == expected, (
                f"mcp/vector-skill.py: {key} got "
                f"{module.os.environ.get(key)!r}, expected {expected!r}"
            )
    finally:
        for key in _EXPECTED:
            os.environ.pop(key, None)


# ── ONE PARSER, PROVED ON THE LIVE PATH ─────────────────────────────────────
#
# python-dotenv is installed in this suite (fastmcp depends on it), so these
# cases run in exactly the state a real install is in. `dotenv_values()`
# DROPS a line whose value opens a quote that never closes, and swallows the
# next quoted line into it (measured, fact:1968) — so a client that reached
# for that library would lose AGENT_TOKEN and COORDINATOR_URL here. Reading a
# value verbatim to the end of its line is what these assert, by VALUE
# (fact:1309), on both front doors.

_VERBATIM_BODY = (
    'AGENT_TOKEN="unbalanced\n'
    'COORDINATOR_URL="http://x:1"\n'
    "AGENT_ID=v # note\n"
)

_VERBATIM_ENV_KEYS = ("AGENT_TOKEN", "COORDINATOR_URL", "AGENT_ID")


def _write_verbatim_env(tmp_path) -> str:
    env_file = tmp_path / "verbatim.env"
    env_file.write_text(_VERBATIM_BODY)
    return str(env_file)


def _assert_read_verbatim(module, door: str):
    assert module._AGENT_TOKEN_FROM_FILE == '"unbalanced', (
        f"{door}: the unbalanced-quote token value was not kept verbatim — got "
        f"{module._AGENT_TOKEN_FROM_FILE!r}"
    )
    assert module.os.environ.get("COORDINATOR_URL") == "http://x:1", (
        f"{door}: the line AFTER the unbalanced quote was swallowed — "
        f"COORDINATOR_URL is {module.os.environ.get('COORDINATOR_URL')!r}"
    )
    assert module.os.environ.get("AGENT_ID") == "v # note", (
        f"{door}: an inline '#' is part of the value, read to the end of the "
        f"line — AGENT_ID is {module.os.environ.get('AGENT_ID')!r}"
    )


def test_cli_parser_reads_a_value_verbatim_to_the_end_of_its_line(tmp_path, monkeypatch):
    env_path = _write_verbatim_env(tmp_path)
    for key in _VERBATIM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SECURE_ENV_FILE", env_path)
    try:
        _assert_read_verbatim(_fresh_module(_MEMORY_BRIDGE_PATH), "memory_bridge.py")
    finally:
        for key in _VERBATIM_ENV_KEYS:
            os.environ.pop(key, None)


def test_mcp_parser_reads_a_value_verbatim_to_the_end_of_its_line(tmp_path, monkeypatch):
    env_path = _write_verbatim_env(tmp_path)
    for key in _VERBATIM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VECTOR_SKILL_ENV", env_path)
    try:
        _assert_read_verbatim(_fresh_module(_VECTOR_SKILL_PATH), "mcp/vector-skill.py")
    finally:
        for key in _VERBATIM_ENV_KEYS:
            os.environ.pop(key, None)


@pytest.mark.parametrize("path,label", [
    (_MEMORY_BRIDGE_PATH, "shared-memory/scripts/memory_bridge.py"),
    (os.path.join(_HERE, "..", "shared-memory-skill", "shared-memory",
                  "scripts", "memory_bridge.py"),
     "shared-memory-skill/shared-memory/scripts/memory_bridge.py"),
    (_VECTOR_SKILL_PATH, "mcp/vector-skill.py"),
])
def test_no_client_imports_dotenv(path, label):
    """The cheap static guard on top of the behavioural pins above: neither
    front door reaches for python-dotenv at all, so no documented invocation
    needs `--with python-dotenv` and no install can take a different parse
    depending on what happens to be resolvable."""
    source = open(path, encoding="utf-8").read()
    assert "from dotenv" not in source, f"{label} imports python-dotenv"
    assert "import dotenv" not in source, f"{label} imports python-dotenv"
