import pytest
import json
import gzip
import os
import importlib.util
import sys
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

# ── module loaders ────────────────────────────────────────────────────────────

_MB_MODULE = "memory_bridge_logging_test"

def load_memory_bridge():
    path = os.path.join(
        os.path.dirname(__file__),
        "..", "shared-memory-skill", "shared-memory", "scripts", "memory_bridge.py"
    )
    spec = importlib.util.spec_from_file_location(_MB_MODULE, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MB_MODULE] = mod
    spec.loader.exec_module(mod)
    return mod

def load_consolidation_loop():
    path = os.path.join(
        os.path.dirname(__file__),
        "..", "shared-memory", "scripts", "consolidation_loop.py"
    )
    spec = importlib.util.spec_from_file_location("consolidation_loop_logging_test", path)
    mod = importlib.util.module_from_spec(spec)
    with patch("neo4j.GraphDatabase.driver"):
        spec.loader.exec_module(mod)
    return mod

memory_bridge = load_memory_bridge()
consolidation_loop = load_consolidation_loop()

MOCK_EMBEDDING = [0.1] * 1024
MOCK_PG_ID = 42

# ── _append_log unit tests ────────────────────────────────────────────────────

class TestAppendLog:
    def test_no_log_when_level_zero(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMORY_LOG_LEVEL", "0")
        monkeypatch.setenv("MEMORY_LOG_PATH", str(tmp_path))
        memory_bridge._append_log("memory_bridge", 1, "no_entities", {"pg_id": 1})
        assert not (tmp_path / "memory_bridge.log").exists()

    def test_warn_logged_at_level_1(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMORY_LOG_LEVEL", "1")
        monkeypatch.setenv("MEMORY_LOG_PATH", str(tmp_path))
        memory_bridge._append_log("memory_bridge", 1, "no_entities", {"pg_id": 7})
        log_path = tmp_path / "memory_bridge.log"
        assert log_path.exists()
        entry = json.loads(log_path.read_text().strip())
        assert entry["event"] == "no_entities"
        assert entry["pg_id"] == 7
        assert "ts" in entry

    def test_error_not_logged_at_level_1(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMORY_LOG_LEVEL", "1")
        monkeypatch.setenv("MEMORY_LOG_PATH", str(tmp_path))
        memory_bridge._append_log("memory_bridge", 2, "gateway_down", {})
        assert not (tmp_path / "memory_bridge.log").exists()

    def test_error_logged_at_level_2(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMORY_LOG_LEVEL", "2")
        monkeypatch.setenv("MEMORY_LOG_PATH", str(tmp_path))
        memory_bridge._append_log("memory_bridge", 2, "gateway_down", {"info": "x"})
        entry = json.loads((tmp_path / "memory_bridge.log").read_text().strip())
        assert entry["event"] == "gateway_down"

    def test_warn_also_logged_at_level_2(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMORY_LOG_LEVEL", "2")
        monkeypatch.setenv("MEMORY_LOG_PATH", str(tmp_path))
        memory_bridge._append_log("memory_bridge", 1, "no_entities", {"pg_id": 5})
        assert (tmp_path / "memory_bridge.log").exists()

    def test_success_not_logged_at_level_2(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMORY_LOG_LEVEL", "2")
        monkeypatch.setenv("MEMORY_LOG_PATH", str(tmp_path))
        memory_bridge._append_log("memory_bridge", 3, "save_success", {"pg_id": 5})
        assert not (tmp_path / "memory_bridge.log").exists()

    def test_success_logged_at_level_3(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMORY_LOG_LEVEL", "3")
        monkeypatch.setenv("MEMORY_LOG_PATH", str(tmp_path))
        memory_bridge._append_log("memory_bridge", 3, "save_success", {"pg_id": 5})
        assert (tmp_path / "memory_bridge.log").exists()

    def test_level_4_includes_content(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMORY_LOG_LEVEL", "4")
        monkeypatch.setenv("MEMORY_LOG_PATH", str(tmp_path))
        memory_bridge._append_log("memory_bridge", 1, "no_entities", {"pg_id": 1}, content="hello world")
        entry = json.loads((tmp_path / "memory_bridge.log").read_text().strip())
        assert entry["content"] == "hello world"
        assert "content_size_warn" not in entry

    def test_level_4_size_warning_on_large_content(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMORY_LOG_LEVEL", "4")
        monkeypatch.setenv("MEMORY_LOG_PATH", str(tmp_path))
        memory_bridge._append_log("memory_bridge", 1, "no_entities", {"pg_id": 1}, content="x" * 20_000)
        entry = json.loads((tmp_path / "memory_bridge.log").read_text().strip())
        assert "content_size_warn" in entry

    def test_level_3_does_not_include_content(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMORY_LOG_LEVEL", "3")
        monkeypatch.setenv("MEMORY_LOG_PATH", str(tmp_path))
        memory_bridge._append_log("memory_bridge", 1, "no_entities", {"pg_id": 1}, content="hello")
        entry = json.loads((tmp_path / "memory_bridge.log").read_text().strip())
        assert "content" not in entry

    def test_multiple_calls_append_multiple_lines(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMORY_LOG_LEVEL", "1")
        monkeypatch.setenv("MEMORY_LOG_PATH", str(tmp_path))
        memory_bridge._append_log("memory_bridge", 1, "no_entities", {"pg_id": 1})
        memory_bridge._append_log("memory_bridge", 1, "no_entities", {"pg_id": 2})
        lines = (tmp_path / "memory_bridge.log").read_text().strip().split("\n")
        assert len(lines) == 2

    def test_creates_log_dir_if_missing(self, tmp_path, monkeypatch):
        nested = tmp_path / "deep" / "logs"
        monkeypatch.setenv("MEMORY_LOG_LEVEL", "1")
        monkeypatch.setenv("MEMORY_LOG_PATH", str(nested))
        memory_bridge._append_log("memory_bridge", 1, "no_entities", {"pg_id": 1})
        assert (nested / "memory_bridge.log").exists()

    def test_tool_name_determines_filename(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMORY_LOG_LEVEL", "1")
        monkeypatch.setenv("MEMORY_LOG_PATH", str(tmp_path))
        memory_bridge._append_log("vector_skill", 1, "no_entities", {"pg_id": 1})
        assert (tmp_path / "vector_skill.log").exists()
        assert not (tmp_path / "memory_bridge.log").exists()


# ── save_artifact logging integration ────────────────────────────────────────

@pytest.mark.asyncio
async def test_save_logs_gateway_down(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_LOG_LEVEL", "2")
    monkeypatch.setenv("MEMORY_LOG_PATH", str(tmp_path))
    with patch(f"{_MB_MODULE}.get_embedding", return_value=None):
        result = await memory_bridge.save_artifact("content", '{"source":"test"}')
    assert result["status"] == "error"
    entry = json.loads((tmp_path / "memory_bridge.log").read_text().strip())
    assert entry["event"] == "gateway_down"

@pytest.mark.asyncio
async def test_save_logs_bad_metadata_json(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_LOG_LEVEL", "2")
    monkeypatch.setenv("MEMORY_LOG_PATH", str(tmp_path))
    with patch(f"{_MB_MODULE}.get_embedding", return_value=MOCK_EMBEDDING):
        result = await memory_bridge.save_artifact("content", "not-json")
    assert result["status"] == "error"
    entry = json.loads((tmp_path / "memory_bridge.log").read_text().strip())
    assert entry["event"] == "bad_metadata"

@pytest.mark.asyncio
async def test_save_logs_bad_metadata_type(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_LOG_LEVEL", "2")
    monkeypatch.setenv("MEMORY_LOG_PATH", str(tmp_path))
    with patch(f"{_MB_MODULE}.get_embedding", return_value=MOCK_EMBEDDING):
        result = await memory_bridge.save_artifact("content", "[1,2,3]")
    assert result["status"] == "error"
    entry = json.loads((tmp_path / "memory_bridge.log").read_text().strip())
    assert entry["event"] == "bad_metadata_type"

@pytest.mark.asyncio
async def test_save_logs_no_entities_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_LOG_LEVEL", "1")
    monkeypatch.setenv("MEMORY_LOG_PATH", str(tmp_path))
    with patch(f"{_MB_MODULE}.get_embedding", return_value=MOCK_EMBEDDING), \
         patch("psycopg2.connect") as mock_pg, \
         patch("neo4j.GraphDatabase.driver"):
        mock_cur = mock_pg.return_value.cursor.return_value.__enter__.return_value
        mock_cur.fetchone.return_value = [MOCK_PG_ID]
        result = await memory_bridge.save_artifact("content", '{"source":"test"}')
    assert result["status"] == "success"
    entry = json.loads((tmp_path / "memory_bridge.log").read_text().strip())
    assert entry["event"] == "no_entities"
    assert entry["pg_id"] == MOCK_PG_ID

@pytest.mark.asyncio
async def test_save_logs_success_at_level_3(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_LOG_LEVEL", "3")
    monkeypatch.setenv("MEMORY_LOG_PATH", str(tmp_path))
    with patch(f"{_MB_MODULE}.get_embedding", return_value=MOCK_EMBEDDING), \
         patch("psycopg2.connect") as mock_pg, \
         patch("neo4j.GraphDatabase.driver"):
        mock_cur = mock_pg.return_value.cursor.return_value.__enter__.return_value
        mock_cur.fetchone.return_value = [MOCK_PG_ID]
        result = await memory_bridge.save_artifact("content", '{"source":"test","entities":["E1"]}')
    assert result["status"] == "success"
    lines = (tmp_path / "memory_bridge.log").read_text().strip().split("\n")
    events = [json.loads(l)["event"] for l in lines]
    assert "save_success" in events

@pytest.mark.asyncio
async def test_save_no_log_at_level_0(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_LOG_LEVEL", "0")
    monkeypatch.setenv("MEMORY_LOG_PATH", str(tmp_path))
    with patch(f"{_MB_MODULE}.get_embedding", return_value=None):
        await memory_bridge.save_artifact("content", '{"source":"test"}')
    assert not (tmp_path / "memory_bridge.log").exists()

@pytest.mark.asyncio
async def test_save_gateway_down_not_logged_at_level_1(tmp_path, monkeypatch):
    # gateway_down is an ERROR (min_level=2), should not appear at level 1
    monkeypatch.setenv("MEMORY_LOG_LEVEL", "1")
    monkeypatch.setenv("MEMORY_LOG_PATH", str(tmp_path))
    with patch(f"{_MB_MODULE}.get_embedding", return_value=None):
        await memory_bridge.save_artifact("content", '{"source":"test"}')
    assert not (tmp_path / "memory_bridge.log").exists()


# ── merge_logs unit tests ─────────────────────────────────────────────────────

def _write_log_entry(path, tool, event, date, hour=10, **kwargs):
    ts = datetime(date.year, date.month, date.day, hour, 0, 0).isoformat()
    line = json.dumps({"ts": ts, "tool": tool, "event": event, **kwargs}) + "\n"
    with open(path, "a") as f:
        f.write(line)

def _read_gz(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]

class TestMergeLogs:
    def test_basic_merge_creates_gz(self, tmp_path):
        yesterday = (datetime.now() - timedelta(days=1)).date()
        _write_log_entry(tmp_path / "memory_bridge.log", "memory_bridge", "no_entities", yesterday, pg_id=1)
        consolidation_loop.merge_logs(str(tmp_path))
        out = tmp_path / f"shared_memory_{yesterday}.log.gz"
        assert out.exists()
        entries = _read_gz(out)
        assert len(entries) == 1
        assert entries[0]["event"] == "no_entities"

    def test_multi_tool_entries_merged(self, tmp_path):
        yesterday = (datetime.now() - timedelta(days=1)).date()
        _write_log_entry(tmp_path / "memory_bridge.log", "memory_bridge", "save_success", yesterday, pg_id=1)
        _write_log_entry(tmp_path / "vector_skill.log", "vector_skill", "no_entities", yesterday, pg_id=2)
        consolidation_loop.merge_logs(str(tmp_path))
        entries = _read_gz(tmp_path / f"shared_memory_{yesterday}.log.gz")
        assert len(entries) == 2
        tools = {e["tool"] for e in entries}
        assert tools == {"memory_bridge", "vector_skill"}

    def test_malformed_lines_skipped(self, tmp_path):
        yesterday = (datetime.now() - timedelta(days=1)).date()
        log = tmp_path / "memory_bridge.log"
        log.write_text("not valid json\n")
        _write_log_entry(log, "memory_bridge", "save_success", yesterday, pg_id=1)
        consolidation_loop.merge_logs(str(tmp_path))
        entries = _read_gz(tmp_path / f"shared_memory_{yesterday}.log.gz")
        assert len(entries) == 1

    def test_empty_log_file_no_crash_no_output(self, tmp_path):
        (tmp_path / "memory_bridge.log").write_text("")
        consolidation_loop.merge_logs(str(tmp_path))
        assert not list(tmp_path.glob("shared_memory_*.log.gz"))

    def test_no_files_no_crash(self, tmp_path):
        consolidation_loop.merge_logs(str(tmp_path))  # must not raise

    def test_source_files_removed_after_merge(self, tmp_path):
        yesterday = (datetime.now() - timedelta(days=1)).date()
        _write_log_entry(tmp_path / "memory_bridge.log", "memory_bridge", "save_success", yesterday, pg_id=1)
        consolidation_loop.merge_logs(str(tmp_path))
        assert not (tmp_path / "memory_bridge.log").exists()
        assert not (tmp_path / "memory_bridge.log.rotating").exists()

    def test_appends_to_existing_archive(self, tmp_path):
        yesterday = (datetime.now() - timedelta(days=1)).date()
        out = tmp_path / f"shared_memory_{yesterday}.log.gz"
        # Pre-existing archive
        with gzip.open(out, "wt", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime(yesterday.year, yesterday.month, yesterday.day, 8).isoformat(),
                                "tool": "memory_bridge", "event": "old_event", "pg_id": 99}) + "\n")
        _write_log_entry(tmp_path / "memory_bridge.log", "memory_bridge", "new_event", yesterday, hour=12, pg_id=100)
        consolidation_loop.merge_logs(str(tmp_path))
        entries = _read_gz(out)
        assert len(entries) == 2
        assert {e["event"] for e in entries} == {"old_event", "new_event"}

    def test_sorted_by_timestamp(self, tmp_path):
        yesterday = (datetime.now() - timedelta(days=1)).date()
        log = tmp_path / "memory_bridge.log"
        # Write in reverse order
        _write_log_entry(log, "memory_bridge", "second", yesterday, hour=12)
        _write_log_entry(log, "memory_bridge", "first", yesterday, hour=8)
        consolidation_loop.merge_logs(str(tmp_path))
        entries = _read_gz(tmp_path / f"shared_memory_{yesterday}.log.gz")
        assert [e["event"] for e in entries] == ["first", "second"]

    def test_multi_date_entries_go_to_separate_files(self, tmp_path):
        day1 = (datetime.now() - timedelta(days=2)).date()
        day2 = (datetime.now() - timedelta(days=1)).date()
        log = tmp_path / "memory_bridge.log"
        _write_log_entry(log, "memory_bridge", "event_day1", day1, pg_id=1)
        _write_log_entry(log, "memory_bridge", "event_day2", day2, pg_id=2)
        consolidation_loop.merge_logs(str(tmp_path))
        assert (tmp_path / f"shared_memory_{day1}.log.gz").exists()
        assert (tmp_path / f"shared_memory_{day2}.log.gz").exists()

    def test_rotating_file_cleaned_up_on_empty(self, tmp_path):
        (tmp_path / "memory_bridge.log").write_text("")
        consolidation_loop.merge_logs(str(tmp_path))
        assert not (tmp_path / "memory_bridge.log.rotating").exists()
