"""Test honest role wording across SKILL.md, MCP docs, and installer surfaces.

ADV-1 / QA-1: named CLI query shortcuts (why-to-check, who-decided,
retrospectives, agent-decisions) call query_graph() -> POST /memory/graph
and return 403 for read tokens. Docs must not claim they hit
search/telemetry or remain available to read.

F-AU-001: user-facing read-role help (minter stdout, AGENTS.md, SECURITY.md,
.env.example) must match gateway truth — a read token may reach
GET /memory/telemetry, POST /memory/search, and GET /memory/status/{pg_id};
POST /memory/graph is 403; /health is anonymous (not a read-role grant).
Mutation: restoring the graph-yes / search-omitted print must kill the
stdout tests below.
"""
import contextlib
import importlib.util
import io
import os
import re
import sys

_ROOT = os.path.join(os.path.dirname(__file__), "..")

_DOCS = [
    os.path.join(_ROOT, "shared-memory", "SKILL.md"),
    os.path.join(_ROOT, "shared-memory-skill", "shared-memory", "SKILL.md"),
    os.path.join(_ROOT, "mcp", "README.md"),
    os.path.join(_ROOT, "mcp", "system-prompt.md"),
    os.path.join(_ROOT, "mcp", "CONSTITUTION_SNIPPET_MCP.md"),
    os.path.join(_ROOT, "mcp", "vector-skill.py"),
]


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_no_doc_claims_query_shortcuts_hit_search_or_stay_available_to_read():
    """ADV-1 / QA-1: No doc may claim named CLI query shortcuts hit search/telemetry or stay available to read."""
    forbidden_snippets = [
        "hit search/telemetry and stay available to `read`",
        "hit search and telemetry and stay available to `read`",
        "stay available to `read` tokens",
        "remain accessible to `read` tokens",
    ]
    for path in _DOCS:
        text = _read(path)
        for snippet in forbidden_snippets:
            assert snippet not in text, f"Found false claim {snippet!r} in {path}"


def test_skill_copies_remain_byte_identical():
    """Both SKILL.md copies must be byte-identical."""
    src = _read(os.path.join(_ROOT, "shared-memory", "SKILL.md"))
    shipped = _read(os.path.join(_ROOT, "shared-memory-skill", "shared-memory", "SKILL.md"))
    assert src == shipped, "The two SKILL.md copies diverged"


def test_honest_wording_present_in_skill():
    """SKILL.md must honestly state that graph and named query templates require full/admin."""
    text = _read(os.path.join(_ROOT, "shared-memory", "SKILL.md"))
    assert "named CLI `query` templates require `full` or `admin`" in text
    assert "search" in text and "telemetry" in text


# ── F-AU-001: installer / minter read-role help matches gateway truth ────────


def _norm_routes(text: str) -> str:
    """Collapse whitespace, drop markdown backticks, and strip paste-safe `# ` prefixes."""
    pieces = []
    for raw in text.splitlines() or [text]:
        line = raw.strip().replace("`", "")
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        pieces.append(line)
    return " ".join(" ".join(pieces).split())


def _assert_read_role_blurb_matches_gateway(blurb: str, *, label: str) -> None:
    """A read token may reach telemetry, search, and lineage; graph is 403;
    /health is anonymous (not a read-role grant). Do not claim search 403.
    Do not claim graph yes."""
    assert blurb.strip(), f"{label}: empty read-role blurb"
    norm = _norm_routes(blurb)
    assert "POST /memory/search" in norm, (
        f"{label}: read-role text must name POST /memory/search"
    )
    assert "GET /memory/telemetry" in norm, (
        f"{label}: read-role text must name GET /memory/telemetry"
    )
    assert "GET /memory/status/{pg_id}" in norm, (
        f"{label}: read-role text must name GET /memory/status/{{pg_id}}"
    )
    assert re.search(r"(?:GET )?/?health is anonymous", norm, re.I), (
        f"{label}: /health is anonymous, not a read-role grant"
    )
    assert "not a read-role grant" in norm.lower(), (
        f"{label}: must say /health is not a read-role grant"
    )
    # Graph, if named, is only as a 403 — never as a reachable route.
    # Mutation: "and POST /memory/graph (read-only Cypher)" has no "is 403"
    # immediately after the path, so this dies.
    for m in re.finditer(r"POST /memory/graph", norm):
        after = norm[m.end(): m.end() + 48].lstrip(" )'\"")
        if re.match(r"(?:is |returns |answers |→ )\*?403", after, re.I):
            continue
        raise AssertionError(
            f"{label}: POST /memory/graph is granted as a read-role route; "
            f"context: {norm[max(0, m.start() - 70): m.end() + 40]!r}"
        )
    inverted = re.search(
        r"(?:save|retrospective).{0,48}search.{0,48}(?:return|returns)\s+\*?\*?403",
        norm,
        re.I,
    )
    assert not inverted, f"{label}: claims search 403 (inverted allowlist)"


def _comments_after_agent_roles(out: str) -> str:
    """The paste-safe read-role comment block printed after AGENT_ROLES=."""
    lines = out.splitlines()
    chunks = []
    for i, line in enumerate(lines):
        if not line.startswith("AGENT_ROLES="):
            continue
        block = []
        for later in lines[i + 1:]:
            if not later.strip() or not later.startswith("#"):
                break
            block.append(later)
        chunks.append("\n".join(block))
    return "\n\n".join(chunks)


def _load_generate_tokens():
    """Fresh module; never the live gateway .env (fact:1471)."""
    scripts_dir = os.path.join(_ROOT, "shared-memory", "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    path = os.path.join(scripts_dir, "generate_tokens.py")
    spec = importlib.util.spec_from_file_location("generate_tokens_wording_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._DEFAULT_GATEWAY_ENV = "/nonexistent/generate-tokens-wording-test.env"
    return mod


def _capture(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*a, **kw)
    return result, buf.getvalue()


def test_bulk_mint_stdout_names_search_and_does_not_grant_graph(tmp_path):
    """Prove-It: bulk-mint stdout names search and does not grant graph."""
    gt = _load_generate_tokens()
    gt.LOCAL_SKILL_ENV_PATHS = {}
    (_result), out = _capture(gt.mint, env_path=str(tmp_path / ".env"))
    blurb = _comments_after_agent_roles(out)
    assert blurb, "bulk mint printed no read-role comment after AGENT_ROLES="
    _assert_read_role_blurb_matches_gateway(
        blurb, label="generate_tokens.py bulk mint stdout",
    )


def test_add_monitor_stdout_names_search_and_does_not_grant_graph(tmp_path):
    """Prove-It: --add monitor stdout names search and does not grant graph."""
    gt = _load_generate_tokens()
    env_path = tmp_path / ".env"
    env_path.write_text("AGENT_TOKENS=claude:sha256:" + ("a" * 64) + "\n")
    (_rc_token), out = _capture(
        gt.add_agent, "monitor", install_path=None, env_path=str(env_path),
    )
    blurb = _comments_after_agent_roles(out)
    assert blurb, "--add monitor printed no read-role comment after AGENT_ROLES="
    _assert_read_role_blurb_matches_gateway(
        blurb, label="generate_tokens.py --add monitor stdout",
    )


def _agents_md_read_role_paragraph() -> str:
    text = _read(os.path.join(_ROOT, "AGENTS.md"))
    start = text.find("A READ-ONLY IDENTITY IS ALWAYS MINTED READ-ONLY")
    assert start != -1, "AGENTS.md is missing the read-only identity paragraph"
    rest = text[start:]
    end = rest.find("\n⚠")
    assert end != -1, "AGENTS.md read-role paragraph has no following warning"
    return rest[:end]


def test_agents_md_read_role_sentence_matches_gateway():
    """Public installer: the read-role sentence matches gateway truth."""
    _assert_read_role_blurb_matches_gateway(
        _agents_md_read_role_paragraph(), label="AGENTS.md read-role sentence",
    )


def _security_md_read_role_paragraph() -> str:
    text = _read(os.path.join(_ROOT, "SECURITY.md"))
    start = text.find("**Read-only roles (`AGENT_ROLES`).**")
    assert start != -1, "SECURITY.md is missing the Read-only roles paragraph"
    rest = text[start:]
    end = rest.find("\n**Admin role")
    assert end != -1, "SECURITY.md read-role paragraph has no following Admin role heading"
    return rest[:end]


def test_security_md_read_role_paragraph_matches_gateway():
    """SECURITY.md currently inverted graph-yes / search-403 — must match gateway."""
    _assert_read_role_blurb_matches_gateway(
        _security_md_read_role_paragraph(),
        label="SECURITY.md Read-only roles paragraph",
    )


def _env_example_read_role_comment() -> str:
    text = _read(os.path.join(_ROOT, "shared-memory", ".env.example"))
    start = text.find("optional read-only roles")
    assert start != -1, ".env.example is missing the read-only roles comment"
    rest = text[start:]
    end = rest.find("AGENT_ROLES=")
    assert end != -1, ".env.example read-role comment has no AGENT_ROLES= line"
    # Include the assignment line so the block is the whole comment + example.
    nl = rest.find("\n", end)
    return rest[: nl if nl != -1 else None]


def test_env_example_read_role_comment_matches_gateway():
    """shared-memory/.env.example read-role comment matches gateway truth."""
    _assert_read_role_blurb_matches_gateway(
        _env_example_read_role_comment(),
        label="shared-memory/.env.example read-role comment",
    )
