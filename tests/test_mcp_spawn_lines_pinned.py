"""S6 (RULING 4) — every documented MCP spawn site must pin `fastmcp`/`httpx`
via `requirements-mcp.lock`, the lock must exist in-tree, and no site may
still carry `python-dotenv` (the S1 defect, re-normalised on this surface
because nothing pinned it before).

Four documented sites (brief S6): `README.md` (two jsonc examples,
`shared-memory` and `rag-orchestrator`), `mcp/README.md`'s worked example,
and `mcp/mcp.json` itself. `mcp/vector-skill.py`'s only third-party imports
are `httpx` and `fastmcp` (verified) -- a breaking release of either turns
every documented MCP config into a dead server without a lock pin.

SAME CONTINUATION-JOINING RULE AS T2. These sites are JSON/JSONC arrays that
do not currently use backslash line-continuation, but the same join is run
first anyway for consistency and so a future edit that DOES wrap one across a
backslash continuation is not silently missed.
"""
import json
import os
import re

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
README = os.path.join(REPO_ROOT, "README.md")
MCP_README = os.path.join(REPO_ROOT, "mcp", "README.md")
MCP_JSON = os.path.join(REPO_ROOT, "mcp", "mcp.json")
MCP_LOCK = os.path.join(REPO_ROOT, "requirements-mcp.lock")
MCP_REQUIREMENTS = os.path.join(REPO_ROOT, "requirements-mcp.txt")


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
    """[(block_text), ...] for every ``` ... ``` fence."""
    lines = text.split("\n")
    blocks = []
    in_block = False
    buf = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_block:
                in_block = True
                buf = []
            else:
                in_block = False
                blocks.append("\n".join(buf))
        elif in_block:
            buf.append(line)
    return blocks


def _mcp_spawn_blocks_from_markdown(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    text = _join_line_continuations(text)
    return [b for b in _fenced_code_blocks(text) if "vector-skill.py" in b]


def _assert_block_is_pinned(block, label):
    assert "requirements-mcp.lock" in block, (
        f"{label}: MCP spawn command does not reference requirements-mcp.lock: "
        f"{block!r}"
    )
    assert not re.search(r'"--with"\s*,\s*"fastmcp"', block), (
        f"{label}: MCP spawn command still carries a leftover bare "
        f"'--with fastmcp' flag alongside/instead of the lock pin: {block!r}"
    )
    assert not re.search(r'"--with"\s*,\s*"httpx"', block), (
        f"{label}: MCP spawn command still carries a leftover bare "
        f"'--with httpx' flag alongside/instead of the lock pin: {block!r}"
    )
    assert "python-dotenv" not in block, (
        f"{label}: MCP spawn command still carries python-dotenv, the "
        f"dependency removed for cause (SECURITY.md) and re-normalised here: "
        f"{block!r}"
    )


def test_readme_mcp_spawn_sites_are_pinned():
    blocks = _mcp_spawn_blocks_from_markdown(README)
    assert len(blocks) >= 2, (
        f"expected at least 2 documented MCP spawn examples in README.md "
        f"(the 'shared-memory' and 'rag-orchestrator' opencode/LM Studio "
        f"blocks), found {len(blocks)}"
    )
    for i, block in enumerate(blocks):
        _assert_block_is_pinned(block, f"README.md block #{i + 1}")


def test_mcp_readme_spawn_site_is_pinned():
    blocks = _mcp_spawn_blocks_from_markdown(MCP_README)
    assert blocks, "no MCP spawn example found in mcp/README.md"
    for i, block in enumerate(blocks):
        _assert_block_is_pinned(block, f"mcp/README.md block #{i + 1}")


def test_mcp_json_is_pinned():
    with open(MCP_JSON, encoding="utf-8") as f:
        config = json.load(f)
    servers = config.get("mcpServers", {})
    spawn_servers = {
        name: entry
        for name, entry in servers.items()
        if any("vector-skill.py" in str(a) for a in entry.get("args", []))
    }
    assert spawn_servers, "no vector-skill.py-spawning entry found in mcp/mcp.json"
    for name, entry in spawn_servers.items():
        args_text = " ".join(str(a) for a in entry.get("args", []))
        _assert_block_is_pinned(
            "\n".join(f'"{a}"' for a in entry.get("args", [])),
            f"mcp/mcp.json[{name}]",
        )


def test_mcp_lock_exists_and_is_generated_the_documented_way():
    assert os.path.isfile(MCP_LOCK), (
        "requirements-mcp.lock does not exist in-tree -- the pinned MCP "
        "spawn lines point at a file that isn't there"
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
