"""A backlog row stops the next call. llm_backlog is a real module over a memory connection.

These die when eligibility is ignored, the REM upsert is removed, the fold
upsert is removed, the fold key is stored raw, or the blocked list is empty.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import llm_backlog
importlib.reload(llm_backlog)


class _Mem:
    def __init__(self):
        self.rows = {}


class _Cur:
    def __init__(self, mem):
        self.mem = mem
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        if sql.lstrip().startswith("SELECT"):
            kind = params[0]
            self._rows = [
                (key, row[0], row[1], row[2], row[3], row[4])
                for (knd, key), row in self.mem.rows.items()
                if knd == kind
            ]
        elif sql.lstrip().startswith("INSERT"):
            kind, key, reason, ctx, reply, pool, boot, specimen = params
            self.mem.rows[(kind, str(key))] = (reason, ctx, reply, pool, boot, specimen)
        elif sql.lstrip().startswith("DELETE"):
            kind, key = params
            self.mem.rows.pop((kind, str(key)), None)

    def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, mem):
        self.mem = mem

    def cursor(self):
        return _Cur(self.mem)

    def close(self):
        return None

    def set_isolation_level(self, level):
        return None


def _arm_backlog(monkeypatch):
    importlib.reload(llm_backlog)
    mem = _Mem()

    def _connect():
        return _Conn(mem)

    monkeypatch.setattr(llm_backlog, "_connect", _connect)
    monkeypatch.setenv("SM_LLM_POOL_ID", "pool-a")
    monkeypatch.setenv("SM_LLM_LARGEST_CTX", "8192")
    monkeypatch.setenv("SM_LLM_LARGEST_REPLY", "2048")
    monkeypatch.setenv("SM_GATEWAY_BOOT_ID", "boot-a")
    return mem


class _Result:
    def __init__(self, rows):
        self._rows = rows

    async def data(self):
        return self._rows


class _Neo:
    def __init__(self, rows):
        self.rows = rows
        self.blocked_seen = []

    def session(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def run(self, query, **params):
        if "AS rem_invalid" in query:
            ids = list(params.get("ids") or [])
            return _Result([
                {"pg_id": i, "rem_processed": True, "superseded": False,
                 "rem_invalid": False}
                for i in ids
            ])
        if "NOT n.pg_id IN $blocked" in query:
            blocked = set(params.get("blocked") or [])
            self.blocked_seen.append(blocked)
            kept = [r for r in self.rows if r["pg_id"] not in blocked]
            return _Result(kept)
        return _Result([])


class _FakeCur:
    def execute(self, *a, **k): pass
    def fetchone(self): return (1,)
    def fetchall(self): return []
    def __enter__(self): return self
    def __exit__(self, *a): return False

class _FakeConn:
    def cursor(self): return _FakeCur()
    def close(self): pass


@pytest.mark.asyncio
async def test_rem_backlog_row_stops_the_second_call(monkeypatch):
    import rem_loop as rem
    from unittest.mock import AsyncMock

    _arm_backlog(monkeypatch)
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon = rem.REMDaemon.__new__(rem.REMDaemon)
    daemon.is_running = True
    daemon._solo_only = set()
    daemon._withheld = set()
    daemon._batch_paid = True
    daemon._last_llm_failure = None
    daemon._last_backlog = None
    daemon._batch_action = None
    daemon._batch_reason = None
    daemon._batch_specimen = ""
    long = "z" * (2004)
    neo = _Neo([{
        "pg_id": 8,
        "rem_attempts": 0,
        "rem_passed_over": 0,
        "labels": [rem.ONT.fact],
    }])
    daemon.driver = type("D", (), {"session": lambda self: neo})()
    calls = []

    async def _post(self, url, **kwargs):
        calls.append(1)

        class R:
            status_code = 200
            headers = {}
            text = ""

            def json(_self):
                return {"choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "{}"},
                }]}

        return R()

    monkeypatch.setattr("httpx.AsyncClient.post", _post)
    daemon._open_pg_conn = lambda: _FakeConn()
    daemon._recent_write_happened = AsyncMock(return_value=False)
    daemon._filter_applied_in_outbox = AsyncMock(side_effect=lambda ids, *a: list(ids))
    daemon._batch_fetch_content = AsyncMock(return_value={
        8: {"content": long, "kind": rem.KIND_FACT, "created_at": None},
    })
    daemon._bump_rem_pickups = AsyncMock()
    daemon._bump_rem_passed_over = AsyncMock()
    daemon._mark_node_invalid = AsyncMock()
    monkeypatch.setattr(rem, "_take_shared_backup_lock", lambda c: True)
    monkeypatch.setattr(rem, "pool_roster_state", AsyncMock(return_value="free"))
    monkeypatch.setattr(rem, "_nrem_is_queuing", lambda c: False)

    await daemon.run_cycle()
    assert calls == [1]
    assert "8" in llm_backlog.blocked_keys("rem")
    await daemon.run_cycle()
    assert calls == [1]
    assert neo.blocked_seen[-1] == {8}


@pytest.mark.asyncio
async def test_fold_backlog_row_stops_the_second_call(monkeypatch, caplog):
    import consolidation_loop as cl
    from unittest.mock import AsyncMock

    mem = _arm_backlog(monkeypatch)
    ids = [245, 267]
    types = {245: "Decision", 267: "Decision"}
    key = cl._fold_backlog_key(ids)

    class _Cur:
        def __init__(self):
            self.sql = ""
            self.rowcount = 0

        def execute(self, sql, params=None):
            self.sql = sql

        def fetchall(self):
            if "ORDER BY id" in self.sql:
                return [
                    (245, "Decision A", "p", "decision", {}),
                    (267, "Decision B", "p", "decision", {}),
                ]
            return []

        def fetchone(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Pg:
        def cursor(self):
            return _Cur()

        def commit(self):
            return None

        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: _Pg())
    monkeypatch.setattr(cl, "fetch_unreconciled_insights", lambda conn: [])
    monkeypatch.setattr(cl, "fetch_open_retro_record_ids", lambda conn: [])
    monkeypatch.setattr(cl, "fetch_refold_insights", lambda conn, ids: [])
    monkeypatch.setattr(cl, "fetch_active_insight_rows", lambda conn: [])
    monkeypatch.setattr(cl, "fetch_active_thematic_summary_id", lambda conn, p, d: None)
    monkeypatch.setattr(cl, "_fetch_outbox_created_at", lambda ids: {})
    monkeypatch.setattr(cl, "_crun_start", lambda ct: 42)
    monkeypatch.setattr(cl, "_crun_finish", lambda *a, **k: None)
    monkeypatch.setattr(cl, "fetch_reversal_context", lambda conn, ids: [])
    monkeypatch.setattr(cl, "fetch_insight_outbox_rows", lambda conn, ids: [])

    daemon = cl.ConsolidationDaemon()
    daemon.driver = _Neo([])
    daemon._find_fresh_insight_clusters = AsyncMock(return_value=[{
        "entity": "OutboxPattern",
        "decision_ids": ids,
        "judgement_ids": ids,
        "judgement_types": types,
        "projects": ["shared-memory-GitHub"],
        "domain": "architecture",
    }])
    folds = []

    async def _slots(self, *a, **k):
        folds.append(1)
        self._last_backlog_reason = "unparseable"
        self._last_backlog_specimen = ""
        return []

    daemon.generate_insight_slots = _slots.__get__(daemon, cl.ConsolidationDaemon)
    with caplog.at_level("ERROR"):
        await daemon.run_insight_cycle()
        await daemon.run_insight_cycle()
    assert folds == [1]
    assert "CRASHED" not in caplog.text
    assert "Insight cycle failed" not in caplog.text
    stored = [item_key for (kind, item_key) in mem.rows if kind == "fold"]
    assert stored == [key]
    assert key != cl._judgement_fold_identity(ids, types)


def _rem_ready(monkeypatch, pg_id, content, post):
    """One REM daemon over the memory backlog and a graph that honours blocks."""
    import rem_loop as rem
    from unittest.mock import AsyncMock

    mem = _arm_backlog(monkeypatch)
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon = rem.REMDaemon.__new__(rem.REMDaemon)
    daemon.is_running = True
    daemon._solo_only = set()
    daemon._withheld = set()
    daemon._batch_paid = True
    daemon._last_llm_failure = None
    daemon._last_backlog = None
    daemon._batch_action = None
    daemon._batch_reason = None
    daemon._batch_specimen = ""
    neo = _Neo([{
        "pg_id": pg_id,
        "rem_passed_over": 0,
        "labels": [rem.ONT.fact],
    }])
    daemon.driver = type("D", (), {"session": lambda self: neo})()
    monkeypatch.setattr("httpx.AsyncClient.post", post)
    daemon._open_pg_conn = lambda: _FakeConn()
    daemon._recent_write_happened = AsyncMock(return_value=False)
    daemon._filter_applied_in_outbox = AsyncMock(side_effect=lambda ids, *a: list(ids))
    daemon._batch_fetch_content = AsyncMock(return_value={
        pg_id: {"content": content, "kind": rem.KIND_FACT, "created_at": None},
    })
    daemon._bump_rem_pickups = AsyncMock()
    daemon._bump_rem_passed_over = AsyncMock()
    daemon._mark_node_invalid = AsyncMock()
    monkeypatch.setattr(rem, "_take_shared_backup_lock", lambda c: True)
    monkeypatch.setattr(rem, "pool_roster_state", AsyncMock(return_value="free"))
    monkeypatch.setattr(rem, "_nrem_is_queuing", lambda c: False)
    return daemon, mem


def _chat(content, usage=None):
    body = {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
    if usage is not None:
        body["usage"] = usage

    class R:
        status_code = 200
        headers = {}
        text = ""

        def json(_self):
            return body

    return R()


@pytest.mark.asyncio
async def test_non_string_summary_pays_once_across_three_cycles(monkeypatch):
    import rem_loop as rem

    calls = []

    async def _post(self, url, **kwargs):
        calls.append(1)
        return _chat('{"summary": 123}', usage={"completion_tokens": 2})

    long = "z" * (2004)
    daemon, mem = _rem_ready(monkeypatch, 8, long, _post)
    for _ in range(3):
        await daemon.run_cycle()
    assert calls == [1]
    assert mem.rows[("rem", "8")][0] == "unparseable"


@pytest.mark.asyncio
async def test_string_usage_pays_once(monkeypatch):
    import rem_loop as rem
    from unittest.mock import AsyncMock

    calls = []

    async def _post(self, url, **kwargs):
        calls.append(1)
        return _chat('{"summary": "fine"}', usage="nope")

    long = "z" * (2004)
    daemon, mem = _rem_ready(monkeypatch, 8, long, _post)
    neo = daemon.driver.session()
    writes = []

    async def _write(*args, **kwargs):
        writes.append(1)
        neo.rows.clear()

    daemon._write_neo4j_rem = _write
    daemon._fact_is_consistent = AsyncMock(return_value=True)
    daemon._mark_outbox_rem_reviewed = AsyncMock()
    daemon._notify_nrem = AsyncMock()
    for _ in range(3):
        await daemon.run_cycle()
    assert calls == [1]
    assert mem.rows == {}
    assert writes == [1]


@pytest.mark.asyncio
async def test_overflow_idx_does_not_escape_the_cycle(monkeypatch):
    import rem_loop as rem
    from unittest.mock import AsyncMock

    calls = []

    async def _post(self, url, **kwargs):
        calls.append(kwargs["json"]["messages"][1]["content"][:12])
        return _chat('{"idx":1e999}')

    mem = _arm_backlog(monkeypatch)
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon = rem.REMDaemon.__new__(rem.REMDaemon)
    daemon.is_running = True
    daemon._solo_only = set()
    daemon._withheld = set()
    daemon._batch_paid = True
    long = "z" * (2004)
    neo = _Neo([
        {"pg_id": 1, "rem_passed_over": 0, "labels": [rem.ONT.fact]},
        {"pg_id": 2, "rem_passed_over": 0, "labels": [rem.ONT.fact]},
    ])
    daemon.driver = type("D", (), {"session": lambda self: neo})()
    monkeypatch.setattr("httpx.AsyncClient.post", _post)
    daemon._open_pg_conn = lambda: _FakeConn()
    daemon._recent_write_happened = AsyncMock(return_value=False)
    daemon._filter_applied_in_outbox = AsyncMock(side_effect=lambda ids, *a: list(ids))
    daemon._batch_fetch_content = AsyncMock(return_value={
        1: {"content": long, "kind": rem.KIND_FACT, "created_at": None},
        2: {"content": long + "q", "kind": rem.KIND_FACT, "created_at": None},
    })
    daemon._bump_rem_pickups = AsyncMock()
    daemon._bump_rem_passed_over = AsyncMock()
    daemon._mark_node_invalid = AsyncMock()
    monkeypatch.setattr(rem, "_take_shared_backup_lock", lambda c: True)
    monkeypatch.setattr(rem, "pool_roster_state", AsyncMock(return_value="free"))
    monkeypatch.setattr(rem, "_nrem_is_queuing", lambda c: False)
    await daemon.run_cycle()
    await daemon.run_cycle()
    await daemon.run_cycle()
    assert len(calls) == 3
    assert mem.rows[("rem", "1")][0] == "unparseable"
    assert mem.rows[("rem", "2")][0] == "unparseable"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [501, 520])
async def test_rem_other_5xx_writes_no_row(monkeypatch, status_code):
    import rem_loop as rem

    calls = []

    async def _post(self, url, **kwargs):
        calls.append(status_code)

        class R:
            text = "upstream"
            headers = {}

            def json(_self):
                return {}

        # Assigned outside the class body. Inside it, the name is the
        # attribute being defined and the parameter is invisible.
        R.status_code = status_code
        return R()

    long = "z" * (2004)
    daemon, mem = _rem_ready(monkeypatch, 8, long, _post)
    await daemon.run_cycle()
    await daemon.run_cycle()
    assert calls == [status_code, status_code]
    assert mem.rows == {}
    assert daemon._last_backlog is None


@pytest.mark.asyncio
async def test_batch_422_cut_off_falls_back_to_solo(monkeypatch):
    import rem_loop as rem

    daemon, _ = _make_pair(monkeypatch)

    class R:
        status_code = 422
        headers = {"X-SM-Fault-Origin": "gateway"}
        text = ""

        def json(self):
            return {"error": "backlog", "reason": "cut_off", "specimen": "tail"}

    async def _post(self, url, **kwargs):
        return R()

    monkeypatch.setattr("httpx.AsyncClient.post", _post)
    long = "z" * (2004)
    await daemon._llm_process_batch([
        {"pg_id": 1, "content": long},
        {"pg_id": 2, "content": long},
    ])
    assert daemon._batch_action == "solo"


@pytest.mark.asyncio
async def test_missing_batch_fact_is_solo_only(monkeypatch):
    import rem_loop as rem
    from unittest.mock import AsyncMock

    prompts = []

    async def _post(self, url, **kwargs):
        prompts.append(kwargs["json"]["messages"][1]["content"])
        return _chat('{"idx":0,"summary":"kept"}')

    mem = _arm_backlog(monkeypatch)
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon = rem.REMDaemon.__new__(rem.REMDaemon)
    daemon.is_running = True
    daemon._solo_only = set()
    daemon._withheld = set()
    daemon._batch_paid = True
    long = "z" * (2004)
    neo = _Neo([
        {"pg_id": 1, "rem_passed_over": 0, "labels": [rem.ONT.fact]},
        {"pg_id": 2, "rem_passed_over": 0, "labels": [rem.ONT.fact]},
    ])
    daemon.driver = type("D", (), {"session": lambda self: neo})()
    monkeypatch.setattr("httpx.AsyncClient.post", _post)
    daemon._open_pg_conn = lambda: _FakeConn()
    daemon._recent_write_happened = AsyncMock(return_value=False)
    daemon._filter_applied_in_outbox = AsyncMock(side_effect=lambda ids, *a: list(ids))
    daemon._batch_fetch_content = AsyncMock(return_value={
        1: {"content": long, "kind": rem.KIND_FACT, "created_at": None},
        2: {"content": long + "q", "kind": rem.KIND_FACT, "created_at": None},
    })
    daemon._bump_rem_pickups = AsyncMock()
    daemon._write_neo4j_rem = AsyncMock()
    daemon._fact_is_consistent = AsyncMock(return_value=True)
    daemon._mark_outbox_rem_reviewed = AsyncMock()
    daemon._notify_nrem = AsyncMock()
    daemon._bump_rem_passed_over = AsyncMock()
    daemon._mark_node_invalid = AsyncMock()
    monkeypatch.setattr(rem, "_take_shared_backup_lock", lambda c: True)
    monkeypatch.setattr(rem, "pool_roster_state", AsyncMock(return_value="free"))
    monkeypatch.setattr(rem, "_nrem_is_queuing", lambda c: False)
    await daemon.run_cycle()
    assert 2 in daemon._solo_only
    assert 1 not in daemon._solo_only
    assert mem.rows == {}


@pytest.mark.asyncio
async def test_rem_success_deletes_the_backlog_row(monkeypatch):
    import rem_loop as rem
    from unittest.mock import AsyncMock

    async def _post(self, url, **kwargs):
        return _chat('{"summary": "stored"}')

    long = "z" * (2004)
    daemon, mem = _rem_ready(monkeypatch, 8, long, _post)
    # A different pool id makes the stored loop eligible. The same pool would withhold it.
    mem.rows[("rem", "8")] = ("loop", 1, 1, "pool-old", "boot-a", "")
    daemon._write_neo4j_rem = AsyncMock()
    daemon._fact_is_consistent = AsyncMock(return_value=True)
    daemon._mark_outbox_rem_reviewed = AsyncMock()
    daemon._notify_nrem = AsyncMock()
    await daemon.run_cycle()
    assert ("rem", "8") not in mem.rows


@pytest.mark.asyncio
async def test_rem_neo4j_failure_writes_write_failed(monkeypatch):
    import rem_loop as rem

    async def _post(self, url, **kwargs):
        return _chat('{"summary": "stored"}')

    async def _boom(*args, **kwargs):
        raise RuntimeError("bolt down")

    long = "z" * (2004)
    daemon, mem = _rem_ready(monkeypatch, 8, long, _post)
    daemon._write_neo4j_rem = _boom
    await daemon.run_cycle()
    assert mem.rows[("rem", "8")][0] == "write_failed"


def _make_pair(monkeypatch):
    import rem_loop as rem
    daemon = rem.REMDaemon.__new__(rem.REMDaemon)
    daemon._last_llm_failure = None
    daemon._batch_action = None
    daemon._batch_reason = None
    daemon._batch_specimen = ""
    return daemon, None


def _fold_daemon(monkeypatch, slots_http):
    """Insight cycle over the memory backlog. generate_insight_slots stays real."""
    import consolidation_loop as cl
    from unittest.mock import AsyncMock

    mem = _arm_backlog(monkeypatch)
    ids = [245, 267]
    types = {245: "decision", 267: "decision"}

    class _Cur:
        def __init__(self):
            self.sql = ""
            self.rowcount = 0

        def execute(self, sql, params=None):
            self.sql = sql

        def fetchall(self):
            if "ORDER BY id" in self.sql:
                return [
                    (245, "Decision A", "p", "decision", {}),
                    (267, "Decision B", "p", "decision", {}),
                ]
            return []

        def fetchone(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Pg:
        def __init__(self):
            self.rollbacks = 0

        def cursor(self):
            return _Cur()

        def commit(self):
            return None

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            return None

    pg = _Pg()
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: pg)
    monkeypatch.setattr(cl, "fetch_unreconciled_insights", lambda conn: [])
    monkeypatch.setattr(cl, "fetch_open_retro_record_ids", lambda conn: [])
    monkeypatch.setattr(cl, "fetch_refold_insights", lambda conn, ids: [])
    monkeypatch.setattr(cl, "fetch_active_insight_rows", lambda conn: [])
    monkeypatch.setattr(cl, "fetch_active_thematic_summary_id", lambda conn, p, d: None)
    monkeypatch.setattr(cl, "_fetch_outbox_created_at", lambda ids: {})
    monkeypatch.setattr(cl, "_crun_start", lambda ct: 42)
    monkeypatch.setattr(cl, "_crun_finish", lambda *a, **k: None)
    monkeypatch.setattr(cl, "fetch_reversal_context", lambda conn, ids: [])
    monkeypatch.setattr(cl, "fetch_insight_outbox_rows", lambda conn, ids: [])
    if slots_http is not None:
        monkeypatch.delenv("MOCK_LLM", raising=False)
        monkeypatch.setattr("httpx.AsyncClient.post", slots_http)
    daemon = cl.ConsolidationDaemon()
    daemon.driver = _Neo([])
    daemon._find_fresh_insight_clusters = AsyncMock(return_value=[{
        "entity": "OutboxPattern",
        "decision_ids": ids,
        "judgement_ids": ids,
        "judgement_types": types,
        "projects": ["shared-memory-GitHub"],
        "domain": "architecture",
    }])
    return daemon, mem, pg, cl._fold_backlog_key(ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [501, 520])
async def test_fold_other_5xx_writes_no_row(monkeypatch, status_code):
    calls = []

    async def _post(self, url, **kwargs):
        calls.append(status_code)

        class R:
            text = "upstream"
            headers = {}

            def json(_self):
                return {}

        R.status_code = status_code
        return R()

    daemon, mem, _pg, _key = _fold_daemon(monkeypatch, _post)
    await daemon.run_insight_cycle()
    await daemon.run_insight_cycle()
    assert calls == [status_code, status_code]
    assert mem.rows == {}


@pytest.mark.asyncio
async def test_fold_embed_failure_writes_write_failed(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, mem, _pg, key = _fold_daemon(monkeypatch, None)
    daemon.get_embedding = AsyncMock(return_value=None)
    calls = []
    real = daemon.generate_insight_slots

    async def _count(*args, **kwargs):
        calls.append(1)
        return await real(*args, **kwargs)

    daemon.generate_insight_slots = _count
    await daemon.run_insight_cycle()
    assert mem.rows[("fold", key)][0] == "write_failed"
    await daemon.run_insight_cycle()
    assert calls == [1]


@pytest.mark.asyncio
async def test_fold_write_failure_rolls_back_and_writes_write_failed(monkeypatch):
    from unittest.mock import AsyncMock
    import consolidation_loop as cl

    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, mem, pg, key = _fold_daemon(monkeypatch, None)
    daemon.get_embedding = AsyncMock(return_value=[0.1, 0.2])

    def _boom(*args, **kwargs):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(cl, "write_insight_summary", _boom)
    await daemon.run_insight_cycle()
    assert pg.rollbacks == 1
    assert mem.rows[("fold", key)][0] == "write_failed"


@pytest.mark.asyncio
async def test_refold_withheld_counts_as_dead_lettered(monkeypatch):
    import consolidation_loop as cl
    from unittest.mock import AsyncMock

    _arm_backlog(monkeypatch)
    ids = [11, 12]
    types = {11: "decision", 12: "retrospective"}
    key = cl._fold_backlog_key(ids)
    llm_backlog.upsert("fold", key, "loop", None)
    finish = {}

    class _Pg:
        def cursor(self):
            return self

        def execute(self, *a):
            return None

        def fetchall(self):
            return []

        def commit(self):
            return None

        def rollback(self):
            return None

        def close(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: _Pg())
    monkeypatch.setattr(cl, "fetch_unreconciled_insights", lambda conn: [])
    monkeypatch.setattr(cl, "fetch_open_retro_record_ids", lambda conn: [12])
    monkeypatch.setattr(cl, "fetch_refold_insights", lambda conn, retro: [
        (70, "OutboxPattern", ids, "old", {}),
    ])
    monkeypatch.setattr(cl, "fetch_active_insight_rows", lambda conn: [])
    monkeypatch.setattr(cl, "_fetch_outbox_created_at", lambda src: {})
    monkeypatch.setattr(cl, "_crun_start", lambda ct: 42)
    monkeypatch.setattr(cl, "_crun_finish", lambda *a, **k: finish.update(kwargs=k))
    monkeypatch.setattr(cl, "close_refold_ledger_rows", lambda conn, context: None)
    daemon = cl.ConsolidationDaemon()
    daemon.driver = _Neo([])
    daemon._find_fresh_insight_clusters = AsyncMock(return_value=[])
    daemon._fold_insight = AsyncMock(return_value=True)
    await daemon.run_insight_cycle()
    assert daemon._fold_insight.await_count == 0
    assert finish["kwargs"]["extra"]["dead_lettered_clusters"] == 1


def _status_chat(code, body):
    class R:
        status_code = code
        headers = {}
        text = "" if isinstance(body, dict) else str(body)

        def json(_self):
            if isinstance(body, dict):
                return body
            raise ValueError("not json")

    return R()


@pytest.mark.asyncio
async def test_backlog_insert_failure_withholds_paid_solo(monkeypatch, caplog):
    """SELECT works. INSERT raises. A paid solo {} is called once across three cycles."""
    import logging
    import rem_loop as rem

    calls = []

    async def _post(self, url, **kwargs):
        calls.append(1)
        return _chat("{}", usage={"completion_tokens": 2})

    long = "z" * (2004)
    daemon, mem = _rem_ready(monkeypatch, 8, long, _post)

    def _boom(*args, **kwargs):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(llm_backlog, "upsert", _boom)
    with caplog.at_level(logging.ERROR):
        for _ in range(3):
            await daemon.run_cycle()
    assert calls == [1]
    assert mem.rows == {}
    assert 8 in daemon._withheld
    assert any("backlog write failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_backlog_insert_failure_withholds_paid_fold(monkeypatch, caplog):
    """A paid fold with no SLOT is called once across three cycles when INSERT raises."""
    import logging

    calls = []

    async def _post(self, url, **kwargs):
        calls.append(1)
        return _chat("PRINCIPLE: only this")

    daemon, mem, _pg, key = _fold_daemon(monkeypatch, _post)

    def _boom(*args, **kwargs):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(llm_backlog, "upsert", _boom)
    with caplog.at_level(logging.ERROR):
        for _ in range(3):
            await daemon.run_insight_cycle()
    assert calls == [1]
    assert mem.rows == {}
    assert key in daemon._withheld_folds
    assert any("backlog write failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [402, 404, 413])
async def test_rem_free_4xx_queues_three_cycles(monkeypatch, code):
    import rem_loop as rem

    calls = []

    async def _post(self, url, **kwargs):
        calls.append(code)
        return _status_chat(code, {"error": "no credit"})

    long = "z" * (2004)
    daemon, mem = _rem_ready(monkeypatch, 8, long, _post)
    for _ in range(3):
        await daemon.run_cycle()
    assert calls == [code, code, code]
    assert mem.rows == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [402, 404, 413])
async def test_rem_4xx_with_usage_backlogs_connection_lost(monkeypatch, code):
    import rem_loop as rem

    calls = []

    async def _post(self, url, **kwargs):
        calls.append(code)
        return _status_chat(code, {"error": "no", "usage": {"completion_tokens": 1}})

    long = "z" * (2004)
    daemon, mem = _rem_ready(monkeypatch, 8, long, _post)
    for _ in range(3):
        await daemon.run_cycle()
    assert calls == [code]
    assert mem.rows[("rem", "8")][0] == "connection_lost"


@pytest.mark.asyncio
async def test_rem_html_200_queues(monkeypatch):
    import rem_loop as rem

    calls = []

    async def _post(self, url, **kwargs):
        calls.append(1)
        return _status_chat(200, "<html>maintenance</html>")

    long = "z" * (2004)
    daemon, mem = _rem_ready(monkeypatch, 8, long, _post)
    for _ in range(3):
        await daemon.run_cycle()
    assert calls == [1, 1, 1]
    assert mem.rows == {}


@pytest.mark.asyncio
async def test_rem_json_without_choices_is_unparseable(monkeypatch):
    import rem_loop as rem

    calls = []

    async def _post(self, url, **kwargs):
        calls.append(1)
        return _status_chat(200, {"usage": {"completion_tokens": 1}})

    long = "z" * (2004)
    daemon, mem = _rem_ready(monkeypatch, 8, long, _post)
    await daemon.run_cycle()
    await daemon.run_cycle()
    assert calls == [1]
    assert mem.rows[("rem", "8")][0] == "unparseable"


@pytest.mark.asyncio
async def test_rem_400_naming_context_length_queues(monkeypatch):
    """The daemon does not escalate. A free 400, even with those words, queues."""
    import rem_loop as rem

    calls = []

    async def _post(self, url, **kwargs):
        calls.append(1)
        return _status_chat(400, {"error": {"message": "maximum context length"}})

    long = "z" * (2004)
    daemon, mem = _rem_ready(monkeypatch, 8, long, _post)
    for _ in range(3):
        await daemon.run_cycle()
    assert calls == [1, 1, 1]
    assert mem.rows == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [402, 404, 413])
async def test_fold_free_4xx_queues_three_cycles(monkeypatch, code):
    calls = []

    async def _post(self, url, **kwargs):
        calls.append(code)
        return _status_chat(code, {"error": "no credit"})

    daemon, mem, _pg, _key = _fold_daemon(monkeypatch, _post)
    for _ in range(3):
        await daemon.run_insight_cycle()
    assert calls == [code, code, code]
    assert mem.rows == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [402, 404, 413])
async def test_fold_4xx_with_usage_backlogs_connection_lost(monkeypatch, code):
    calls = []

    async def _post(self, url, **kwargs):
        calls.append(code)
        return _status_chat(code, {"error": "no", "usage": "paid"})

    daemon, mem, _pg, key = _fold_daemon(monkeypatch, _post)
    for _ in range(3):
        await daemon.run_insight_cycle()
    assert calls == [code]
    assert mem.rows[("fold", key)][0] == "connection_lost"


@pytest.mark.asyncio
async def test_fold_html_200_queues(monkeypatch):
    calls = []

    async def _post(self, url, **kwargs):
        calls.append(1)
        return _status_chat(200, "<html>maintenance</html>")

    daemon, mem, _pg, _key = _fold_daemon(monkeypatch, _post)
    for _ in range(3):
        await daemon.run_insight_cycle()
    assert calls == [1, 1, 1]
    assert mem.rows == {}


@pytest.mark.asyncio
async def test_fold_json_without_choices_is_unparseable(monkeypatch):
    calls = []

    async def _post(self, url, **kwargs):
        calls.append(1)
        return _status_chat(200, {"usage": {"completion_tokens": 1}})

    daemon, mem, _pg, key = _fold_daemon(monkeypatch, _post)
    await daemon.run_insight_cycle()
    await daemon.run_insight_cycle()
    assert calls == [1]
    assert mem.rows[("fold", key)][0] == "unparseable"


@pytest.mark.asyncio
async def test_oversized_slot_id_backlogs_once(monkeypatch):
    calls = []
    digits = "1" * 5000

    async def _post(self, url, **kwargs):
        calls.append(1)
        return _chat(f"SLOT {digits}: text\nPRINCIPLE: kept")

    daemon, mem, _pg, key = _fold_daemon(monkeypatch, _post)
    for _ in range(3):
        await daemon.run_insight_cycle()
    assert calls == [1]
    assert mem.rows[("fold", key)][0] == "unparseable"


@pytest.mark.asyncio
async def test_failed_revert_retries_write_failed_on_a_new_boot(monkeypatch):
    """A revert that raises leaves rem_processed set. A new boot id clears it and retries."""
    import rem_loop as rem
    from unittest.mock import AsyncMock

    calls = []

    async def _post(self, url, **kwargs):
        calls.append(1)
        return _chat('{"summary": "stored"}', usage={"completion_tokens": 3})

    long = "z" * (2004)
    daemon, mem = _rem_ready(monkeypatch, 8, long, _post)
    state = {"processed": False, "reverts": 0}

    class _Graph:
        def session(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def run(self, query, **params):
            if "IN $ids" in query and "rem_processed = false" in query:
                for item in params.get("ids") or []:
                    if item == 8:
                        state["processed"] = False
                return _Result([])
            if "rem_processed = false" in query:
                state["reverts"] += 1
                raise RuntimeError("bolt")
            if "rem_processed = true" in query:
                state["processed"] = True
                return _Result([{"count": 1}])
            if "coalesce(n.rem_processed, false) = false" in query:
                blocked = set(params.get("blocked") or [])
                if state["processed"] or 8 in blocked:
                    return _Result([])
                return _Result([{
                    "pg_id": 8,
                    "rem_passed_over": 0,
                    "labels": [rem.ONT.fact],
                }])
            return _Result([])

    daemon.driver = _Graph()
    daemon._fact_is_consistent = AsyncMock(return_value=False)
    await daemon.run_cycle()
    assert calls == [1]
    assert state["processed"] is True
    assert state["reverts"] == 1
    assert mem.rows[("rem", "8")][0] == "write_failed"
    await daemon.run_cycle()
    assert calls == [1]
    monkeypatch.setenv("SM_GATEWAY_BOOT_ID", "boot-b")
    await daemon.run_cycle()
    assert calls == [1, 1]
    assert state["processed"] is True


def test_fold_key_is_the_sorted_pg_ids():
    import hashlib
    import consolidation_loop as cl

    assert cl._fold_backlog_key([900, 245]) == hashlib.sha256(b"245,900").hexdigest()


@pytest.mark.asyncio
async def test_mismatched_label_and_metadata_type_still_blocks(monkeypatch):
    from unittest.mock import AsyncMock

    calls = []

    async def _post(self, url, **kwargs):
        calls.append(1)
        return _chat("SLOT 245: a\nSLOT 267: b\nPRINCIPLE: c")

    daemon, _mem, _pg, key = _fold_daemon(monkeypatch, _post)
    llm_backlog.upsert("fold", key, "loop", None)
    daemon._find_fresh_insight_clusters = AsyncMock(return_value=[{
        "entity": "OutboxPattern",
        "decision_ids": [245, 267],
        "judgement_ids": [245, 267],
        "judgement_types": {245: "Retrospective", 267: "Decision"},
        "projects": ["shared-memory-GitHub"],
        "domain": "architecture",
    }])
    await daemon.run_insight_cycle()
    assert calls == []
