"""
Tests for memory_bridge.py — save_retrospective subcommand (v2 retro-as-record).

Coverage:
  - build_retrospective_payload: correct shape with required fields
  - build_retrospective_payload: date defaults to ISO today when omitted
  - build_retrospective_payload: source defaults to AGENT_ID
  - build_retrospective_payload: grounded_in "pgid[:role]" grammar (same as save_decision)
  - build_retrospective_payload: source_ref / elicited passthrough; never sends entities
  - save_retrospective_artifact: client-side outcome-state enum rejection
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
        rating="validated",
        notes="Outbox-as-WAL held under concurrent load.",
    )
    assert payload["pg_id"] == 42
    assert payload["rating"] == "validated"
    assert payload["notes"] == "Outbox-as-WAL held under concurrent load."
    assert "date" in payload
    assert "agent_id" in payload


def test_rating_normalised_to_lowercase():
    payload = mb.build_retrospective_payload(pg_id=1, rating=" Validated ", notes="ok")
    assert payload["rating"] == "validated"


def test_date_defaults_to_today():
    payload = mb.build_retrospective_payload(pg_id=1, rating="mixed", notes="ok")
    date.fromisoformat(payload["date"])  # raises ValueError if invalid ISO


def test_explicit_date_preserved():
    payload = mb.build_retrospective_payload(
        pg_id=1, rating="reversed", notes="regressed", date="2026-01-15"
    )
    assert payload["date"] == "2026-01-15"


def test_source_defaults_to_agent_id(monkeypatch):
    monkeypatch.setattr(mb, "AGENT_ID", "test_agent")
    payload = mb.build_retrospective_payload(pg_id=1, rating="validated", notes="good")
    assert payload["agent_id"] == "test_agent"


def test_explicit_source_overrides_agent_id(monkeypatch):
    monkeypatch.setattr(mb, "AGENT_ID", "default_agent")
    payload = mb.build_retrospective_payload(
        pg_id=1, rating="validated", notes="good", source="claude_code"
    )
    assert payload["agent_id"] == "claude_code"


def test_grounded_in_grammar_ids_and_roles():
    """Same "pgid[:role],pgid" grammar as save_decision — the facts that
    MEASURED the outcome, with an optional per-fact role."""
    payload = mb.build_retrospective_payload(
        pg_id=1, rating="validated", notes="ok",
        grounded_in="601, 602:considered, junk, 603:BASED_ON",
    )
    assert payload["grounded_in"] == [601, 602, 603]
    assert payload["grounded_roles"] == {"602": "considered", "603": "based_on"}


def test_grounded_in_empty_omits_keys():
    payload = mb.build_retrospective_payload(pg_id=1, rating="pending", notes="ok")
    assert "grounded_in" not in payload
    assert "grounded_roles" not in payload


def test_source_ref_elicited_passthrough():
    payload = mb.build_retrospective_payload(
        pg_id=1, rating="validated", notes="ok",
        source_ref="tests/test_outbox_ledger.py",
        elicited=True,
    )
    assert payload["source_ref"] == "tests/test_outbox_ledger.py"
    assert payload["elicited"] is True


def test_retrospective_never_sends_entities_or_new_entities():
    """A retrospective names no entities of its own (decision:1664) — the
    payload never carries the key at all, not even empty, and the function no
    longer accepts one to send (v0.9.69)."""
    payload = mb.build_retrospective_payload(
        pg_id=1, rating="validated", notes="ok",
    )
    assert "entities" not in payload
    assert "new_entities" not in payload
    with pytest.raises(TypeError):
        mb.build_retrospective_payload(
            pg_id=1, rating="validated", notes="ok", entities="X",
        )


# ── client-side enum gate ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_save_retrospective_rejects_non_enum_rating():
    """The rating is a closed outcome-state enum — a free-text grade must be
    rejected client-side with the enum listed (friendlier than the 400)."""
    out = await mb.save_retrospective_artifact(pg_id=1, rating="high", notes="ok")
    assert out["status"] == "error"
    assert "validated" in out["message"] and "reversed" in out["message"]


# ── CLI integration ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_save_retrospective_cli_forwards_correct_payload(capsys):
    captured = {}

    async def mock_save(pg_id, rating, notes, date, source,
                        grounded_in, source_ref, elicited):
        captured.update(pg_id=pg_id, rating=rating, notes=notes, date=date,
                        source=source, grounded_in=grounded_in,
                        source_ref=source_ref, elicited=elicited)
        return {"status": "success", "pg_id": 91, "target_pg_id": pg_id}

    argv_backup = sys.argv
    try:
        sys.argv = [
            "memory_bridge.py", "save_retrospective",
            "--pg-id",  "42",
            "--rating", "validated",
            "--notes",  "Outbox atomicity held for 30 days in prod.",
            "--grounded-in", "601:based_on",
            "--source-ref", "tests/test_outbox_ledger.py",
            "--elicited",
            "--source", "claude_code",
        ]
        with patch.object(mb, "save_retrospective_artifact", side_effect=mock_save):
            await mb.main()
    finally:
        sys.argv = argv_backup

    assert captured["pg_id"]  == 42
    assert captured["rating"] == "validated"
    assert "30 days" in captured["notes"]
    assert captured["source"] == "claude_code"
    assert captured["grounded_in"] == "601:based_on"
    assert captured["source_ref"] == "tests/test_outbox_ledger.py"
    assert captured["elicited"] is True


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


def test_save_retrospective_cli_rejects_removed_entities_flag():
    """--entities was removed from the argparse surface (v0.9.69) — a caller
    still passing it must get argparse's own unknown-argument refusal."""
    argv_backup = sys.argv
    try:
        sys.argv = [
            "memory_bridge.py", "save_retrospective",
            "--pg-id", "42", "--rating", "validated", "--notes", "ok",
            "--grounded-in", "601", "--entities", "OutboxPattern",
        ]
        with pytest.raises(SystemExit) as exc_info:
            import asyncio
            asyncio.run(mb.main())
    finally:
        sys.argv = argv_backup
    assert exc_info.value.code != 0
