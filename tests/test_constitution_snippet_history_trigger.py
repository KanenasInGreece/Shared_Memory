"""The "was it tested?" trigger — added 2026-08-25, on every surface.

Ruling: the 2026-08-25 debrief (`retro:1593` on `decision:1099`; portable lesson
`fact:1594`). Measured gap: a question of the form "was X tested / did we / has
this been done / was that tried or rejected" does not read as any of the search-
first snippet's named trigger categories (project direction, a prior decision, a
claim that may have been superseded) — so an agent answered one from a state
instrument at hand (a file, a config, an `ls`) instead of the shared memory
store. State can only confirm an answer already known, never supply one: it
cannot show what was exercised and has since been removed, moved, or contained.
One such answer reached the public README.

The fix touches all three constitution/system-prompt surfaces in the same
words, with their version markers bumped so an installed block reads as
drifted and Phase 8b/8c re-propose it. This file is the mechanical guard that
the phrase actually landed everywhere it must, and that the two tracked CLI
copies stayed byte-identical.
"""
import os
import re

_REPO = os.path.join(os.path.dirname(__file__), "..")

_CLI_SNIPPET = os.path.join(_REPO, "shared-memory", "CONSTITUTION_SNIPPET.md")
_CLI_SNIPPET_SHIPPED = os.path.join(
    _REPO, "shared-memory-skill", "shared-memory", "CONSTITUTION_SNIPPET.md")
_MCP_SNIPPET = os.path.join(_REPO, "mcp", "CONSTITUTION_SNIPPET_MCP.md")
_SYSTEM_PROMPT = os.path.join(_REPO, "mcp", "system-prompt.md")

_PHRASE = "tested, tried, rejected or done"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_the_cli_snippet_carries_v3():
    text = _read(_CLI_SNIPPET)
    assert "<!-- shared-memory:constitution-snippet v3 -->" in text, (
        "the CLI snippet's marker did not advance to v3 — an installed block "
        "will not read as drifted and Phase 8c will not re-propose it")


def test_the_mcp_snippet_carries_v2():
    text = _read(_MCP_SNIPPET)
    assert "<!-- shared-memory:mcp-constitution-snippet v2 -->" in text, (
        "the MCP snippet's marker did not advance to v2 — an installed block "
        "will not read as drifted and Phase 8c will not re-propose it")


def test_all_three_surfaces_name_the_history_trigger():
    """Assert the VALUE, not just that some edit happened — the phrase must be
    present verbatim (case-sensitive) so an agent reading any one surface sees
    the same trigger category the other two carry. Whitespace-normalised: the
    three files wrap prose at different widths, so the phrase can straddle a
    newline in one and not another — a check that a reflow could break would
    be measuring line length, not content."""
    def _flat(path):
        return re.sub(r"\s+", " ", _read(path))

    missing = [path for path in (_CLI_SNIPPET, _MCP_SNIPPET, _SYSTEM_PROMPT)
               if _PHRASE not in _flat(path)]
    assert not missing, (
        f"{_PHRASE!r} missing from: {missing} — the history-question trigger "
        "must appear on every surface, in the same words")


def test_the_two_tracked_cli_snippet_copies_stay_byte_identical():
    """The source (`shared-memory/CONSTITUTION_SNIPPET.md`) and the tracked
    shipped copy (`shared-memory-skill/shared-memory/CONSTITUTION_SNIPPET.md`)
    must be byte-identical — sync_skills.sh is not this change's to run, so the
    tracked copy has to already match by hand."""
    assert _read(_CLI_SNIPPET) == _read(_CLI_SNIPPET_SHIPPED), (
        "the source CLI snippet and its tracked shipped copy have diverged — "
        "make the shipped copy byte-identical to the source")
