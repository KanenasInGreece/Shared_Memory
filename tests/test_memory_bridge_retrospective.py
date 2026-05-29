"""
Tests for memory_bridge.py Phase C — save_retrospective subcommand.

Coverage:
  - build_retrospective_payload: correct shape with required fields
  - build_retrospective_payload: date defaults to ISO today when omitted
  - build_retrospective_payload: source defaults to AGENT_ID
  - save_retrospective CLI action: correct payload forwarded to /memory/retrospective
  - save_retrospective CLI action: missing required flag exits with non-zero status
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
    spec = importlib.util.spec_from_file_location("memory_bridge_retrospective", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["memory_bridge_retrospective"] = mod
    spec.loader.exec_module(mod)
    return mod


mb = load_memory_bridge()


# ── build_retrospective_payload ───────────────────────────────────────────────

def test_required_fields_shape():
    payload = mb.build_retrospective_payload(
        pg_id=42,
        rating="high",
        notes="Outbox-as-WAL held under concurrent load.",
    )
    assert payload["pg_id"] == 42
    assert payload["rating"] == "high"
    assert payload["notes"] == "Outbox-as-WAL held under concurrent load."
    assert "date" in payload
    assert "agent_id" in payload


def test_date_defaults_to_today():
    payload = mb.build_retrospective_payload(pg_id=1, rating="medium", notes="ok")
    date.fromisoformat(payload["date"])  # raises ValueError if invalid ISO


def test_explicit_date_preserved():
    payload = mb.build_retrospective_payload(
        pg_id=1, rating="low", notes="regressed", date="2026-01-15"
    )
    assert payload["date"] == "2026-01-15"


def test_source_defaults_to_agent_id(monkeypatch):
    monkeypatch.setattr(mb, "AGENT_ID", "test_agent")
    payload = mb.build_retrospective_payload(pg_id=1, rating="high", notes="good")
    assert payload["agent_id"] == "test_agent"


def test_explicit_source_overrides_agent_id(monkeypatch):
    monkeypatch.setattr(mb, "AGENT_ID", "default_agent")
    payload = mb.build_retrospective_payload(
        pg_id=1, rating="high", notes="good", source="claude_code"
    )
    assert payload["agent_id"] == "claude_code"


# ── CLI integration ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_save_retrospective_cli_forwards_correct_payload(capsys):
    captured = {}

    async def mock_save(pg_id, rating, notes, date, source):
        captured["pg_id"]   = pg_id
        captured["rating"]  = rating
        captured["notes"]   = notes
        captured["date"]    = date
        captured["source"]  = source
        return {"status": "success", "target_pg_id": pg_id}

    argv_backup = sys.argv
    try:
        sys.argv = [
            "memory_bridge.py", "save_retrospective",
            "--pg-id",  "42",
            "--rating", "high",
            "--notes",  "Outbox atomicity held for 30 days in prod.",
            "--source", "claude_code",
        ]
        with patch.object(mb, "save_retrospective_artifact", side_effect=mock_save):
            await mb.main()
    finally:
        sys.argv = argv_backup

    assert captured["pg_id"]  == 42
    assert captured["rating"] == "high"
    assert "30 days" in captured["notes"]
    assert captured["source"] == "claude_code"


def test_save_retrospective_cli_missing_required_flag_exits():
    argv_backup = sys.argv
    try:
        sys.argv = [
            "memory_bridge.py", "save_retrospective",
            "--pg-id", "42",
            # missing --rating and --notes
        ]
        with pytest.raises(SystemExit) as exc_info:
            import asyncio
            asyncio.run(mb.main())
    finally:
        sys.argv = argv_backup
    assert exc_info.value.code != 0
