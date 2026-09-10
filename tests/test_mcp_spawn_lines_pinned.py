"""Every documented MCP spawn site must be the PEP 723 form —
`uv run --no-project <path to vector-skill.py>` — and the connector's own
inline dependency pins must equal the versions `requirements-mcp.lock` holds.

WHY THE FORM CHANGED (W7/F1). The previous pinned form named
`requirements-mcp.lock` through `--with-requirements`. That flag resolves the
lock RELATIVE TO THE SPAWNING PROCESS'S WORKING DIRECTORY, and no documented
deployment spawns the connector from the repository root — an MCP host launches
its stdio servers from a directory nobody documents. Measured from a neutral
directory: `error: File not found: requirements-mcp.lock`, exit 2. It is also a
local dependency-hijack surface: whoever can plant a file of that name in the
host's spawn directory chooses what `uv` installs and executes. PEP 723 inline
metadata travels inside the script, so it cannot be aimed somewhere else.
`--no-project` is part of the form, not decoration: without it `uv run` tries to
build a project whenever the spawn directory happens to contain a
`pyproject.toml`.

WHY DISCOVERY IS BY SHAPE, NEVER BY FILENAME. A filename-based glob over files
mentioning `vector-skill.py` sweeps in `CHANGELOG.md` (which records the old
`--with fastmcp` lines as history, and must), `SECURITY.md`, both `SKILL.md`
copies, `AGENTS.md`, `mcp/system-prompt.md` and `mcp/CONSTITUTION_SNIPPET_MCP.md`
— so it is either permanently red on history, or blind behind an exclude list
that swallows exactly the files a NEW spawn example would first appear in. This
test instead discovers, in every markdown file in the tree, each fenced code
block that contains `uv` together with `vector-skill.py`, plus every entry in
`mcp/mcp.json` whose args name the connector. A new documented example is
therefore checked wherever someone writes it.

WHY A TOKEN SEQUENCE, NOT A SUBSTRING. The blocks are JSON, JSONC and markdown;
quoting, commas and line wrapping differ between them and say nothing about the
command. Each block is normalised to a token list first (quotes, commas and
whitespace stripped, backslash continuations joined), so all three shapes are
checked identically, and the assertion is then positional: `run`, then exactly
`--no-project`, then the connector's path — nothing else in between, which is
what rules out a stray `python`, a leftover `--with`, or a re-added
`--with-requirements`.

EACH SITE KEEPS ITS OWN PATH. A walled install spawns the walled copy; the
VS Code workspace example spawns the repository copy, because that install is
not walled. One pasted path for every site is a documented command that cannot
start, so the assertion pins the SHAPE of the path (it ends in
`vector-skill.py`) and never its value.
"""
import ast
import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MCP_JSON = os.path.join(REPO_ROOT, "mcp", "mcp.json")
MCP_LOCK = os.path.join(REPO_ROOT, "requirements-mcp.lock")
MCP_REQUIREMENTS = os.path.join(REPO_ROOT, "requirements-mcp.txt")
CONNECTOR = os.path.join(REPO_ROOT, "mcp", "vector-skill.py")

# Directories that are gitignored, local-only and never shipped — a working
# note in one of them is not a documented spawn site. Named, with the reason,
# rather than a filename exclude list over the shipped tree (see docstring).
_SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    "Local_Documentation",  # gitignored: this repo's transient working notes
    "research", ".claude", ".gemini", ".codex", ".grok", "scratch",
}

# The documented sites this test expects to find. It is a FLOOR, not a
# whitelist: any further block discovered by shape is checked too. Its job is
# to fail when a site DISAPPEARS from discovery — the way a filename-based
# glob fails silently.
_EXPECTED_SITES = {
    os.path.join("README.md"): 2,
    os.path.join("mcp", "README.md"): 1,
    os.path.join("shared-memory", "Documentation", "vscode-copilot-mcp.md"): 2,
}


def _join_line_continuations(text):
    raw_lines = text.split("\n")
    logical = []
    i = 0
    n = len(raw_lines)
    while i < n:
        buf = raw_lines[i]
        while buf.endswith("\\") and i + 1 < n:
            i += 1
            buf = buf[:-1].rstrip() + " " + raw_lines[i].strip()
        logical.append(buf)
        i += 1
    return "\n".join(logical)


def _fenced_code_blocks(text):
    lines = text.split("\n")
    blocks = []
    in_block = False
    buf = []
    for line in lines:
        if line.strip().startswith("```"):
            if not in_block:
                in_block = True
                buf = []
            else:
                in_block = False
                blocks.append("\n".join(buf))
        elif in_block:
            buf.append(line)
    return blocks


_UV_TOKEN = re.compile(r"(?:^|[\"'\s/])uv(?:[\"'\s]|$)")


def _is_spawn_block(block):
    """The SHAPE: a `uv` invocation that names the connector."""
    return "vector-skill.py" in block and bool(_UV_TOKEN.search(block))


def _tokenise(block):
    """Normalise a JSON / JSONC / markdown command block to bare tokens.

    Quotes, commas and whitespace carry no meaning here; a comment line is
    dropped whole, so a flag "named only in a comment" is correctly NOT
    counted as being on the command line.
    """
    tokens = []
    for line in block.split("\n"):
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("#"):
            continue
        for raw in stripped.replace(",", " ").split():
            tok = raw.strip("\"'[](){}")
            if tok:
                tokens.append(tok)
    return tokens


def _assert_pep723_spawn_form(block, label):
    tokens = _tokenise(_join_line_continuations(block))

    assert "run" in tokens, f"{label}: no `run` token in the spawn command: {block!r}"
    run_at = tokens.index("run")

    script_at = None
    for i in range(run_at + 1, len(tokens)):
        if tokens[i].endswith("vector-skill.py"):
            script_at = i
            break
    assert script_at is not None, (
        f"{label}: no path ending in `vector-skill.py` follows `run` — the "
        f"documented command does not spawn the connector: {block!r}"
    )

    between = tokens[run_at + 1 : script_at]
    assert between == ["--no-project"], (
        f"{label}: the spawn command between `run` and the connector's path "
        f"must be exactly ['--no-project'] (the PEP 723 form: no `python`, no "
        f"`--with`, no `--with-requirements`) — found {between!r} in: {block!r}"
    )

    command = tokens[: script_at + 1]
    for forbidden in ("--with", "--with-requirements"):
        assert forbidden not in command, (
            f"{label}: `{forbidden}` is still on the spawn command line; the "
            f"connector declares its dependencies inline now: {block!r}"
        )
    assert "python-dotenv" not in command, (
        f"{label}: the spawn command still carries python-dotenv, the "
        f"dependency removed for cause (SECURITY.md): {block!r}"
    )


def _markdown_spawn_blocks():
    """[(relpath, index, block), ...] for every markdown fence in the tree that
    has the spawn shape."""
    found = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in sorted(files):
            if not name.endswith(".md"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            if "vector-skill.py" not in text:
                continue
            rel = os.path.relpath(path, REPO_ROOT)
            for i, block in enumerate(_fenced_code_blocks(_join_line_continuations(text))):
                if _is_spawn_block(block):
                    found.append((rel, i, block))
    return found


def _mcp_json_spawn_entries():
    with open(MCP_JSON, encoding="utf-8") as f:
        config = json.load(f)
    entries = []
    for name, entry in config.get("mcpServers", {}).items():
        args = entry.get("args", [])
        if any("vector-skill.py" in str(a) for a in args):
            block = "\n".join([str(entry.get("command", ""))] + [str(a) for a in args])
            entries.append((name, block))
    return entries


def test_every_documented_markdown_spawn_block_is_the_pep723_form():
    blocks = _markdown_spawn_blocks()
    assert blocks, (
        "no MCP spawn block discovered anywhere in the tree — discovery is by "
        "SHAPE (a fenced block naming `uv` and `vector-skill.py`); finding "
        "none means either every documented example vanished or the shape "
        "test itself stopped matching"
    )
    per_file = {}
    for rel, _, _ in blocks:
        per_file[rel] = per_file.get(rel, 0) + 1
    for rel, expected in _EXPECTED_SITES.items():
        assert per_file.get(rel, 0) >= expected, (
            f"{rel} is documented as carrying {expected} MCP spawn example(s) "
            f"but discovery found {per_file.get(rel, 0)} — a site was deleted, "
            f"or its block no longer has the spawn shape"
        )
    for rel, index, block in blocks:
        _assert_pep723_spawn_form(block, f"{rel} code block #{index + 1}")


def test_mcp_json_is_the_pep723_form():
    entries = _mcp_json_spawn_entries()
    assert entries, "no vector-skill.py-spawning entry found in mcp/mcp.json"
    for name, block in entries:
        _assert_pep723_spawn_form(block, f"mcp/mcp.json[{name}]")


# --- the table-driven proof: the correct form passes, five broken forms fail --

_GOOD_JSON_BLOCK = """
"shared-memory": {
  "type": "local",
  "command": ["/home/you/.local/bin/uv", "run", "--no-project",
              "/home/you/.config/host/shared-memory-mcp/vector-skill.py"]
}
"""

_GOOD_MARKDOWN_BLOCK = "uv run --no-project /path/to/mcp/vector-skill.py"

_BROKEN_BLOCKS = {
    "the old --with flag naming the lock as a package": """
"args": ["run", "--with", "requirements-mcp.lock", "--no-project",
         "/path/to/mcp/vector-skill.py"]
""",
    "a dropped --no-project": """
"args": ["run", "/path/to/mcp/vector-skill.py"]
""",
    "the flag named only in a comment": """
// spawn with --no-project
"args": ["run", "/path/to/mcp/vector-skill.py"]
""",
    "the wrong filename on the command line": """
// delivers vector-skill.py into the walled directory
"args": ["run", "--no-project", "/path/to/mcp/vectorskill.py"]
""",
    "a leftover --with fastmcp (JSON shape)": """
"args": ["run", "--no-project", "--with", "fastmcp", "--with", "httpx",
         "python", "/path/to/mcp/vector-skill.py"]
""",
    "a leftover --with fastmcp (markdown shape)":
        "uv run --with fastmcp --with httpx python /path/to/mcp/vector-skill.py",
}


@pytest.mark.parametrize("block", [_GOOD_JSON_BLOCK, _GOOD_MARKDOWN_BLOCK])
def test_the_correct_spawn_form_passes_the_assertion(block):
    _assert_pep723_spawn_form(block, "synthetic good block")


@pytest.mark.parametrize("label", sorted(_BROKEN_BLOCKS))
def test_every_broken_spawn_form_fails_the_assertion(label):
    """A check that has only ever passed has not been tested. Each of QA's
    broken forms must make the assertion raise — not merely differ."""
    with pytest.raises(AssertionError):
        _assert_pep723_spawn_form(_BROKEN_BLOCKS[label], f"synthetic: {label}")


# --- P-F1b: the inline pins equal the lock's pins ---------------------------


def _inline_dependencies():
    """The `dependencies = [...]` list from the connector's PEP 723 block."""
    with open(CONNECTOR, encoding="utf-8") as f:
        text = f.read()
    match = re.search(
        r"^# /// script\s*$(.*?)^# ///\s*$", text, re.MULTILINE | re.DOTALL
    )
    assert match, (
        "mcp/vector-skill.py carries no PEP 723 inline metadata block — the "
        "documented `uv run --no-project` spawn has nothing to install from"
    )
    body = "\n".join(
        line[2:] if line.startswith("# ") else line.lstrip("#")
        for line in match.group(1).strip("\n").split("\n")
    )
    dep_match = re.search(r"dependencies\s*=\s*(\[[^\]]*\])", body, re.DOTALL)
    assert dep_match, f"no `dependencies` key in the inline block: {body!r}"
    return ast.literal_eval(dep_match.group(1))


def _lock_version(package):
    with open(MCP_LOCK, encoding="utf-8") as f:
        for line in f:
            match = re.match(r"^" + re.escape(package) + r"==([^\s\\]+)", line)
            if match:
                return match.group(1)
    return None


def test_inline_pins_equal_the_lock_pins():
    """The lock stays in-tree as the hashed audit artefact; these two pins are
    what actually installs. If they can drift apart silently, the lock is
    decoration."""
    deps = _inline_dependencies()
    assert {d.split("==")[0] for d in deps} == {"fastmcp", "httpx"}, (
        f"the inline block must pin exactly fastmcp and httpx — "
        f"vector-skill.py's only third-party imports — found {deps!r}"
    )
    for dep in deps:
        assert "==" in dep, (
            f"inline dependency {dep!r} is not pinned to an exact version"
        )
        package, version = dep.split("==", 1)
        locked = _lock_version(package)
        assert locked is not None, (
            f"{package} is pinned inline but absent from requirements-mcp.lock"
        )
        assert version == locked, (
            f"{package} is pinned inline at {version} but requirements-mcp.lock "
            f"holds {locked} — the lock is the source of truth; regenerate it "
            f"or correct the inline block, never let the two drift"
        )


def test_mcp_lock_exists_and_is_generated_the_documented_way():
    assert os.path.isfile(MCP_LOCK), (
        "requirements-mcp.lock does not exist in-tree -- it is the hashed "
        "audit artefact the connector's inline pins are checked against"
    )
    assert os.path.isfile(MCP_REQUIREMENTS), "requirements-mcp.txt does not exist in-tree"
    with open(MCP_REQUIREMENTS, encoding="utf-8") as f:
        req_text = f.read()
    assert "fastmcp" in req_text and "httpx" in req_text, (
        "requirements-mcp.txt does not name both fastmcp and httpx -- those "
        "are vector-skill.py's only third-party imports"
    )
    with open(MCP_LOCK, encoding="utf-8") as f:
        lock_header = f.read().split("\n", 5)
    header_text = "\n".join(lock_header[:3])
    assert "uv pip compile requirements-mcp.txt" in header_text, (
        f"requirements-mcp.lock's header does not record the documented "
        f"generation command (same form as requirements-gateway.lock's own "
        f"header): {header_text!r}"
    )


# --- P-F1a: the documented command actually starts, from a NEUTRAL directory -


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
def test_the_documented_spawn_starts_from_a_directory_that_is_neither_repo_nor_script():
    """The whole point of F1: the command must work from wherever an MCP host
    happens to spawn it.

    The temp directory is neither the repository root nor `mcp/`, and the
    assertion below runs the PRE-FIX form there FIRST — if that form does not
    fail with `File not found`, the directory is not actually neutral and this
    test would be passing for the wrong reason.
    """
    with tempfile.TemporaryDirectory() as neutral:
        # 1. The pre-fix form must be broken here. This is the control.
        pre_fix = subprocess.run(
            [
                "uv", "run", "--no-project",
                "--with-requirements", "requirements-mcp.lock",
                "python", CONNECTOR,
            ],
            cwd=neutral,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert pre_fix.returncode != 0 and "File not found" in pre_fix.stderr, (
            "the pre-fix `--with-requirements requirements-mcp.lock` form did "
            "NOT fail in this directory — the directory is not neutral, so a "
            "pass below would prove nothing.\n"
            f"rc={pre_fix.returncode} stderr={pre_fix.stderr[:400]!r}"
        )

        # 2. The documented form must start. FastMCP's stdio transport reads
        #    stdin, gets EOF from DEVNULL and exits cleanly — reaching that
        #    point means the interpreter imported both fastmcp and httpx (the
        #    connector imports them at module scope, before any server runs).
        result = subprocess.run(
            ["uv", "run", "--no-project", CONNECTOR],
            cwd=neutral,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=600,
        )
        combined = result.stdout + result.stderr
        assert "File not found" not in combined, (
            f"the documented spawn still resolves a file relative to the "
            f"spawn directory: {combined[:400]!r}"
        )
        assert "ModuleNotFoundError" not in combined, (
            f"the connector started but a declared dependency was missing: "
            f"{combined[:400]!r}"
        )
        assert result.returncode == 0, (
            f"`uv run --no-project <connector>` did not start cleanly from a "
            f"neutral directory: rc={result.returncode} {combined[:800]!r}"
        )
        assert "FastMCP" in combined, (
            f"the connector process started but never reached FastMCP — "
            f"fastmcp was not importable: {combined[:400]!r}"
        )
