"""Release 2 steps 1 and 2: pointer first write, entry_text_is, REM summary contract.

Each test names the failure it kills (decision:2671). A test whose assertion
is only Cypher or source text is marked shape only. Semantic legs wait for
the rig in step 5.
"""
import ast
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "shared-memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rem_contract as rc  # noqa: E402
import coordinator as c  # noqa: E402
import llm_backlog  # noqa: E402
import rem_loop as rem  # noqa: E402


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


# ── Step 2: REM summary contract (inv 2, inv 3, inv 5) ───────────────────────

REM_SRC = (SCRIPTS / "rem_loop.py").read_text(encoding="utf-8")
_REM_TREE = ast.parse(REM_SRC)
BACKLOG_SRC = (SCRIPTS / "llm_backlog.py").read_text(encoding="utf-8")


def _rem_method(name):
    for node in _REM_TREE.body:
        if isinstance(node, ast.ClassDef) and node.name == "REMDaemon":
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name:
                    return ast.get_source_segment(REM_SRC, item)
    raise AssertionError(f"REMDaemon.{name} not found")


def _backlog_fn(name):
    tree = ast.parse(BACKLOG_SRC)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(BACKLOG_SRC, node)
    raise AssertionError(f"{name} not found")


class _Ctx:
    def __init__(self, val):
        self._val = val

    async def __aenter__(self):
        return self._val

    async def __aexit__(self, *_):
        return False


def _daemon(rows):
    d = rem.REMDaemon.__new__(rem.REMDaemon)
    d._withheld = set()
    d._solo_only = set()
    d._last_llm_failure = None
    d._last_backlog = None
    d._batch_action = None
    d._batch_paid = True
    result = MagicMock()
    result.data = AsyncMock(return_value=rows)
    session = AsyncMock()
    session.run = AsyncMock(return_value=result)
    driver = MagicMock()
    driver.session = MagicMock(return_value=_Ctx(session))
    d.driver = driver
    return d, session


def test_inv2_write_is_one_statement_with_stamp_and_no_cut():
    """Kills: a second statement, a [:2000] cut, or a missing prompt stamp (inv 2). Shape only."""
    src = _rem_method("_write_neo4j_rem")
    assert src.count("await session.run") == 1
    assert "rem_prompt_version" in src
    assert "RETURN count(n)" in src
    assert "rem_processed = true" in src
    assert "[:2000]" not in src
    assert "f.content" not in src
    assert "$orig" not in src
    assert ".content" not in src


@pytest.mark.asyncio
async def test_inv2_shorter_summary_is_stored_whole_and_equal_length_is_null():
    """Kills: cutting a shorter summary, or dropping the rem_summary SET when it is not shorter (inv 2)."""
    original = "o" * 9000
    summary = "s" * 5000
    daemon, session = _daemon([])
    await daemon._write_neo4j_rem(7, summary, original_content=original)
    assert session.run.await_count == 1
    call = session.run.await_args
    assert call.kwargs["summary"] == summary
    assert call.kwargs["v"] == 2
    assert "n.rem_summary = $summary" in call.args[0]

    original_eq = "o" * 40
    await daemon._write_neo4j_rem(7, "s" * 40, original_content=original_eq)
    call = session.run.await_args
    assert call.kwargs["summary"] is None
    assert "n.rem_summary = $summary" in call.args[0]

    await daemon._write_neo4j_rem(7, "s", original_content="longer than one")
    assert session.run.await_args.kwargs["summary"] == "s"


def test_inv3_selection_release_and_set_omit_decision():
    """Kills: OR n:Decision in selection, release, or the pickup SET (inv 3). Shape only."""
    for name in ("_fetch_non_rem_batch", "_release_eligible_write_failed", "_set_on_records"):
        src = _rem_method(name)
        assert "ONT.decision" not in src
        assert "Decision" not in src


@pytest.mark.asyncio
async def test_inv3_decision_write_and_apply_issue_nothing(monkeypatch):
    """Kills: a REM write or backlog row for a decision kind (inv 3, fact:3541)."""
    daemon, session = _daemon([])
    wrote = await daemon._write_neo4j_rem(
        7, "a summary", kind=rem.KIND_DECISION, original_content="rationale text")
    assert wrote == 0
    session.run.assert_not_awaited()

    reasons = []
    monkeypatch.setattr(
        rem.llm_backlog, "upsert",
        lambda *a, **k: reasons.append(a[2] if len(a) > 2 else k.get("reason")))
    ok = await daemon._apply_fact_result(
        7, rem.KIND_DECISION, {"summary": "a summary"}, MagicMock(),
        asyncio.get_running_loop(), original_content="rationale text")
    assert ok is False
    assert reasons == []
    session.run.assert_not_awaited()


def test_inv5_release_clears_only_a_current_stamp():
    """Kills: clearing rem_processed on every write_failed id regardless of stamp (inv 5). Shape only."""
    src = _rem_method("_release_eligible_write_failed")
    assert "coalesce(n.rem_prompt_version, 0) >= $v" in src
    assert "coalesce(n.rem_prompt_version, 0) < $v" in src
    assert "llm_backlog.delete" in src
    assert src.count("rem_processed = false") == 1
    assert "ONT.decision" not in src


def test_inv5_tier2_selection_excludes_write_failed_and_current_stamps():
    """Kills: a tier-2 match of a write_failed node or a current stamp (inv 5). Shape only."""
    src = _rem_method("_fetch_non_rem_batch")
    assert "NOT n.pg_id IN $write_failed" in src
    assert "coalesce(n.rem_prompt_version, 0) < $v" in src
    assert "NOT n.pg_id IN $blocked" in src


def test_inv5_batch_timing_omits_poll_ms_on_tier_2():
    """Kills: writing poll_ms on a tier-2 batch (brief §0.2-11). Shape only."""
    src = _rem_method("run_cycle")
    idx = src.index('timing["poll_ms"]')
    assert "if tier == 1" in src[max(0, idx - 120):idx]


@pytest.mark.asyncio
async def test_inv5_deferred_ids_join_blocked_before_limit():
    """Kills: filtering deferred outbox ids after LIMIT (inv 5, Codex R4-02)."""
    daemon, session = _daemon([
        {"pg_id": 1, "rem_passed_over": 0, "labels": [rem.ONT.fact]}])

    class _Cur:
        def __init__(self):
            self.sql = ""

        def execute(self, sql, params=None):
            self.sql = sql

        def fetchall(self):
            if "neo4j_outbox" in self.sql:
                return [(11,)]
            return []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    conn = MagicMock()
    conn.cursor.side_effect = lambda: _Cur()
    await daemon._fetch_non_rem_batch(conn)
    assert session.run.await_count == 1
    blocked = session.run.await_args.kwargs["blocked"]
    assert 11 in blocked


@pytest.mark.asyncio
async def test_inv5_tier2_count_zero_is_rewrite_failed(monkeypatch):
    """Kills: ignoring a zero count, or writing write_failed, or notifying, on a tier-2 miss (inv 5)."""
    daemon, session = _daemon([{"count": 0}])
    reasons = []
    monkeypatch.setattr(
        rem.llm_backlog, "upsert",
        lambda *a, **k: reasons.append(a[2]))
    daemon._notify_nrem = AsyncMock()
    daemon._revert_rem_mark = AsyncMock()
    ok = await daemon._apply_fact_result(
        7, rem.KIND_FACT, {"summary": "short"}, MagicMock(),
        asyncio.get_running_loop(), original_content="a much longer original", tier=2)
    assert ok is False
    assert reasons == ["rewrite_failed"]
    assert session.run.await_count == 1
    daemon._notify_nrem.assert_not_awaited()
    daemon._revert_rem_mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_inv5_tier2_write_exception_is_rewrite_failed(monkeypatch):
    """Kills: a tier-2 write exception that reverts or uses write_failed (inv 5)."""
    daemon, session = _daemon([])
    session.run = AsyncMock(side_effect=RuntimeError("neo4j down"))
    reasons = []
    monkeypatch.setattr(
        rem.llm_backlog, "upsert",
        lambda *a, **k: reasons.append(a[2]))
    daemon._revert_rem_mark = AsyncMock()
    ok = await daemon._apply_fact_result(
        7, rem.KIND_FACT, {"summary": "short"}, MagicMock(),
        asyncio.get_running_loop(), original_content="a much longer original", tier=2)
    assert ok is False
    assert reasons == ["rewrite_failed"]
    daemon._revert_rem_mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_inv5_tier1_count_zero_is_write_failed(monkeypatch):
    """Kills: a tier-1 miss that is not write_failed (D6-01)."""
    daemon, _session = _daemon([{"count": 0}])
    reasons = []
    monkeypatch.setattr(
        rem.llm_backlog, "upsert",
        lambda *a, **k: reasons.append(a[2]))
    ok = await daemon._apply_fact_result(
        7, rem.KIND_FACT, {"summary": "short"}, MagicMock(),
        asyncio.get_running_loop(), original_content="a much longer original", tier=1)
    assert ok is False
    assert reasons == ["write_failed"]


def test_inv5_rewrite_failed_retries_on_boot_change_only():
    """Kills: dropping rewrite_failed from the boot-scoped reasons (brief §3.2b)."""
    assert "rewrite_failed" in llm_backlog._BOOT_REASONS
    same = {"pool_id": "p", "largest_ctx": 1, "largest_reply": 1, "boot_id": "boot"}
    other = dict(same, boot_id="next")
    assert llm_backlog.eligible("rewrite_failed", 0, 0, "p", "boot", same) is False
    assert llm_backlog.eligible("rewrite_failed", 0, 0, "p", "boot", other) is True
    src = _backlog_fn("eligible_write_failed")
    assert "reason = 'write_failed'" in src
    assert "rewrite_failed" not in src


@pytest.mark.asyncio
async def test_inv5_tier2_unparseable_stays_the_tier1_reason(monkeypatch):
    """Kills: rewriting an LLM-level failure as rewrite_failed on tier 2 (brief §3.2)."""
    daemon, _session = _daemon([])
    reasons = []
    monkeypatch.setattr(
        rem.llm_backlog, "upsert",
        lambda *a, **k: reasons.append(a[2]))

    async def _llm(*_a, **_k):
        daemon._last_backlog = ("unparseable", "", True)
        daemon._last_llm_failure = rem.LLM_FAIL_PARSE
        return None, "m"

    daemon._llm_process = _llm
    ok = await daemon._process_fact(
        3, "content here", rem.KIND_FACT, MagicMock(),
        asyncio.get_running_loop(), tier=2)
    assert ok is False
    assert reasons == ["unparseable"]


@pytest.mark.asyncio
async def test_inv5_tier2_cycle_skips_outbox_filter_and_notify(monkeypatch):
    """Kills: a tier-2 cycle that filters the outbox or sends pg_notify (inv 5, §0.2-9)."""
    daemon = rem.REMDaemon.__new__(rem.REMDaemon)
    daemon._withheld = set()
    daemon._solo_only = set()
    daemon._last_llm_failure = None
    daemon._last_backlog = None
    daemon._batch_action = None
    daemon._batch_reason = None
    daemon._batch_specimen = ""
    daemon._batch_paid = True
    conn = MagicMock()
    monkeypatch.setattr(daemon, "_open_pg_conn", lambda: conn)
    monkeypatch.setattr(rem, "_take_shared_backup_lock", lambda _c: True)
    monkeypatch.setattr(rem, "_nrem_is_queuing", lambda _c: False)

    async def _pool(**_k):
        return "ready"

    monkeypatch.setattr(rem, "pool_roster_state", _pool)
    daemon._release_eligible_write_failed = AsyncMock()

    async def _fetch(_conn=None):
        daemon._cycle_tier = 2
        return (
            [1, 2],
            {1: rem.ONT.fact, 2: rem.ONT.fact},
            {1: 0, 2: 0},
        )

    daemon._fetch_non_rem_batch = _fetch
    daemon._recent_write_happened = AsyncMock(return_value=False)
    daemon._filter_applied_in_outbox = AsyncMock(
        side_effect=AssertionError("tier 2 filtered the outbox"))
    daemon._batch_fetch_content = AsyncMock(return_value={
        1: {"content": "alpha record text", "kind": "fact",
            "fact_kind": "discussion", "created_at": None},
        2: {"content": "beta record text", "kind": "fact",
            "fact_kind": "discussion", "created_at": None},
    })
    daemon._bump_rem_pickups = AsyncMock()
    daemon._llm_process_batch = AsyncMock(return_value=(
        {1: {"summary": "alpha"}, 2: {"summary": "beta"}},
        {"wall_ms": 1}, "m"))
    daemon._write_neo4j_rem = AsyncMock(return_value=1)
    daemon._notify_nrem = AsyncMock()
    daemon._write_audit_log = AsyncMock()
    daemon._clear_backlog = AsyncMock()
    daemon._write_rem_timing = AsyncMock()
    monkeypatch.setattr(rem, "AUDIT_LOG_PATH", None)

    processed, attempted = await daemon.run_cycle()
    assert processed == 2 and attempted == 2
    daemon._filter_applied_in_outbox.assert_not_awaited()
    daemon._notify_nrem.assert_not_awaited()
    timing = daemon._write_rem_timing.await_args_list[0].args[1]
    assert "poll_ms" not in timing


import logging

import insight_gate  # noqa: E402
import consolidation_loop as cl  # noqa: E402
import nrem_gate  # noqa: E402


def test_inv4_readiness_cypher_is_outgoing_and_not_the_stored_flag():
    """Kills: an incoming HAD_OUTCOME, a Decision blocker, or AS rem_invalid (inv 4). Shape only."""
    q = insight_gate.decision_readiness_cypher()
    assert "-[rel]->(b)" in q
    assert "<-" not in q
    assert "HAD_OUTCOME" in q
    assert "b:Decision" not in q
    assert "AS rem_invalid" not in q
    assert "d.rem_processed" not in q
    assert "AS ready" in q
    assert "blocking_ids" in q
    assert "spine_nodes" in q
    assert "b.pg_id IS NOT NULL" in q


def test_inv4_admit_drops_only_an_unconsolidated_not_ready_decision():
    """Kills: leaving a not-ready fresh decision in the gate set (inv 4, decision:3556)."""
    labels = {1: "Decision", 2: "Retrospective"}
    consolidated = {1: False, 2: True}
    components = [[1, 2]]
    readiness = [{
        "pg_id": 1, "ready": False,
        "blocking_ids": ["fact:4", "retrospective:5"], "spine_nodes": 1,
    }]
    new_labels, _cons, comps, excluded = insight_gate.admit_members(
        labels, consolidated, components, readiness)
    assert excluded == [{
        "pg_id": 1, "blocking_ids": ["fact:4", "retrospective:5"]}]
    assert 1 not in new_labels
    assert 2 in new_labels
    assert comps == [[2]]


def test_inv4_admit_keeps_an_admitted_decision_and_a_twin():
    """Kills: dropping an admitted decision, or a twin before the duplicate check (inv 4, decision:3582)."""
    labels = {1: "Decision", 2: "Decision", 3: "Retrospective"}
    consolidated = {1: True, 2: False, 3: False}
    components = [[1, 3], [2]]
    readiness = [
        {"pg_id": 1, "ready": False, "blocking_ids": ["fact:8"], "spine_nodes": 1},
        {"pg_id": 2, "ready": False, "blocking_ids": ["fact:9"], "spine_nodes": 2},
    ]
    new_labels, _cons, comps, excluded = insight_gate.admit_members(
        labels, consolidated, components, readiness)
    assert excluded == []
    assert new_labels == labels
    assert comps == components


def test_inv4_decision_presence_ignores_stored_rem_processed():
    """Kills: reading a Decision's stored rem_processed in refold checks 1 and 2 (inv 4)."""
    rows = [{
        "pg_id": 10, "rem_processed": False, "superseded": False,
        "labels": ["Decision"],
    }]
    unprocessed, duplicated = insight_gate.unprocessed_carried([10], rows)
    assert unprocessed == []
    assert duplicated == []


def test_inv4_decision_twins_still_defer():
    """Kills: treating two Decision nodes as one ready member (inv 4)."""
    rows = [
        {"pg_id": 10, "rem_processed": True, "labels": ["Decision"]},
        {"pg_id": 10, "rem_processed": True, "labels": ["Decision"]},
    ]
    unprocessed, duplicated = insight_gate.unprocessed_carried([10], rows)
    assert unprocessed == [10]
    assert duplicated == [10]


def test_inv4_decision_cycles_calls_admit_members_and_not_the_daemon():
    """Kills: decision_cycles counting a not-ready fresh decision, or importing consolidation_loop (inv 4). Shape only."""
    src = _fn_source(_TREE, "_nrem_cycle_counts")
    assert "admit_members(" in src
    assert "decision_readiness_cypher()" in src
    assert "from consolidation_loop import" not in src
    assert "import consolidation_loop" not in src
    telem = _fn_source(_TREE, "_build_telemetry")
    err = telem.index('snap["neo4j"] = {"error": str(exc)}')
    pend = telem.index('["decisions_rem_pending"]')
    assert err < pend
    assert "decision_readiness_cypher()" in telem[err:pend]


def test_inv6_shorter_summary_names_summary_and_ref():
    """Kills: a fold body that ignores entry_text_is and keeps the whole record (inv 6)."""
    state = {"rem_processed": True, "rem_summary": "short", "rem_prompt_version": 2}
    line = cl.fold_record_line(
        {"rtype": "fact", "kind": "observation", "recorded": "2026-01-01", "pg_id": 4},
        "the whole postgres text", state)
    assert "ref=fact:4" in line
    assert "text_is=summary" in line
    assert line.endswith("] short")
    assert "the whole postgres text" not in line


def test_inv6_no_summary_is_the_whole_postgres_text():
    """Kills: dropping the whole record when rem_summary is absent (inv 6)."""
    state = {"rem_processed": True, "rem_summary": None, "rem_prompt_version": 2}
    line = cl.fold_record_line(
        {"rtype": "fact", "kind": "observation", "recorded": "2026-01-01", "pg_id": 4},
        "the whole postgres text", state)
    assert "text_is=full_record" in line
    assert "ref=fact:4" in line
    assert line.endswith("] the whole postgres text")


def test_inv6_unstamped_2000_is_full_record_and_stamped_2000_is_summary():
    """Kills: keeping a stamp-less 2000-character summary, or discarding a stamped one (inv 6)."""
    body = "b" * 4000
    summary = "s" * 2000
    rec = {"rtype": "fact", "kind": "observation", "recorded": "d", "pg_id": 8}
    full = cl.fold_record_line(
        rec, body, {"rem_processed": True, "rem_summary": summary})
    summed = cl.fold_record_line(
        rec, body, {"rem_processed": True, "rem_summary": summary, "rem_prompt_version": 2})
    assert "text_is=full_record" in full
    assert full.endswith(body)
    assert "text_is=summary" in summed
    assert summed.endswith(summary)


def test_inv6_unprocessed_fact_is_a_pointer():
    """Kills: folding an unprocessed fact as full_record (inv 6)."""
    line = cl.fold_record_line(
        {"rtype": "fact", "kind": "observation", "recorded": "d", "pg_id": 3},
        "whole", {"rem_processed": False})
    assert "text_is=pointer" in line
    assert line.endswith("] ")
    assert "whole" not in line.split("] ", 1)[1]


def test_inv6_invalid_fact_without_a_postgres_row_is_left_out(caplog):
    """Kills: folding a rem_invalid fact that has no Postgres row (inv 6, Opus r2 F19)."""
    caplog.set_level(logging.WARNING)
    summary, ids = cl.render_thematic_fold(
        ["gone", "kept"], [9, 10],
        record_map={10: {"rtype": "fact", "kind": "observation", "recorded": "d"}},
        state_map={9: {"rem_invalid": True, "rem_processed": True}},
    )
    assert ids == [10]
    assert "fact:9" in caplog.text
    assert "pg_id=9" not in summary
    assert "kept" in summary


def test_inv6_gate_projects_node_fields_not_coalesced_content():
    """Kills: coalesce(f.rem_summary, f.content) in the fact gate (inv 6). Shape only."""
    q = nrem_gate.grounded_fact_groups_cypher(True)
    assert "coalesce(f.rem_summary, f.content)" not in q
    assert "f.rem_summary AS rem_summary" in q
    assert "AS rem_processed" in q
    assert "AS rem_prompt_version" in q
    assert "AS fact_invalid" in q
    assert "AS rem_invalid" not in q
    assert "coalesce(f.rem_processed, false) = true" in q


@pytest.mark.asyncio
async def test_step2_finding1_empty_or_countless_write_is_failure():
    """Kills: an empty write result, or a row with no count, counting as success (step 2 finding 1)."""
    daemon, _session = _daemon([])
    assert await daemon._write_neo4j_rem(7, "short", original_content="o" * 20) == 0
    daemon, _session = _daemon([{"ok": True}])
    assert await daemon._write_neo4j_rem(7, "short", original_content="o" * 20) == 0
    daemon, _session = _daemon([{"count": 1}])
    assert await daemon._write_neo4j_rem(7, "short", original_content="o" * 20) == 1
