"""Tests for memory_bridge.py — query subcommand (Phase D named Cypher templates)."""

import argparse
import asyncio
import json
import os
import sys
from unittest.mock import patch

import importlib.util
import pytest

# Dynamic import so the test file can live anywhere relative to the module.
_bridge_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts", "memory_bridge.py")
)
_spec = importlib.util.spec_from_file_location("memory_bridge_query", _bridge_path)
mb = importlib.util.module_from_spec(_spec)
sys.modules["memory_bridge_query"] = mb
_spec.loader.exec_module(mb)


def _ns(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


# ── Pure function tests ───────────────────────────────────────────────────────

def test_who_decided_builds_cypher():
    cypher = mb._build_query("who-decided", _ns(title="outbox", project=""))
    assert "WAS_ATTRIBUTED_TO" in cypher
    assert "outbox" in cypher


def test_agent_decisions_builds_cypher():
    cypher = mb._build_query("agent-decisions", _ns(assisted_by="claude", project=""))
    assert "WAS_ASSISTED_BY" in cypher
    assert "claude" in cypher


def test_retrospectives_builds_cypher():
    cypher = mb._build_query("retrospectives", _ns(rating="good"))
    assert "HAD_OUTCOME" in cypher
    assert "good" in cypher


def test_why_to_check_builds_cypher():
    cypher = mb._build_query("why-to-check", _ns(title="outbox", project=""))
    assert "HAD_OUTCOME" in cypher
    assert "WAS_ATTRIBUTED_TO" in cypher
    assert "outbox" in cypher


def test_retro_templates_read_both_shapes():
    """Retro payload lives on the record node post-conversion, on the edge in
    pre-conversion installs — the templates must read BOTH (the live regression:
    edge-only reads returned null rating/notes after the migration)."""
    for tpl in ("retrospectives", "why-to-check"):
        cypher = mb._build_query(tpl, _ns(rating="validated", title="", project=""))
        assert "t:Retrospective" in cypher
        assert "o.rating" in cypher
        assert "coalesce(t.rem_summary, t.content)" in cypher


def test_filter_values_are_sanitised():
    # ';' and "'" are stripped — only the two structural CONTAINS delimiters remain.
    cypher = mb._build_query("retrospectives", _ns(rating="good'; DROP TABLE"))
    assert ";" not in cypher
    assert cypher.count("'") == 2  # exactly the opening/closing pair around the value
    assert "good" in cypher


def test_unknown_template_exits_nonzero():
    with pytest.raises(SystemExit) as exc_info:
        mb._build_query("nonexistent", _ns())
    assert exc_info.value.code != 0


# ── CLI integration test ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_query_cli_forwards_cypher_to_query_graph():
    argv_backup = sys.argv
    try:
        sys.argv = [
            "memory_bridge.py", "query", "why-to-check",
            "--title", "outbox",
        ]
        with patch.object(mb, "query_graph", return_value=[]) as mock_graph:
            await mb.main()
        assert mock_graph.call_count == 1
        called_cypher = mock_graph.call_args[0][0]
        assert isinstance(called_cypher, str)
        assert len(called_cypher) > 0
    finally:
        sys.argv = argv_backup
