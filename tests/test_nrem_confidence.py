"""Unit tests for NREM stage 5 of the REM rebuild — calibration-gated cluster
assessment + the deterministic preservation gate (decisions 718/726/727).

Covers: the edge predicate the cluster finders carry (the Cypher mirror of
relation_confidence.consumable — the Python function is the source of truth),
the fail-closed uncalibrated gate, the excluded-machine-edge telemetry
("filtered back" to the relation_adjudications review queue), type/kind
differentiated fold blocks, grounding-edge evidence lines (operator vs
MACHINE-PROPOSED, consumable-gated), the preservation_anchor/summary_preserves
pure functions, the retry-then-requeue flow, and the MOCK_LLM anchor-echoing
end-to-end pass.

All Postgres/Neo4j/LLM I/O is stubbed — no live infrastructure required.
"""
import datetime
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import consolidation_loop as cl
import relation_confidence as rc
from consolidation_loop import (
    ConsolidationDaemon,
    OPERATOR_ASSERTED,
    _CycleRec,
    _default_calibration_gate,
    preservation_anchor,
    summary_preserves,
)


# ── Stubs (test_insight_consolidation conventions) ────────────────────────────

class StubCursor:
    def __init__(self, script, executed):
        self._script = script
        self.executed = executed
        self._current = {"rowcount": 0, "rows": []}

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        self._current = self._script.pop(0) if self._script else {"rowcount": 0, "rows": []}

    @property
    def rowcount(self):
        return self._current["rowcount"]

    def fetchall(self):
        return self._current["rows"]

    def fetchone(self):
        rows = self._current["rows"]
        return rows[0] if rows else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class StubConn:
    def __init__(self, script=None):
        self._script = script or []
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return StubCursor(self._script, self.executed)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class _AsyncCtx:
    def __init__(self, val):
        self._val = val
    async def __aenter__(self):
        return self._val
    async def __aexit__(self, *_):
        pass


class FakeResult:
    def __init__(self, rows=None):
        self._rows = rows or []
    async def data(self):
        return self._rows


class FakeSession:
    """Captures every (query, params) run against the fake Neo4j driver."""
    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])
    async def run(self, query, **params):
        self.calls.append((" ".join(query.split()), params))
        return self._results.pop(0) if self._results else FakeResult()


def daemon_with_fake_graph(results=None):
    daemon = ConsolidationDaemon()
    session = FakeSession(results)
    daemon.driver = MagicMock()
    daemon.driver.session = MagicMock(return_value=_AsyncCtx(session))
    return daemon, session


def _gate(entity=False, evidential=False):
    g = _default_calibration_gate()
    g[rc.FAMILY_ENTITY]["calibrated"] = entity
    g[rc.FAMILY_EVIDENTIAL]["calibrated"] = evidential
    return g


# ── The Cypher gate mirrors relation_confidence.consumable (source of truth) ──

def test_gate_semantics_match_consumable():
    """The finder predicate `asserted_by IS NULL OR asserted_by IN operator OR
    (calibrated AND confidence >= threshold)` must agree with consumable():
    legacy and operator edges always pass; machine edges only when the family
    is calibrated AND numeric confidence clears the threshold."""
    thr = rc.CONSUME_THRESHOLD[rc.FAMILY_ENTITY]
    # legacy (no asserted_by) — era-gated class, ALWAYS consumable
    assert rc.consumable(rc.FAMILY_ENTITY, None, None, False)
    # operator / system_default — always
    assert rc.consumable(rc.FAMILY_ENTITY, "operator", None, False)
    assert rc.consumable(rc.FAMILY_ENTITY, "system_default", None, False)
    # machine, uncalibrated family — never, regardless of confidence
    assert not rc.consumable(rc.FAMILY_ENTITY, "rem", 0.99, False)
    # machine, calibrated, no confidence — never (Cypher: null >= x is not true)
    assert not rc.consumable(rc.FAMILY_ENTITY, "rem", None, True)
    # machine, calibrated, at/above threshold — passes; below — fails
    assert rc.consumable(rc.FAMILY_ENTITY, "rem", thr, True)
    assert not rc.consumable(rc.FAMILY_ENTITY, "rem_sweep", thr - 0.01, True)


def test_default_gate_is_fail_closed():
    g = _default_calibration_gate()
    for fam in rc.FAMILIES:
        assert g[fam]["calibrated"] is False
        assert g[fam]["threshold"] == rc.CONSUME_THRESHOLD[fam]
    assert OPERATOR_ASSERTED == ["operator", "system_default"]


# ── Cluster finder Cypher carries the edge predicate + parameters ─────────────

@pytest.mark.asyncio
async def test_anchored_finder_edge_predicate_and_params_uncalibrated():
    daemon, session = daemon_with_fake_graph(
        [FakeResult([]), FakeResult([{"consumed": 0, "excluded": 3}])])
    clusters, stats = await daemon._find_anchored_clusters([1, 2], _gate(entity=False))
    assert clusters == []
    query, params = session.calls[0]
    # The consumable() mirror, parameterised — never literals.
    assert ("(r.asserted_by IS NULL OR r.asserted_by IN $operator_asserted"
            " OR ($entity_calibrated AND r.confidence >= $entity_threshold))") in query
    assert params["operator_asserted"] == ["operator", "system_default"]
    assert params["entity_calibrated"] is False          # uncalibrated → machine edges out
    assert params["entity_threshold"] == rc.CONSUME_THRESHOLD[rc.FAMILY_ENTITY]
    # The predicate gates the NEIGHBOR traversal (edges), never the component logic.
    assert "MATCH (m)<-[r:" in query
    assert "alias_component" in query                    # clustering semantics intact
    # Follow-up cheap aggregate counts consumed vs excluded machine edges.
    cquery, cparams = session.calls[1]
    assert cparams["machine_asserted"] == ["rem", "rem_sweep"]
    assert "sum(CASE WHEN $entity_calibrated AND r.confidence >= $entity_threshold" in cquery
    assert stats == {"machine_edges_consumed": 0, "edges_awaiting_calibration": 3}


@pytest.mark.asyncio
async def test_anchored_finder_calibrated_params_pass_through():
    daemon, session = daemon_with_fake_graph(
        [FakeResult([]), FakeResult([{"consumed": 2, "excluded": 1}])])
    gate = _gate(entity=True)
    gate[rc.FAMILY_ENTITY]["threshold"] = 0.6
    _clusters, stats = await daemon._find_anchored_clusters([1], gate)
    _q, params = session.calls[0]
    assert params["entity_calibrated"] is True
    assert params["entity_threshold"] == 0.6
    assert stats["machine_edges_consumed"] == 2
    assert stats["edges_awaiting_calibration"] == 1


@pytest.mark.asyncio
async def test_anchored_finder_excluded_count_leaves_log_line(caplog):
    daemon, _ = daemon_with_fake_graph(
        [FakeResult([]), FakeResult([{"consumed": 0, "excluded": 5}])])
    with caplog.at_level("INFO"):
        await daemon._find_anchored_clusters([1], _gate())
    assert any("5 machine-asserted edge(s) excluded" in m for m in caplog.messages)


@pytest.mark.asyncio
async def test_global_sweep_query_carries_edge_predicate(monkeypatch, caplog):
    monkeypatch.setattr(cl, "fetch_calibration_gate", lambda: _gate(entity=False))
    daemon, session = daemon_with_fake_graph(
        [FakeResult([]), FakeResult([{"consumed": 0, "excluded": 4}])])
    with caplog.at_level("INFO"):
        await daemon.run_global_sweep()
    query, params = session.calls[0]
    assert ("(r.asserted_by IS NULL OR r.asserted_by IN $operator_asserted"
            " OR ($entity_calibrated AND r.confidence >= $entity_threshold))") in query
    assert params["operator_asserted"] == ["operator", "system_default"]
    assert params["entity_calibrated"] is False
    # non-destructive read + rem_processed guard unchanged
    assert "coalesce(fact.rem_summary, fact.content)" in query
    assert "rem_processed" in query
    _cq, cparams = session.calls[1]
    assert cparams["machine_asserted"] == ["rem", "rem_sweep"]
    assert any("4 machine-asserted edge(s) excluded" in m for m in caplog.messages)


# ── preservation_anchor / summary_preserves (pure) ────────────────────────────

def test_preservation_anchor_fact_longest_distinctive_word():
    assert preservation_anchor("The consolidation daemon writes summaries. More.") \
        == "consolidation"


def test_preservation_anchor_falls_back_when_no_long_words():
    assert preservation_anchor("ab cde fg") == "cde"


def test_preservation_anchor_empty_and_non_string():
    assert preservation_anchor("") == ""
    assert preservation_anchor(None) == ""
    assert preservation_anchor("   ") == ""


def test_preservation_anchor_decision_adds_title_words():
    a = preservation_anchor("Adopt outbox pattern for atomic writes\n\nrationale here",
                            "decision")
    toks = a.split()
    # longest word + first 4 significant title words, de-duplicated
    assert "pattern" in toks and "Adopt" in toks and "outbox" in toks and "atomic" in toks
    assert len(toks) == len({t.lower() for t in toks})   # deterministic dedup


def test_preservation_anchor_is_deterministic():
    c = "Measured latency dropped after the reranker deploy"
    assert preservation_anchor(c) == preservation_anchor(c)


def test_summary_preserves_pass_and_missing():
    anchors = [("consolidation", False), ("outbox", False)]
    ok, missing = summary_preserves("The consolidation daemon and the outbox worker.", anchors)
    assert ok and missing == []
    ok, missing = summary_preserves("The consolidation daemon only.", anchors)
    assert not ok and missing == ["outbox"]


def test_summary_preserves_fact_slack_ten_percent():
    # 10 plain-fact anchors, 1 missing → 90% coverage → PASS (paraphrase slack).
    anchors = [(f"anchorword{i}", False) for i in range(10)]
    text = " ".join(f"anchorword{i}" for i in range(9))
    ok, missing = summary_preserves(text, anchors)
    assert ok and missing == ["anchorword9"]
    # 2 missing → 80% → FAIL.
    text = " ".join(f"anchorword{i}" for i in range(8))
    ok, _ = summary_preserves(text, anchors)
    assert not ok


def test_summary_preserves_decision_anchor_never_droppable():
    # Same 90% coverage, but the missing anchor is a DECISION anchor → hard fail.
    anchors = [(f"anchorword{i}", False) for i in range(9)] + [("decisiontitle", True)]
    text = " ".join(f"anchorword{i}" for i in range(9))
    ok, missing = summary_preserves(text, anchors)
    assert not ok and missing == ["decisiontitle"]


def test_summary_preserves_multiword_anchor_token_level_case_insensitive():
    ok, _ = summary_preserves("The OUTBOX pattern was adopted; writes stayed atomic.",
                              [("Adopt outbox atomic", True)])
    assert ok                                            # re-ordering absorbed
    ok, missing = summary_preserves("The outbox pattern.", [("Adopt outbox atomic", True)])
    assert not ok and missing == ["Adopt outbox atomic"]


def test_summary_preserves_empty_anchor_set_passes():
    assert summary_preserves("anything", []) == (True, [])
    assert summary_preserves("anything", [("", True)]) == (True, [])


# ── Fold block lines carry type/kind/date/pg_id markers ───────────────────────

def _capture_nrem(monkeypatch, reply="synth"):
    captured = {}
    async def fake_post(client, payload, ceiling_s=None):
        captured["prompt"] = payload["messages"][1]["content"]
        class R:
            status_code = 200
            def json(self):
                return {"choices": [{"message": {"content": reply}}]}
        return R()
    monkeypatch.setattr(cl, "_post_nrem", fake_post)
    return captured


@pytest.mark.asyncio
async def test_generate_summary_renders_differentiated_block_lines(monkeypatch):
    monkeypatch.delenv("MOCK_LLM", raising=False)
    captured = _capture_nrem(monkeypatch)
    daemon, _ = daemon_with_fake_graph()
    out = await daemon.generate_summary(
        "E", ["Latency measured at 40ms", "Adopt outbox pattern"],
        records=[{"pg_id": 42, "rtype": "fact", "kind": "tested", "recorded": "2026-07-11"},
                 {"pg_id": 77, "rtype": "decision", "kind": "discussion", "recorded": "2026-07-14"}])
    assert out == "synth"
    prompt = captured["prompt"]
    assert "[FACT kind=tested recorded=2026-07-11 pg_id=42] Latency measured at 40ms" in prompt
    assert "[DECISION kind=discussion recorded=2026-07-14 pg_id=77] Adopt outbox pattern" in prompt
    # Preservation instructions present verbatim markers.
    assert "integrate EVERY record listed above" in prompt
    assert "nothing may be dropped or de-emphasized because it is inconvenient" in prompt
    assert "the captured record set IS the importance signal" in prompt
    assert "do not cite internal pg-id numbers in the narrative body" in prompt


@pytest.mark.asyncio
async def test_generate_summary_without_records_keeps_legacy_lines(monkeypatch):
    monkeypatch.delenv("MOCK_LLM", raising=False)
    captured = _capture_nrem(monkeypatch)
    daemon, _ = daemon_with_fake_graph()
    await daemon.generate_summary("E", ["plain fact"])
    assert "[FACT] plain fact" in captured["prompt"]


@pytest.mark.asyncio
async def test_generate_summary_corrective_paragraph_names_dropped_anchors(monkeypatch):
    monkeypatch.delenv("MOCK_LLM", raising=False)
    captured = _capture_nrem(monkeypatch)
    daemon, _ = daemon_with_fake_graph()
    await daemon.generate_summary("E", ["f1"], corrective=["consolidation", "outbox"])
    prompt = captured["prompt"]
    assert "CORRECTION: the following captured records were dropped" in prompt
    assert "consolidation; outbox" in prompt


@pytest.mark.asyncio
async def test_generate_insight_prompt_carries_grounding_instruction(monkeypatch):
    monkeypatch.delenv("MOCK_LLM", raising=False)
    captured = _capture_nrem(monkeypatch)
    daemon, _ = daemon_with_fake_graph()
    await daemon.generate_insight("E", ["[DECISION] a", "[DECISION] b"])
    prompt = captured["prompt"]
    assert "Treat [GROUNDING] lines as the decision's evidence base" in prompt
    assert "operator-asserted grounding is authoritative" in prompt
    assert "MACHINE-PROPOSED" in prompt and "attributed as machine-proposed" in prompt


# ── MOCK_LLM stubs echo the anchors (gate passes honestly, no special-casing) ─

@pytest.mark.asyncio
async def test_mock_summary_echoes_anchors(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, _ = daemon_with_fake_graph()
    contents = ["The consolidation daemon writes summaries", "The outbox worker applies rows"]
    out = await daemon.generate_summary("E", contents)
    anchors = [(preservation_anchor(c), False) for c in contents]
    ok, missing = summary_preserves(out, anchors)
    assert ok and missing == []


@pytest.mark.asyncio
async def test_mock_insight_echoes_blocks(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, _ = daemon_with_fake_graph()
    blocks = ["[DECISION pg_id=1]\nAdopt outbox pattern\n[RETROSPECTIVE rating=validated LATEST] held"]
    out = await daemon.generate_insight("E", blocks)
    ok, missing = summary_preserves(
        out, [(preservation_anchor("Adopt outbox pattern", "decision"), True),
              ("validated", True)])
    assert ok and missing == []


# ── Grounding-edge evidence lines in _fold_insight ────────────────────────────

def _fold_script_two_decisions():
    return [
        # 1. decision content fetch
        {"rowcount": 2, "rows": [
            (245, "Choose outbox pattern for atomic writes", "shared-memory-GitHub"),
            (267, "Adopt listen notify triggers everywhere", "tier3-cloe"),
        ]},
        # 2. fetch_insight_outbox_rows snapshot
        {"rowcount": 2, "rows": [(101,), (102,)]},
        # 3. insert  4. flip  5. supersession  6. close
        {"rowcount": 1, "rows": [(77,)]},
        {"rowcount": 2, "rows": []},
        {"rowcount": 0, "rows": []},
        {"rowcount": 2, "rows": [(101, 245), (102, 267)]},
    ]


@pytest.mark.asyncio
async def test_grounding_lines_render_and_gate_by_family(monkeypatch):
    """Operator + legacy edges always render; a machine evidential edge renders
    MACHINE-PROPOSED only when its family is calibrated and confidence clears
    the threshold; a machine entity edge under an UNCALIBRATED entity family is
    excluded and counted into edges_awaiting_calibration."""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    outcomes = [{"pg_id": 245, "rating": "validated", "date": "2026-07-14",
                 "notes": "held", "retro_pg_id": None}]
    grounding = [
        {"pg_id": 245, "role": "GROUNDED_IN", "asserted_by": "operator",
         "confidence": None, "is_entity": True, "target_name": "OutboxPattern",
         "target_pg_id": None, "snippet": ""},
        {"pg_id": 245, "role": "INFORMED_BY", "asserted_by": "rem",
         "confidence": 0.75, "is_entity": False, "target_name": None,
         "target_pg_id": 267, "snippet": "Adopt listen notify triggers"},
        {"pg_id": 245, "role": "CONSIDERED", "asserted_by": "rem",
         "confidence": 0.95, "is_entity": True, "target_name": "ListenNotify",
         "target_pg_id": None, "snippet": ""},
        {"pg_id": 267, "role": "GROUNDED_IN", "asserted_by": None,
         "confidence": None, "is_entity": True, "target_name": "Postgres",
         "target_pg_id": None, "snippet": ""},
    ]
    daemon, session = daemon_with_fake_graph(
        [FakeResult(outcomes), FakeResult(grounding)])
    daemon.generate_insight = AsyncMock(
        return_value="Choose the outbox pattern for atomic writes; Adopt listen "
                     "notify triggers everywhere — validated.")
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)
    conn = StubConn(script=_fold_script_two_decisions())
    gate = _gate(entity=False, evidential=True)          # evidential calibrated at 0.70
    cyc = _CycleRec()

    assert await daemon._fold_insight(conn, "OutboxPattern", [245, 267],
                                      gate=gate, cyc=cyc) is True

    # Grounding fetch query contract: typed roles, outgoing from Decision.
    gq, gparams = session.calls[1]
    for rel in ("GROUNDED_IN", "CONSIDERED", "REJECTED", "UNDER_CONDITIONS", "INFORMED_BY"):
        assert rel in gq
    assert "MATCH (d:Decision)-[g:" in gq
    assert gparams["ids"] == [245, 267]

    blocks = daemon.generate_insight.call_args.args[1]
    d245 = next(b for b in blocks if b.startswith("[DECISION pg_id=245"))
    d267 = next(b for b in blocks if b.startswith("[DECISION pg_id=267"))
    # Operator edge — authoritative form, after the retro lines.
    assert "[GROUNDING role=GROUNDED_IN asserted_by=operator] OutboxPattern" in d245
    assert d245.index("[RETROSPECTIVE") < d245.index("[GROUNDING")
    # Consumable machine evidential edge — MACHINE-PROPOSED with confidence + record target.
    assert ('[GROUNDING role=INFORMED_BY asserted_by=rem MACHINE-PROPOSED conf=0.75]'
            ' pg_id=267 "Adopt listen notify triggers"') in d245
    # Machine entity edge under an uncalibrated entity family — excluded, counted.
    assert "ListenNotify" not in d245
    # Legacy edge (no asserted_by) — always consumable, rendered as legacy.
    assert "[GROUNDING role=GROUNDED_IN asserted_by=legacy] Postgres" in d267
    assert cyc.edges_awaiting_calibration == 1
    assert cyc.machine_edges_consumed == 1


@pytest.mark.asyncio
async def test_grounding_excluded_edges_leave_log_line(monkeypatch, caplog):
    monkeypatch.delenv("MOCK_LLM", raising=False)
    grounding = [{"pg_id": 245, "role": "INFORMED_BY", "asserted_by": "rem",
                  "confidence": 0.65, "is_entity": False, "target_name": None,
                  "target_pg_id": 267, "snippet": "x"}]     # 0.65 < 0.70 evidential
    daemon, _ = daemon_with_fake_graph([FakeResult([]), FakeResult(grounding)])
    daemon.generate_insight = AsyncMock(
        return_value="Choose the outbox pattern for atomic writes; Adopt listen "
                     "notify triggers everywhere.")
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)
    conn = StubConn(script=_fold_script_two_decisions())
    cyc = _CycleRec()
    with caplog.at_level("INFO"):
        await daemon._fold_insight(conn, "OutboxPattern", [245, 267],
                                   gate=_gate(evidential=True), cyc=cyc)
    assert cyc.edges_awaiting_calibration == 1
    assert any("grounding edge(s) excluded" in m for m in caplog.messages)


# ── Preservation gate: retry-then-requeue in _consolidate_clusters ────────────

def _thematic_conn_script(insert_id=90):
    d = datetime.date(2026, 7, 11)
    return [
        # 1. _fetch_records (id, domain, type, source_ref, created_at::date)
        {"rowcount": 2, "rows": [
            (1, "general", "fact", "tests/test_x.py", d),
            (2, "general", "fact", None, d),
        ]},
        # 2. fold dead-letter counts (own-conn SELECT; empty → no dead-lettering)
        {"rowcount": 0, "rows": []},
        # 3. previous summary fetch
        {"rowcount": 0, "rows": []},
        # 4. summary INSERT  5. outbox flip  6. supersession SELECT
        {"rowcount": 1, "rows": [(insert_id,)]},
        {"rowcount": 2, "rows": []},
        {"rowcount": 0, "rows": []},
        # 7. close_ledger_rows DELETE  8. superseded-predecessor purge
        {"rowcount": 2, "rows": [(11, 1), (12, 2)]},
        {"rowcount": 0, "rows": []},
    ]


_CLUSTER = {
    "entity": "TestEntity",
    "aliases": ["TestEntity"],
    "contents": ["The consolidation daemon writes summaries",
                 "The outbox worker applies rows"],
    "pg_ids": [1, 2],
}


def _wire_thematic(monkeypatch, daemon, conn, finish):
    monkeypatch.setattr(cl, "DENSITY_THRESHOLD", 2)
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(cl, "_crun_start", lambda ct: 42)
    monkeypatch.setattr(cl, "_crun_finish",
                        lambda *a, **k: finish.update(args=a, kwargs=k))
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)


@pytest.mark.asyncio
async def test_preservation_retry_succeeds_with_corrective_prompt(monkeypatch):
    """First summary drops an anchor → ONE corrective retry naming it; the
    retry passes → the summary is written and preservation_retries recorded."""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, _ = daemon_with_fake_graph()
    conn = StubConn(script=_thematic_conn_script())
    finish = {}
    _wire_thematic(monkeypatch, daemon, conn, finish)
    daemon.generate_summary = AsyncMock(side_effect=[
        # anchors: 'consolidation' (fact 1), 'applies' (fact 2 — its longest
        # distinctive word). The first draft drops 'applies'.
        "A summary about the consolidation daemon only.",
        "The consolidation daemon; the outbox worker applies rows.",   # corrective pass
    ])

    await daemon._consolidate_clusters([_CLUSTER], gate=_gate())

    assert daemon.generate_summary.await_count == 2
    retry_kwargs = daemon.generate_summary.call_args_list[1].kwargs
    assert retry_kwargs["corrective"] == ["applies"]
    # aligned records reached the fold prompt on both calls
    assert retry_kwargs["records"][0]["pg_id"] == 1
    assert retry_kwargs["records"][0]["kind"] == "tested"
    assert retry_kwargs["records"][1]["recorded"] == "2026-07-11"
    # summary written
    assert any(s.startswith("INSERT INTO community_summaries") for s, _ in conn.executed)
    assert finish["args"][1] == "completed"
    extra = finish["kwargs"]["extra"]
    assert extra["preservation_retries"] == 1
    assert extra["preservation_failures"] == 0
    assert daemon.pending_pg_ids == set()


@pytest.mark.asyncio
async def test_preservation_double_failure_requeues_and_blocks_tier3(monkeypatch, caplog):
    """Both drafts drop an anchor → the summary is NEVER written, the pg_ids are
    re-queued, and the failure lands in extra + a loud log line."""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, session = daemon_with_fake_graph()
    conn = StubConn(script=_thematic_conn_script())
    finish = {}
    _wire_thematic(monkeypatch, daemon, conn, finish)
    daemon.generate_summary = AsyncMock(side_effect=[
        "Nothing relevant at all.", "Still nothing relevant."])

    with caplog.at_level("INFO"):
        await daemon._consolidate_clusters([_CLUSTER], gate=_gate(),
                                           edge_stats={"machine_edges_consumed": 1,
                                                       "edges_awaiting_calibration": 3})

    # no Tier-3 write, no graph marking
    assert not any(s.startswith("INSERT INTO community_summaries") for s, _ in conn.executed)
    assert session.calls == []
    # requeued for a later cycle
    assert daemon.pending_pg_ids == {1, 2}
    assert finish["args"][1] == "completed"           # cycle completed; the FOLD failed
    assert finish["args"][2:5] == (1, 0, 1)           # attempted/succeeded/failed
    extra = finish["kwargs"]["extra"]
    assert extra["preservation_retries"] == 1
    assert extra["preservation_failures"] == 1
    assert extra["preservation_failed"] == ["TestEntity/general"]
    assert extra["edges_awaiting_calibration"] == 3
    assert extra["machine_edges_consumed"] == 1
    assert extra["calibration"] == {"entity_relation": False, "evidential": False}
    assert any("Preservation gate FAILED twice" in m for m in caplog.messages)
    assert any("edges_awaiting_calibration=3" in m for m in caplog.messages)


@pytest.mark.asyncio
async def test_insight_preservation_double_failure_no_write(monkeypatch, caplog):
    """The same gate guards generate_insight: two failing drafts → no Postgres
    write, False returned (open ledger rows are the durable requeue)."""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, _ = daemon_with_fake_graph([FakeResult([]), FakeResult([])])
    daemon.generate_insight = AsyncMock(side_effect=["irrelevant", "still irrelevant"])
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)
    conn = StubConn(script=_fold_script_two_decisions())
    cyc = _CycleRec()

    with caplog.at_level("WARNING"):
        ok = await daemon._fold_insight(conn, "OutboxPattern", [245, 267],
                                        gate=_gate(), cyc=cyc)

    assert ok is False
    assert daemon.generate_insight.await_count == 2
    assert daemon.generate_insight.call_args_list[1].kwargs["corrective"]
    assert not any(s.startswith("INSERT INTO community_summaries") for s, _ in conn.executed)
    assert cyc.preservation_retries == 1
    assert cyc.preservation_failures == 1
    assert cyc.preservation_failed == ["insight/OutboxPattern"]
    assert any("Preservation gate FAILED twice for insight" in m for m in caplog.messages)


# ── MOCK_LLM end-to-end passes the gate honestly ──────────────────────────────

@pytest.mark.asyncio
async def test_mock_llm_thematic_fold_passes_preservation_gate(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, session = daemon_with_fake_graph()
    conn = StubConn(script=_thematic_conn_script())
    finish = {}
    _wire_thematic(monkeypatch, daemon, conn, finish)

    await daemon._consolidate_clusters(
        [_CLUSTER], gate=_gate(),
        edge_stats={"machine_edges_consumed": 0, "edges_awaiting_calibration": 0})

    insert = next(p for s, p in conn.executed
                  if s.startswith("INSERT INTO community_summaries"))
    assert "Mocked Summary for TestEntity" in insert[0]
    # anchors echoed → the gate passed with zero retries
    extra = finish["kwargs"]["extra"]
    assert extra["preservation_retries"] == 0
    assert extra["preservation_failures"] == 0
    assert finish["args"][2:5] == (1, 1, 0)
    # graph marking ran
    assert any("consolidated = true" in q for q, _ in session.calls)


@pytest.mark.asyncio
async def test_mock_llm_insight_fold_passes_preservation_gate(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    outcomes = [{"pg_id": 245, "rating": "validated", "date": "2026-07-14",
                 "notes": "held", "retro_pg_id": None}]
    daemon, _ = daemon_with_fake_graph([FakeResult(outcomes), FakeResult([])])
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)
    conn = StubConn(script=_fold_script_two_decisions())
    cyc = _CycleRec()

    ok = await daemon._fold_insight(conn, "OutboxPattern", [245, 267],
                                    gate=_gate(), cyc=cyc)

    assert ok is True
    assert cyc.preservation_retries == 0 and cyc.preservation_failures == 0
    assert any(s.startswith("INSERT INTO community_summaries") for s, _ in conn.executed)


# ── _CycleRec.extra() shape ───────────────────────────────────────────────────

def test_cyclerec_extra_none_when_untouched():
    # Pre-stage-5 cycles (no gate fetched, nothing counted) stay ledger-identical.
    assert _CycleRec().extra() is None


def test_cyclerec_extra_carries_all_stage5_fields():
    r = _CycleRec()
    r.calibration = {"entity_relation": True, "evidential": False}
    r.edges_awaiting_calibration = 4
    r.machine_edges_consumed = 2
    r.preservation_retries = 1
    r.preservation_failures = 1
    r.preservation_failed = ["E/general"]
    assert r.extra() == {
        "edges_awaiting_calibration": 4,
        "machine_edges_consumed": 2,
        "preservation_retries": 1,
        "preservation_failures": 1,
        "truncation_failures": 0,
        "calibration": {"entity_relation": True, "evidential": False},
        "preservation_failed": ["E/general"],
    }


# ── fetch_calibration_gate (stubbed ledger) ───────────────────────────────────

def test_fetch_calibration_gate_fail_closed_on_db_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("pg down")
    monkeypatch.setattr(cl.psycopg2, "connect", _boom)
    gate = cl.fetch_calibration_gate()
    assert gate == _default_calibration_gate()


def test_fetch_calibration_gate_reads_ledger(monkeypatch):
    # calibration_state runs one query per family; ≥ min labels → calibrated.
    n = rc.CALIBRATION_MIN_LABELS
    conn = StubConn(script=[
        {"rowcount": 1, "rows": [(7, n, n - 1)]},   # entity family: n labels
        {"rowcount": 1, "rows": [(7, 1, 1)]},       # evidential family: 1 label
    ])
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: conn)
    gate = cl.fetch_calibration_gate()
    assert gate[rc.FAMILY_ENTITY]["calibrated"] is True
    assert gate[rc.FAMILY_EVIDENTIAL]["calibrated"] is False
    assert gate[rc.FAMILY_ENTITY]["threshold"] == rc.CONSUME_THRESHOLD[rc.FAMILY_ENTITY]
    assert conn.closed


# ── Fix-wave: NREM truncation is a capacity failure, NOT a preservation miss ───
# A length-finish draft can PASS the anchor check (the gate detects omission, not
# truncation), so it must be discarded BEFORE the gate, counted separately, and
# never persisted / never spend the corrective retry.

@pytest.mark.asyncio
async def test_generate_summary_truncated_sets_flag_and_bounds_tokens(monkeypatch):
    monkeypatch.delenv("MOCK_LLM", raising=False)
    captured = {}
    async def fake_post(client, payload, ceiling_s=None):
        captured.update(payload)
        class R:
            status_code = 200
            def json(self):
                return {"choices": [{"finish_reason": "length",
                                     "message": {"content": "partial narrative"}}]}
        return R()
    monkeypatch.setattr(cl, "_post_nrem", fake_post)
    daemon, _ = daemon_with_fake_graph()
    daemon._last_llm_truncated = False

    out = await daemon.generate_summary("TestEntity", ["fact one", "fact two"])
    assert out is None
    assert daemon._last_llm_truncated is True
    assert captured["max_tokens"] == cl.NREM_MAX_TOKENS_SUMMARY


@pytest.mark.asyncio
async def test_thematic_truncated_summary_off_gate_not_written(monkeypatch):
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, _ = daemon_with_fake_graph()
    conn = StubConn(script=_thematic_conn_script())
    finish = {}
    _wire_thematic(monkeypatch, daemon, conn, finish)

    async def _truncated_summary(*a, **k):
        daemon._last_llm_truncated = True
        return ""     # falsy draft on a length-finish
    daemon.generate_summary = _truncated_summary

    await daemon._consolidate_clusters([_CLUSTER], gate=_gate())

    assert not any(s.startswith("INSERT INTO community_summaries") for s, _ in conn.executed)
    extra = finish["kwargs"]["extra"]
    assert extra["truncation_failures"] == 1
    assert extra["preservation_retries"] == 0     # gate never engaged
    assert extra["preservation_failures"] == 0
    assert finish["args"][2:5] == (1, 0, 1)       # attempted / succeeded / failed


@pytest.mark.asyncio
async def test_insight_truncated_off_gate_not_written(monkeypatch):
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, _ = daemon_with_fake_graph([FakeResult([]), FakeResult([])])
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)
    conn = StubConn(script=_fold_script_two_decisions())
    cyc = _CycleRec()

    async def _truncated_insight(*a, **k):
        daemon._last_llm_truncated = True
        return ""
    daemon.generate_insight = _truncated_insight

    ok = await daemon._fold_insight(conn, "OutboxPattern", [245, 267], gate=_gate(), cyc=cyc)

    assert ok is False
    assert cyc.truncation_failures == 1
    assert cyc.preservation_retries == 0 and cyc.preservation_failures == 0
    assert not any(s.startswith("INSERT INTO community_summaries") for s, _ in conn.executed)
