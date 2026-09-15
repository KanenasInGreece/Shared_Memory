"""Test honest role wording across SKILL.md, MCP docs, and HANDOFF.md (ADV-1 / QA-1).

Named CLI query shortcuts (why-to-check, who-decided, retrospectives, agent-decisions)
call query_graph() -> POST /memory/graph and return 403 for read tokens.
Docs must not claim they hit search/telemetry or remain available to read.
"""
import os

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


def test_handoff_proposed_readme_sentence_1_has_honest_wording():
    """HANDOFF.md proposed README sentence 1 must carry honest wording (ADV-1 / QA-1)."""
    text = _read(os.path.join(_ROOT, "HANDOFF.md"))
    assert (
        "Querying the raw graph via `POST /memory/graph` (CLI `graph` or MCP `graph_query`) "
        "and named CLI `query` templates require an authenticated token with `full` or `admin` role "
        "(`read` tokens receive HTTP 403); `search`, `lineage`/`status`, and `telemetry` remain for `read`."
    ) in text
