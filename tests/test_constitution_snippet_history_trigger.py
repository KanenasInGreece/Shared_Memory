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


def _shipped_text(path: str) -> str:
    """What an agent actually reads.

    A snippet file is mostly an HTML comment addressed to whoever installs it;
    only the marker-delimited block is pasted into a constitution. Reading the
    whole file let a rule live in the version-history comment and still satisfy
    these checks -- measured on the MCP snippet at v7, where "is a pointer, not
    the record" was in the header and absent from the block. system-prompt.md
    has no markers: the whole file is the surface.
    """
    text = _read(path)
    for marker in ("<!-- shared-memory:mcp-constitution-snippet",
                   "<!-- shared-memory:constitution-snippet"):
        if marker in text:
            after = text.split(marker, 1)[1]
            body = after.split("-->", 1)[1]
            return body.split("<!-- /shared-memory:", 1)[0]
    return text


def test_the_cli_snippet_carries_v7():
    text = _read(_CLI_SNIPPET)
    assert "<!-- shared-memory:constitution-snippet v7 -->" in text, (
        "the CLI snippet's marker did not advance to v7 — an installed block "
        "will not read as drifted and Phase 8c will not re-propose it")


def test_the_mcp_snippet_carries_v9():
    text = _read(_MCP_SNIPPET)
    assert "<!-- shared-memory:mcp-constitution-snippet v9 -->" in text, (
        "the MCP snippet's marker did not advance to v9 — an installed block "
        "will not read as drifted and Phase 8c will not re-propose it")


def test_all_three_surfaces_name_the_history_trigger():
    """Assert the VALUE, not just that some edit happened — the phrase must be
    present verbatim (case-sensitive) so an agent reading any one surface sees
    the same trigger category the other two carry. Whitespace-normalised: the
    three files wrap prose at different widths, so the phrase can straddle a
    newline in one and not another — a check that a reflow could break would
    be measuring line length, not content."""
    def _flat(path):
        return re.sub(r"\s+", " ", _shipped_text(path))

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


_POINTER_PHRASE = "is a pointer, not the record"
_REPAIR_PHRASE = "rewrite the index line to the current id"


def test_all_three_surfaces_name_the_index_pointer_rule():
    """v4 (2026-08-27): an agent cited a superseded fact id from a preloaded
    local index without searching, although the store held the current record
    and filters superseded ones from search. The rule must read the same on
    every surface; whitespace-normalised for the same reason as above."""
    def _flat(path):
        return re.sub(r"\s+", " ", _shipped_text(path))

    missing = [path for path in (_CLI_SNIPPET, _MCP_SNIPPET, _SYSTEM_PROMPT)
               if _POINTER_PHRASE not in _flat(path)]
    assert not missing, (
        f"{_POINTER_PHRASE!r} missing from: {missing} — the index-pointer rule "
        "must appear on every surface, in the same words")


def test_all_three_surfaces_prescribe_the_index_repair():
    """v5 (2026-08-27): checking a stale pointer is not enough — the surfaces
    must prescribe following superseded_by to the current record and rewriting
    the index line, or the next invocation repeats the same wrong answer."""
    def _flat(path):
        return re.sub(r"\s+", " ", _shipped_text(path))

    missing = [path for path in (_CLI_SNIPPET, _MCP_SNIPPET, _SYSTEM_PROMPT)
               if _REPAIR_PHRASE not in _flat(path)]
    assert not missing, (
        f"{_REPAIR_PHRASE!r} missing from: {missing} — the index-repair rule "
        "must appear on every surface, in the same words")


_HOOK = "Any entry can be expanded by its ref, and a pointer entry must be."

# decision:3501: one definition of the entry, shared by SKILL.md and the three
# MCP read tools. Backticks are the skill's index style; the words are the contract.
# The skill names search --ref. The MCP docstrings name the tools.
_DEFINITION = (
    "text_is summary is a whole shorter summary of the record, including a "
    "decision's rationale. text_is full_record is the whole record. text_is "
    "pointer has no text; the ref is the only context, so expand it with "
    "read_with. full_record carries ref, chars and read_with. A relation names "
    "from, the relation and to, the stored edge direction, and record is the "
    "far end. A null end is a record this caller cannot read. Search never "
    "returns superseded or reversed records. Explicit reads show them with "
    "obsolete and superseded_by. retired_grounds on a decision hit means its "
    "evidence was retired: read the successor before relying on it. "
    "latest_verdict on a retrospective names the verdict that counts most."
)

_SKILL = os.path.join(_REPO, "shared-memory", "SKILL.md")
_SKILL_SHIPPED = os.path.join(
    _REPO, "shared-memory-skill", "shared-memory", "SKILL.md")
_USAGE = os.path.join(_REPO, "shared-memory", "USAGE.md")
_USAGE_SHIPPED = os.path.join(
    _REPO, "shared-memory-skill", "shared-memory", "USAGE.md")
_VECTOR = os.path.join(_REPO, "mcp", "vector-skill.py")

_REMOVED_KEYS = ("content_truncated", "rel_type", "content_chars", "adr_props")


def _flat_words(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("`", ""))


def test_f11_skill_and_mcp_docstrings_share_the_read_definition():
    """F11: the docs surfaces describe different contracts (the words differ)."""
    import ast

    skill = _flat_words(_read(_SKILL))
    assert _DEFINITION in skill, "SKILL.md dropped the shared read definition"
    assert "Reading is two steps" in skill
    assert "search --ref" in skill
    assert _HOOK in skill
    assert _flat_words(_read(_SKILL_SHIPPED)) == skill, (
        "the shipped SKILL.md does not carry the same definition")
    tree = ast.parse(_read(_VECTOR))
    docs = {}
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name in (
                "hybrid_search_and_rerank", "record_lineage", "graph_query"):
            docs[node.name] = _flat_words(ast.get_docstring(node) or "")
    missing = [name for name in (
        "hybrid_search_and_rerank", "record_lineage", "graph_query")
        if _DEFINITION not in docs.get(name, "")
        or "search --ref" in docs.get(name, "")
        or "hybrid_search_and_rerank(refs=[" not in docs.get(name, "")
        or "record_lineage" not in docs.get(name, "")
        or "graph_query" not in docs.get(name, "")]
    assert not missing, (
        f"MCP docstring missing the shared definition: {missing}")


def test_f11_constitution_surfaces_share_the_expand_hook():
    """F11: a pointer entry is optional when one surface omits the expand hook."""
    def _flat(path):
        return re.sub(r"\s+", " ", _shipped_text(path))

    missing = [path for path in (_CLI_SNIPPET, _MCP_SNIPPET, _SYSTEM_PROMPT)
               if _HOOK not in _flat(path)]
    assert not missing, (
        f"expand hook missing from: {missing} — decision:3501 says any entry "
        "can be expanded by its ref, and a pointer entry must be")


def test_f11_usage_says_related_is_absent_on_a_summary():
    """F11: removing the summary-lineage clause leaves both USAGE copies saying a summary has related."""
    clause = "`related` is absent on a summary"
    missing = [path for path in (_USAGE, _USAGE_SHIPPED) if clause not in _read(path)]
    assert not missing, f"USAGE dropped the summary lineage clause: {missing}"


def test_f11_mcp_read_docstrings_name_the_two_steps_and_stay_wrapped():
    """F11: an MCP read tool omits the two-step reading line or the expand hook, or a docstring line runs past the wrap."""
    import ast

    text = _read(_VECTOR)
    lines = text.splitlines()
    tree = ast.parse(text)
    missing = []
    long_lines = []
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if node.name not in (
                "hybrid_search_and_rerank", "record_lineage", "graph_query"):
            continue
        doc = _flat_words(ast.get_docstring(node) or "")
        if "Reading is two steps" not in doc or _HOOK not in doc:
            missing.append(node.name)
        doc_node = node.body[0]
        for line in lines[doc_node.lineno - 1:doc_node.end_lineno]:
            if len(line) > 96:
                long_lines.append((node.name, doc_node.lineno, len(line)))
    assert not missing, f"MCP read docstring missing the two steps or the hook: {missing}"
    assert not long_lines, f"MCP read docstring line over the wrap: {long_lines}"


def test_f11_skill_says_paths_and_maps_resolve_like_nodes():
    """F11: SKILL.md still says a path and a map resolve, without saying which nodes that reaches."""
    clause = (
        "On /memory/graph, nodes inside paths and maps are resolved like any node. "
        "Only a projected property is not the record."
    )
    for path in (_SKILL, _SKILL_SHIPPED):
        flat = _flat_words(_read(path))
        assert clause in flat, path
        assert "A path and a map resolve." not in flat


def test_f11_usage_names_successor_visibility_on_stale_and_retired():
    """F11: USAGE still shows superseded_by on stale_sources and retired_summaries when the successor is not readable."""
    clause = "carry `superseded_by` only when the successor is readable"
    missing = [path for path in (_USAGE, _USAGE_SHIPPED) if clause not in _read(path)]
    assert not missing, missing


def test_f11_removed_read_keys_are_absent_from_the_doc_surfaces():
    """F11: a removed key still names the old contract on a doc surface."""
    files = (_SKILL, _SKILL_SHIPPED, _USAGE, _USAGE_SHIPPED, _SYSTEM_PROMPT,
             _CLI_SNIPPET, _CLI_SNIPPET_SHIPPED, _MCP_SNIPPET)
    bad = []
    for path in files:
        # The splice marker and the file names are the block's identity.
        text = _read(path).replace("constitution-snippet", "")
        text = text.replace("constitution_snippet", "")
        text = text.replace("CONSTITUTION_SNIPPET", "")
        if "snippet" in text:
            bad.append((path, "snippet"))
        for key in _REMOVED_KEYS:
            if key in text:
                bad.append((path, key))
    assert not bad, f"removed read keys still named: {bad}"
