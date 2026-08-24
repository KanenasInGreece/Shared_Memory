"""GROUP 1 — the MCP constitution snippet joins the pin contract.

`CONSTITUTION_SNIPPET_MCP.md` is delivered into every `mcp`-kind install and
spliced into an agent host's own constitution file (AGENTS.md Phase 8b). What
makes that splice repeatable rather than duplicative is the MARKER: an
install/upgrade pass finds the exact block by its opening comment, compares the
version in it against the file's, and re-proposes only on a difference. A
missing or malformed marker does not fail loudly — it silently turns every
future upgrade into a second copy of the block appended below the first.

The second guard here is the parity one the design turns on: the same four
standing rules live in TWO files (this snippet for an agent host,
`system-prompt.md` for an LLM server), and ⛔ no rule may live in only one of
them. That is a promise nothing enforces by construction, so it is enforced
here — a rule dropped from one file while it lives on in the other is exactly
the drift that makes two front doors disagree.
"""
import os
import re

_REPO = os.path.join(os.path.dirname(__file__), "..")

_SNIPPET = os.path.join(_REPO, "mcp", "CONSTITUTION_SNIPPET_MCP.md")
_SYSTEM_PROMPT = os.path.join(_REPO, "mcp", "system-prompt.md")
_CLI_SNIPPET = os.path.join(_REPO, "shared-memory", "CONSTITUTION_SNIPPET.md")

_OPEN = re.compile(r"<!--\s*shared-memory:mcp-constitution-snippet\s+v(\d+)\s*-->")
_CLOSE = "<!-- /shared-memory:mcp-constitution-snippet -->"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_the_snippet_carries_a_versioned_open_and_close_marker():
    text = _read(_SNIPPET)
    match = _OPEN.search(text)
    assert match, (
        "CONSTITUTION_SNIPPET_MCP.md has no `<!-- shared-memory:"
        "mcp-constitution-snippet vN -->` marker. Without it Phase 8c cannot "
        "find the installed block, and every upgrade appends a second copy "
        "instead of replacing the first.")
    assert int(match.group(1)) >= 1
    assert _CLOSE in text, (
        "the snippet has an opening marker but no closing one — a find-and-"
        "replace would swallow whatever follows it in the operator's file")
    assert text.index(_CLOSE) > match.end(), "the close marker precedes the open marker"


def test_the_two_snippets_use_DISTINCT_markers():
    """An agent holds one block or the other, never both. Sharing a marker
    would make a Phase 8c pass for one snippet find and overwrite the other's
    block — silently replacing an MCP agent's rules with the CLI wording that
    tells it to use a skill it does not have."""
    mcp_marker = _OPEN.search(_read(_SNIPPET)).group(0)
    assert mcp_marker not in _read(_CLI_SNIPPET)
    cli_marker = re.search(r"<!--\s*shared-memory:constitution-snippet\s+v\d+\s*-->",
                           _read(_CLI_SNIPPET))
    assert cli_marker, "fixture stale: the CLI snippet's own marker is gone"
    assert cli_marker.group(0) not in _read(_SNIPPET)


def test_the_block_names_no_install_specific_value():
    """The block is copied VERBATIM into any host's constitution file. A path, a
    host name or an agent name in it would need per-install substitution — and a
    regenerated block is one no later find-and-replace can match."""
    text = _read(_SNIPPET)
    block = text[text.index(_OPEN.search(text).group(0)):text.index(_CLOSE)]
    for forbidden in ("/home/", "~/", "AGENT_TOKEN", "localhost", "8888"):
        assert forbidden not in block, (
            f"the snippet block hardcodes {forbidden!r} — it must carry no "
            "install-specific value")


# ── The parity guard: no rule lives in only one of the two files ─────────────

# Each rule, as a pair of substrings that must appear in BOTH files. Written as
# phrases rather than whole sentences deliberately: the wording is allowed to be
# adapted to each surface (a constitution block vs a system prompt), the RULE is
# not allowed to be absent.
_RULES = {
    "search first, then graph depth": ("hybrid_search_and_rerank", "graph_query"),
    "quote the ref, never a bare id": ("ref", "unique only WITHIN its table"),
    "role decides which writes succeed": ("403", "role"),
    "never a database MCP alongside": ("database MCP", "gateway"),
}


def test_every_standing_rule_appears_in_both_mcp_rule_files():
    """⛔ NO RULE MAY LIVE IN ONLY ONE OF THE TWO. `system-prompt.md` is the
    LLM-server wrapper and `CONSTITUTION_SNIPPET_MCP.md` the agent-host block;
    they are two surfaces onto ONE set of rules. The role/403 rule is the
    measured case — it existed in neither file's predecessor, so an MCP identity
    confined read-only met its first 403 with no idea it was the system working
    as designed."""
    # Whitespace-normalised: the two files wrap at different widths, so a needle
    # can straddle a newline in one and not the other. A parity check that a
    # reflow could break would be measuring line length, not content.
    def _flat(path):
        return re.sub(r"\s+", " ", _read(path))

    snippet, prompt = _flat(_SNIPPET), _flat(_SYSTEM_PROMPT)
    missing = []
    for rule, needles in _RULES.items():
        for needle in needles:
            if needle not in snippet:
                missing.append(f"{rule!r}: {needle!r} absent from CONSTITUTION_SNIPPET_MCP.md")
            if needle not in prompt:
                missing.append(f"{rule!r}: {needle!r} absent from system-prompt.md")
    assert not missing, "\n".join(missing)


def test_the_system_prompt_points_at_its_twin():
    """A reader who lands on one file has to learn the other exists, or the
    parity rule is invisible to whoever next edits either."""
    prompt = _read(_SYSTEM_PROMPT)
    assert "CONSTITUTION_SNIPPET_MCP.md" in prompt
    assert "system-prompt.md" in _read(_SNIPPET)
