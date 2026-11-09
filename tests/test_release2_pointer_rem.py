"""Release 2 step 1: pointer first write and entry_text_is (shape only).

Each test names the failure it kills (decision:2671). Semantic proof of
invariants 1 and 7 is the throwaway graph; these pins keep merge_release.sh
on the shape.
"""
import ast
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "shared-memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rem_contract as rc  # noqa: E402
import coordinator as c  # noqa: E402


COORD_SRC = (SCRIPTS / "coordinator.py").read_text(encoding="utf-8")


def _fn_source(tree, name):
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) or isinstance(node, ast.FunctionDef):
            if node.name == name:
                return ast.get_source_segment(COORD_SRC, node)
        if isinstance(node, ast.ClassDef) and node.name == "MemoryCoordinator":
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name:
                    return ast.get_source_segment(COORD_SRC, item)
    raise AssertionError(f"function {name} not found")


_TREE = ast.parse(COORD_SRC)
_FACT_APPLY = _fn_source(_TREE, "_apply_outbox_row")
_RETRO_APPLY = _fn_source(_TREE, "_apply_retrospective_outbox_row")
_DECISION_APPLY = _fn_source(_TREE, "_apply_decision_outbox_row")


def test_inv1_fact_and_v2_retro_merge_set_no_content():
    """Kills: a first-write MERGE that SETs f.content or r.content (inv 1)."""
    assert "SET f.content" not in _FACT_APPLY
    assert "SET r.content" not in _RETRO_APPLY
    assert "r.content = $content" not in _RETRO_APPLY
    assert "f.content = $content" not in _FACT_APPLY


def test_inv7_entity_axis_attribution_clauses_remain_on_fact_merge():
    """Kills: dropping UNWIND $entities (or the attribution FOREACH) when content SET is removed (inv 7)."""
    assert "UNWIND $entities AS ename" in _FACT_APPLY
    assert "MERGE (e:" in _FACT_APPLY
    assert "was_attributed_to" in _FACT_APPLY
    assert "acted_on_behalf_of" in _FACT_APPLY
    assert "project_of" in _FACT_APPLY
    assert "UNWIND $entities" not in _RETRO_APPLY


def test_inv7_decision_title_has_no_snippet_fallback():
    """Kills: the content_snippet[:100] title fallback (fact:3547 ruling 1, §0.2-1)."""
    assert "[:100]" not in _DECISION_APPLY
    assert "content_snippet" not in _DECISION_APPLY


def test_rem_prompt_version_is_2():
    """Kills: shipping a prompt version other than the ruled int 2 (fact:1309, ruling E)."""
    assert rc.REM_PROMPT_VERSION == 2
    assert rc.REM_LEGACY_SUMMARY_CUT == 2000


def test_entry_text_is_missing_state_is_pointer():
    """Kills: a missing node_state for a fact or retrospective rendering as full_record (inv 6, Opus r2 F11)."""
    for kind in ("fact", "retrospective"):
        text_is, text = rc.entry_text_is(kind, None, "whole Postgres row")
        assert text_is == "pointer"
        assert text is None
        text_is, text = rc.entry_text_is(kind, {}, "whole Postgres row")
        assert text_is == "pointer"
        assert text is None
        unprocessed = {"rem_summary": "short", "rem_processed": False}
        text_is, text = rc.entry_text_is(kind, unprocessed, "whole Postgres row")
        assert text_is == "pointer"
        assert text is None


def test_entry_text_is_stamp_present_keeps_a_2000_char_summary():
    """Kills: treating a stamped 2,000-character summary as the legacy cut (inv 6, brief §0.2-3)."""
    body = "B" * 4000
    summary = "S" * 2000
    stamped = {"rem_summary": summary, "rem_processed": True, "rem_prompt_version": 2}
    text_is, text = rc.entry_text_is("fact", stamped, body)
    assert text_is == "summary"
    assert text == summary
    unstamped = {"rem_summary": summary, "rem_processed": True}
    text_is, text = rc.entry_text_is("fact", unstamped, body)
    assert text_is == "full_record"
    assert text == body


def test_entry_text_is_processed_without_shorter_summary_is_full_record():
    """Kills: showing a pointer after REM when the summary is not shorter (fact:3544)."""
    body = "whole record"
    processed = {"rem_processed": True, "rem_summary": "longer than the whole record xx"}
    text_is, text = rc.entry_text_is("fact", processed, body)
    assert text_is == "full_record"
    assert text == body
    none_summary = {"rem_processed": True, "rem_prompt_version": 2}
    text_is, text = rc.entry_text_is("retrospective", none_summary, body)
    assert text_is == "full_record"
    assert text == body


def test_entry_text_is_decision_summary_and_insight_unchanged():
    """Kills: routing a decision or a thematic summary through the fact pointer rule."""
    text_is, text = rc.entry_text_is("decision", None, "the rationale")
    assert text_is == "summary" and text == "the rationale"
    text_is, text = rc.entry_text_is("decision", None, "")
    assert text_is == "full_record" and text == ""
    text_is, text = rc.entry_text_is("summary", {"rem_processed": True}, "body")
    assert text_is == "pointer" and text is None
    text_is, text = rc.entry_text_is("insight", None, "body")
    assert text_is == "pointer" and text is None


def _row(kind="fact", **kw):
    return dict(id=1, type=kind, ref=f"{kind}:1", content="whole record",
                content_chars=12, readable=True,
                metadata={"source_ref": "discussion_context"}, **kw)


def test_render_record_entry_missing_state_is_pointer():
    """Kills: render_record_entry filling text from Postgres when Neo4j state is missing (inv 6)."""
    entry = c.render_record_entry(_row(), None, "fact:1", {})
    assert entry["text_is"] == "pointer"
    assert "text" not in entry
    assert entry["full_record"]["ref"] == "fact:1"
    entry = c.render_record_entry(_row("retrospective"), None, "retrospective:1", {})
    assert entry["text_is"] == "pointer"
    assert "text" not in entry


def test_render_record_entry_stamped_2000_is_summary():
    """Kills: render_record_entry ignoring rem_prompt_version on a 2,000-character summary (inv 6)."""
    d = _row()
    d["content"] = "a" * 4001
    d["content_chars"] = 4001
    state = {"rem_summary": "s" * 2000, "rem_processed": True, "rem_prompt_version": 2}
    entry = c.render_record_entry(d, state, "fact:1", {})
    assert entry["text_is"] == "summary"
    assert entry["text"] == "s" * 2000
    unstamped = {"rem_summary": "s" * 2000, "rem_processed": True}
    entry = c.render_record_entry(d, unstamped, "fact:1", {})
    assert entry["text_is"] == "full_record"
    assert entry["text"] == d["content"]
