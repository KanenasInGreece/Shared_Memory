"""
Tests for memory_bridge.py Phase B — save_decision subcommand.

Coverage:
  - build_decision_metadata: required fields only → correct shape
  - build_decision_metadata: all optional fields parsed from comma strings
  - build_decision_metadata: date is valid ISO format
  - build_decision_metadata: empty/blank comma strings produce empty lists
  - save_decision CLI action: correct metadata forwarded to save_artifact
  - save_decision CLI action: missing required flag exits with non-zero status
"""

import importlib.util
import json
import os
import sys
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest


def load_memory_bridge():
    path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts", "memory_bridge.py")
    )
    spec = importlib.util.spec_from_file_location("memory_bridge_decision", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["memory_bridge_decision"] = mod
    spec.loader.exec_module(mod)
    return mod


mb = load_memory_bridge()


# ── build_decision_metadata ───────────────────────────────────────────────────

def test_required_fields_only():
    content, meta = mb.build_decision_metadata(
        title="Use asyncpg",
        decided_by="Xenofon",
        project="shared-memory",
        rationale="asyncpg is fully async; psycopg2 blocks the event loop",
    )
    assert content == "Use asyncpg\n\nasyncpg is fully async; psycopg2 blocks the event loop"
    assert meta["type"] == "decision"
    assert meta["decision"]["title"] == "Use asyncpg"
    assert meta["decision"]["decided_by"] == "Xenofon"
    assert meta["decision"]["project"] == "shared-memory"
    assert meta["decision"]["rationale"] == "asyncpg is fully async; psycopg2 blocks the event loop"
    assert meta["entities"] == []
    assert "assisted_by" not in meta["decision"]
    assert "alternatives" not in meta["decision"]
    assert "confidence" not in meta["decision"]


def test_all_optional_fields():
    _, meta = mb.build_decision_metadata(
        title="Use asyncpg",
        decided_by="Xenofon",
        project="shared-memory",
        rationale="async I/O",
        source="claude-code",
        assisted_by="claude-sonnet-4-6, grok-3",
        alternatives=["psycopg2", "aiopg"],
        confidence="high",
        entities="asyncpg, PostgreSQL, SharedMemory",
    )
    assert meta["source"] == "claude-code"
    assert meta["decision"]["assisted_by"] == ["claude-sonnet-4-6", "grok-3"]
    assert meta["decision"]["alternatives"] == ["psycopg2", "aiopg"]
    assert meta["decision"]["confidence"] == "high"
    assert meta["entities"] == ["asyncpg", "PostgreSQL", "SharedMemory"]


# ── alternatives are stored VERBATIM — the capture surface must not shred ─────
#
# `--alternatives` used to split on ",". A well-written alternative contains
# commas, so 46 of the 217 decisions carrying alternatives ended up holding
# fragments that do not stand alone — in Postgres AND in the graph's ADR
# properties, with no warning and no way to tell afterwards which pieces had
# once been one entry.

def test_an_alternative_containing_a_comma_survives_as_one_entry():
    """The defect, stated as the test that would have caught it. This exact
    shape (pg_id 194) was stored as two meaningless halves."""
    alt = "use explicit Neo4j transactions for atomicity (APOC not available, auto-commit is the existing pattern)"
    _, meta = mb.build_decision_metadata(
        title="T", decided_by="X", project="P", rationale="R",
        alternatives=[alt],
    )
    assert meta["decision"]["alternatives"] == [alt]


def test_a_lone_string_is_one_alternative_not_a_list_to_split():
    """Under-splitting is the safe direction: a caller who passes one string
    gets exactly what they typed. Splitting would invent options nobody wrote."""
    assert mb.alternatives_list("a, b, c") == ["a, b, c"]


def test_repeating_the_flag_is_what_produces_several_alternatives():
    assert mb.alternatives_list(["first (with, commas)", "second"]) == [
        "first (with, commas)", "second"]


def test_blank_and_absent_alternatives_are_an_absence():
    assert mb.alternatives_list(None) == []
    assert mb.alternatives_list("") == []
    assert mb.alternatives_list(["", "  ", "real"]) == ["real"]


def test_date_is_valid_iso():
    _, meta = mb.build_decision_metadata(
        title="T", decided_by="X", project="P", rationale="R"
    )
    date.fromisoformat(meta["decision"]["date"])  # raises ValueError if invalid


def test_empty_comma_strings_produce_empty_lists():
    _, meta = mb.build_decision_metadata(
        title="T", decided_by="X", project="P", rationale="R",
        assisted_by="",
        alternatives=None,
        entities=",,,",
    )
    assert meta["entities"] == []
    assert "assisted_by" not in meta["decision"]
    assert "alternatives" not in meta["decision"]


def test_source_defaults_to_agent_id(monkeypatch):
    monkeypatch.setattr(mb, "AGENT_ID", "test_agent")
    _, meta = mb.build_decision_metadata(
        title="T", decided_by="X", project="P", rationale="R"
    )
    assert meta["source"] == "test_agent"


# ── CLI integration ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_save_decision_cli_forwards_correct_metadata(capsys):
    captured = {}

    async def mock_save(content, metadata):
        captured["content"] = content
        captured["metadata"] = metadata
        return {"status": "success", "pg_id": 99}

    argv_backup = sys.argv
    try:
        sys.argv = [
            "memory_bridge.py", "save_decision",
            "--title", "Use asyncpg over psycopg2",
            "--decided-by", "Xenofon",
            "--project", "shared-memory",
            "--rationale", "asyncpg does not block",
            "--assisted-by", "claude-sonnet-4-6",
            "--confidence", "high",
            "--entities", "asyncpg,PostgreSQL",
        ]
        with patch.object(mb, "save_artifact", side_effect=mock_save):
            await mb.main()
    finally:
        sys.argv = argv_backup

    assert captured["metadata"]["type"] == "decision"
    assert captured["metadata"]["decision"]["title"] == "Use asyncpg over psycopg2"
    assert captured["metadata"]["decision"]["decided_by"] == "Xenofon"
    assert captured["metadata"]["decision"]["confidence"] == "high"
    assert "asyncpg" in captured["metadata"]["entities"]
    assert "Use asyncpg over psycopg2" in captured["content"]


def test_save_decision_cli_missing_required_flag_exits():
    argv_backup = sys.argv
    try:
        sys.argv = [
            "memory_bridge.py", "save_decision",
            "--title", "Only title",
            # missing --decided-by, --project, --rationale
        ]
        with pytest.raises(SystemExit) as exc_info:
            import asyncio
            asyncio.run(mb.main())
    finally:
        sys.argv = argv_backup
    assert exc_info.value.code != 0
