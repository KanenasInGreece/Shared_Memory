"""Tests for secure_env.read_env_value and read_env_key CLI helper (Slice 5).

Verifies the single Python parser used by bash scripts (preflight.sh,
postflight.sh, init_db.sh, reconcile_stack.sh).

Cases required by BRIEF_FOLDED.md:
  - KEY="foo # bar"  -> foo # bar
  - KEY=foo # comment -> foo
  - KEY="quoted"     -> quoted
  - KEY=value\r      -> value
Plus mutation proof: inverting comment-vs-quote order in Python must fail
the quoted-hash test.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent / "shared-memory" / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import secure_env


def test_read_env_value_quoted_hash(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text('KEY="foo # bar"\n')
    assert secure_env.read_env_value(env_file, "KEY") == "foo # bar"


def test_read_env_value_inline_comment(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("KEY=foo # comment\n")
    assert secure_env.read_env_value(env_file, "KEY") == "foo"


def test_read_env_value_quoted(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text('KEY="quoted"\n')
    assert secure_env.read_env_value(env_file, "KEY") == "quoted"


def test_read_env_value_crlf(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"KEY=value\r\n")
    assert secure_env.read_env_value(env_file, "KEY") == "value"


def test_read_env_value_single_quoted_hash(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("KEY='single # quoted'\n")
    assert secure_env.read_env_value(env_file, "KEY") == "single # quoted"


def test_read_env_value_quoted_with_inline_comment(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text('KEY="quoted" # comment\n')
    assert secure_env.read_env_value(env_file, "KEY") == "quoted"


def test_read_env_value_absent_returns_default(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER=1\n")
    assert secure_env.read_env_value(env_file, "KEY") is None
    assert secure_env.read_env_value(env_file, "KEY", default="def") == "def"


def test_read_env_key_cli(tmp_path: Path):
    cli = _SCRIPTS_DIR / "read_env_key.py"
    env_file = tmp_path / ".env"
    env_file.write_text('FOO="bar # baz"\nBAZ=qux # comment\n')
    res = subprocess.run(
        [sys.executable, str(cli), str(env_file), "FOO"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert res.stdout == "bar # baz"

    res2 = subprocess.run(
        [sys.executable, str(cli), str(env_file), "BAZ"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert res2.stdout == "qux"


def test_mutation_inverted_comment_vs_quote_fails_quoted_hash():
    """Mutation proof: if comment stripping runs before quote checking,
    quoted-hash test must die."""
    import re

    def mutated_parser(val: str) -> str:
        val = val.strip()
        # Inverted: comment stripped FIRST without checking quotes
        m = re.search(r"\s+#.*$", val)
        if m:
            val = val[:m.start()].strip()
        elif val.startswith("#"):
            return ""
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            return val[1:-1]
        return val

    # On KEY="foo # bar", the naive/inverted parser strips " # bar" and leaves '"foo'
    result = mutated_parser('"foo # bar"')
    assert result != "foo # bar", "Mutation should fail on quoted hash"
    assert result == '"foo'


@pytest.mark.parametrize(
    "script_name",
    [
        "preflight.sh",
        "postflight.sh",
        "init_db.sh",
        "reconcile_stack.sh",
    ],
)
def test_bash_scripts_use_read_env_key(script_name: str, tmp_path: Path):
    """Verify that all four named bash files define read_env calling read_env_key.py."""
    script_path = _SCRIPTS_DIR / script_name
    text = script_path.read_text(encoding="utf-8")
    assert 'read_env() { python3 "$SCRIPT_DIR/read_env_key.py" "$ENV_FILE" "$1"; }' in text

    # Exercise bash execution directly
    env_file = tmp_path / ".env"
    env_file.write_text('TEST_KEY="hello # world"\n')
    bash_cmd = (
        f'SCRIPT_DIR="{_SCRIPTS_DIR}"\n'
        f'ENV_FILE="{env_file}"\n'
        'read_env() { python3 "$SCRIPT_DIR/read_env_key.py" "$ENV_FILE" "$1"; }\n'
        'val="$(read_env TEST_KEY)"\n'
        'printf "%s" "$val"\n'
    )
    res = subprocess.run(
        ["bash", "-c", bash_cmd],
        capture_output=True,
        text=True,
        check=True,
    )
    assert res.stdout == "hello # world"

