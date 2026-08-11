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
    corrective_block,
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


def test_inherited_edge_takes_its_source_standing_not_a_new_one():
    """A judgement's COPY of its evidence's topic (989) is stated explicitly in
    consumable() rather than falling through to the legacy branch. Copied from
    an operator naming (no confidence) it is operator-grade; copied from a
    machine edge it carries that score and is gated exactly as the original was.

    The distinction is what stopped a machine name from reading as a first-write
    one: before the stamp, an inherited edge was written BARE — the same
    signature an operator naming leaves."""
    thr = rc.CONSUME_THRESHOLD[rc.FAMILY_ENTITY]
    inh = rc.ASSERTED_INHERITED
    # copied from an operator naming → no confidence → operator-grade
    assert rc.consumable(rc.FAMILY_ENTITY, inh, None, False)
    # copied from a machine edge → gated on the family exactly like the source
    assert rc.consumable(rc.FAMILY_ENTITY, inh, thr, True)
    assert not rc.consumable(rc.FAMILY_ENTITY, inh, thr - 0.01, True)
    assert not rc.consumable(rc.FAMILY_ENTITY, inh, thr, False)   # uncalibrated
    # and it is NOT the legacy class: a scored copy in an uncalibrated family
    # must not pass the way a bare pre-provenance edge does
    assert rc.consumable(rc.FAMILY_ENTITY, None, thr, False)


# ── The WRITE floor (989) — the gate that stopped being consumption-only ──────

def test_write_admitted_floors_entity_family_and_fails_closed():
    floor = rc.WRITE_FLOOR[rc.FAMILY_ENTITY]
    # at/above the floor with real verification → written
    assert rc.write_admitted(rc.FAMILY_ENTITY, 3, 3, floor)
    assert rc.write_admitted(rc.FAMILY_ENTITY, 2, 3, floor + 0.01)
    # below → withheld
    assert not rc.write_admitted(rc.FAMILY_ENTITY, 1, 3, floor - 0.0001)
    # no numeric confidence → withheld (never written "just in case")
    assert not rc.write_admitted(rc.FAMILY_ENTITY, 3, 3, None)
    # FAIL-CLOSED: k <= 1 means no verification call succeeded. votes/k is then
    # 1.0 and vote_confidence would hand it the CEILING — the one input where
    # the score is highest precisely because nothing checked it.
    assert rc.vote_confidence(1, 1, "tested") == pytest.approx(0.95)
    assert not rc.write_admitted(rc.FAMILY_ENTITY, 1, 1, 0.95)
    assert not rc.write_admitted(rc.FAMILY_EVIDENTIAL, 1, 1, 0.95)


def test_write_floor_never_applies_to_the_evidential_family():
    """Evidential proposals are BORN capped below their own consumption
    threshold (rung 1) so that adjudication promotes them, never the proposer.
    A write floor above that cap would make every one of them unwritable at
    birth — closing the ladder silently instead of leaving it visibly unbuilt."""
    assert rc.FAMILY_EVIDENTIAL not in rc.WRITE_FLOOR
    assert rc.EVIDENTIAL_BORN_BELOW_CAP < rc.WRITE_FLOOR[rc.FAMILY_ENTITY]
    # a fully-confirmed evidential edge sits below the entity floor by design,
    # and is still written
    conf = rc.vote_confidence(3, 3, "tested", family=rc.FAMILY_EVIDENTIAL)
    assert conf < rc.WRITE_FLOOR[rc.FAMILY_ENTITY]
    assert rc.write_admitted(rc.FAMILY_EVIDENTIAL, 3, 3, conf)


def test_default_gate_is_fail_closed():
    g = _default_calibration_gate()
    for fam in rc.FAMILIES:
        assert g[fam]["calibrated"] is False
        assert g[fam]["threshold"] == rc.CONSUME_THRESHOLD[fam]
    assert OPERATOR_ASSERTED == ["operator", "system_default"]


# ── ⛔ REMOVED (v2, C1): the entity-hub/MENTIONS calibration-gated cluster
# finder (`_find_anchored_clusters`) and `run_global_sweep`'s matching entity
# Cypher no longer exist — the v2 FACT GATE (Dreaming Cycle Plan to v2, §2.1)
# discovers on (project, domain) via GROUNDED_IN/DOMAIN_OF/PROJECT_OF, never
# on an entity/MENTIONS hub, so there is no more entity-link edge predicate to
# calibrate for this run type. The four tests that lived here
# (`test_anchored_finder_edge_predicate_and_params_uncalibrated`,
# `test_anchored_finder_calibrated_params_pass_through`,
# `test_anchored_finder_excluded_count_leaves_log_line`,
# `test_global_sweep_query_carries_edge_predicate`) asserted on that removed
# Cypher and are removed with it. `_find_grounded_fact_groups` (the v2
# discovery method) and I1/I2/I8 are covered in test_nrem_axis_levels.py.
# `relation_confidence`'s own semantics (consumable/write_admitted, tested
# above) are untouched — they still gate the INSIGHT path's grounding-edge
# rendering (`_fold_insight`, tested below).


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


def test_corrective_block_empty_is_noop():
    assert corrective_block([]) == ""
    assert corrective_block(None) == ""


def test_corrective_block_demands_per_word_verbatim_not_whole_phrase():
    """D4 (fact:1189): summary_preserves checks TOKEN-LEVEL containment —
    each whitespace-separated word of an anchor fragment, independently,
    anywhere in the text. The instruction used to claim a stricter bar (the
    WHOLE fragment as one exact, character-for-character substring), which
    forced the LLM to embed constructed multi-word fragments verbatim as one
    phrase to satisfy a rule the gate was never actually enforcing. The text
    must now state the real per-word requirement, not the old whole-phrase
    one — and still name each fragment, still forbid omission."""
    text = corrective_block(["Outbox-to-Ingest Adopt Gated Promotion", "refined"])
    assert "WORD BY WORD, not as one exact phrase" in text
    assert "do NOT need to stay together, stay in order, or be adjacent" in text
    # ⛔ The old, over-strict claim must be GONE — its presence is exactly
    # the D4 defect (instruction stricter than the check it corrects for).
    assert "EXACT, literal, character-for-character substring" not in text
    assert '"Outbox-to-Ingest Adopt Gated Promotion"' in text
    assert '"refined"' in text
    assert "none of the words may be omitted" in text.lower()


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


def test_summary_preserves_slack_reaches_the_ordinary_cluster_band():
    """The ratio alone quantises to zero slack below 10 anchors — and
    DENSITY_THRESHOLD makes 5-9 the ordinary band, so the advertised
    paraphrase tolerance never reached the common case. One dropped soft
    anchor must be survivable at every size from the slack floor upward."""
    for n in range(5, 12):
        anchors = [(f"anchorword{i}", False) for i in range(n)]
        text = " ".join(f"anchorword{i}" for i in range(n - 1))   # 1 dropped
        ok, missing = summary_preserves(text, anchors)
        assert ok, f"one dropped soft anchor must survive at cluster size {n}"
        assert missing == [f"anchorword{n - 1}"]


def test_summary_preserves_slack_is_monotone_no_cliff_at_ten():
    """No discontinuity: a 9-record cluster must not be gated harder than a
    10-record one. Before the count-based budget, 9 tolerated zero drops and
    10 tolerated one — neighbouring sizes with materially different gates."""
    def tolerated(n):
        anchors = [(f"anchorword{i}", False) for i in range(n)]
        drops = 0
        while drops < n:
            text = " ".join(f"anchorword{i}" for i in range(n - drops - 1))
            ok, _ = summary_preserves(text, anchors)
            if not ok:
                break
            drops += 1
        return drops
    budgets = [tolerated(n) for n in range(2, 22)]
    assert budgets == sorted(budgets), f"slack must not shrink as n grows: {budgets}"
    assert tolerated(9) == tolerated(10) == 1


def test_summary_preserves_tiny_clusters_stay_all_or_nothing():
    """Below the slack floor the gate stays absolute — a 2-record cluster
    must not get a 50%-loss allowance out of the rounding fix."""
    for n in (2, 3, 4):
        anchors = [(f"anchorword{i}", False) for i in range(n)]
        text = " ".join(f"anchorword{i}" for i in range(n - 1))
        ok, _ = summary_preserves(text, anchors)
        assert not ok, f"cluster size {n} is below the slack floor — no drops"


def test_summary_preserves_slack_floor_is_tunable():
    anchors = [(f"anchorword{i}", False) for i in range(6)]
    text = " ".join(f"anchorword{i}" for i in range(5))           # 1 dropped
    assert summary_preserves(text, anchors, slack_min_units=5)[0]
    assert not summary_preserves(text, anchors, slack_min_units=9)[0]


def test_summary_preserves_hard_anchor_ignores_the_slack_floor():
    """The slack budget must never rescue a decision/retrospective anchor —
    the operator's core demand is untouched by the rounding fix."""
    anchors = [(f"anchorword{i}", False) for i in range(8)] + [("decisiontitle", True)]
    text = " ".join(f"anchorword{i}" for i in range(8))           # only the hard one missing
    ok, missing = summary_preserves(text, anchors)
    assert not ok and missing == ["decisiontitle"]


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


# ⛔ REMOVED (C4): `generate_summary`'s differentiated-block-line /
# corrective-paragraph tests, and the GROUNDING-instruction test on
# `generate_insight`'s prompt. `generate_summary` itself is gone (§3.1's
# thematic fold is zero/low-inference — a deterministic concatenation via
# `fold_record_line`, tested directly in `test_fold_origin.py`; the
# preservation-gate retry loop it used to drive is gone with it, tested
# below only for the still-LLM-backed insight path). `generate_insight`'s
# prompt no longer instructs on GROUNDING lines — see
# `test_insight_consolidation.py`'s
# `test_fold_insight_blocks_are_strictly_title_and_rationale` for the
# positive assertion that those lines never reach the prompt at all.

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
async def test_generate_insight_corrective_paragraph_names_dropped_anchors(monkeypatch):
    monkeypatch.delenv("MOCK_LLM", raising=False)
    captured = _capture_nrem(monkeypatch)
    daemon, _ = daemon_with_fake_graph()
    await daemon.generate_insight("E", ["[DECISION pg_id=1]\nf1"],
                                  corrective=["consolidation", "outbox"])
    prompt = captured["prompt"]
    assert "CORRECTION: the previous draft dropped" in prompt
    assert "WORD BY WORD, not as one exact phrase" in prompt
    assert '"consolidation"' in prompt and '"outbox"' in prompt


@pytest.mark.asyncio
async def test_generate_insight_reversal_block_present_only_when_given(monkeypatch):
    monkeypatch.delenv("MOCK_LLM", raising=False)
    captured = _capture_nrem(monkeypatch)
    daemon, _ = daemon_with_fake_graph()
    await daemon.generate_insight("E", ["[DECISION pg_id=1]\nf1"])
    assert "REVERSALS" not in captured["prompt"]

    captured2 = _capture_nrem(monkeypatch)
    await daemon.generate_insight(
        "E", ["[DECISION pg_id=1]\nf1"],
        reversal_lines=["Decision pg_id=9 (\"Old\") was REVERTED. Reversing "
                        "retrospective pg_id=10: because it failed"])
    assert "[BEGIN REVERSALS]" in captured2["prompt"]
    assert "was REVERTED" in captured2["prompt"]
    assert "explicitly state WHAT was reverted and WHY" in captured2["prompt"]


# ── MOCK_LLM stubs echo the anchors (gate passes honestly, no special-casing) ─

@pytest.mark.asyncio
async def test_mock_insight_echoes_blocks(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, _ = daemon_with_fake_graph()
    blocks = ["[DECISION pg_id=1 project=p]\nAdopt outbox pattern",
              "[RETROSPECTIVE pg_id=2 project=p]\nvalidated under load"]
    out = await daemon.generate_insight("E", blocks)
    ok, missing = summary_preserves(
        out, [(preservation_anchor("Adopt outbox pattern", "decision"), True),
              (preservation_anchor("validated under load", "retrospective"), True)])
    assert ok and missing == []


# ── _fold_insight — Postgres-only fetch shape (C4) ────────────────────────────

def _fold_script_two_decisions():
    return [
        # 1. judgement content fetch — (id, content, project, type, metadata)
        {"rowcount": 2, "rows": [
            (245, "Choose outbox pattern for atomic writes", "shared-memory-GitHub",
             "decision", {}),
            (267, "Adopt listen notify triggers everywhere", "other-project",
             "decision", {}),
        ]},
        # 2. fetch_reversal_context leg 1 (open ledger rows) — none
        {"rowcount": 0, "rows": []},
        # 3. fetch_insight_outbox_rows snapshot
        {"rowcount": 2, "rows": [(101,), (102,)]},
        # 4. insert  5. flip  6. supersession  7. close
        {"rowcount": 1, "rows": [(77,)]},
        {"rowcount": 2, "rows": []},
        {"rowcount": 0, "rows": []},
        {"rowcount": 2, "rows": [(101, 245), (102, 267)]},
    ]


# ⛔ REMOVED (C4): `test_grounding_lines_render_and_gate_by_family` /
# `test_grounding_excluded_edges_leave_log_line` — the stage-5
# calibration-gated GROUNDING-line rendering they exercised is gone from
# `_fold_insight` entirely (§3.2: the embedded text is strictly each
# judgement's own Title+Rationale; grounding-edge detail is deferred to the
# graph walk, `insight_cypher_query`). `relation_confidence`'s own
# consumable()/calibration semantics are untouched and still tested above —
# they now gate nothing inside THIS module, but `relation_sweep.py` and
# `rem_loop.py` still consume them directly.


# ── Preservation gate: retry-then-requeue (insight path — still LLM-backed) ──

def _thematic_conn_script(insert_id=90):
    d = datetime.date(2026, 7, 11)
    return [
        # 1. _fetch_records — (id, project, type, source_ref, created_at::date,
        #    metadata). The trailing metadata blob is what the SECTION axis
        #    used to be resolved from for record kind/date bookkeeping (and,
        #    C4, `entities`); project/domain for GATE PARTITIONING come from
        #    `_ROWS` itself (v2, C1) — this query only feeds `record_map`.
        {"rowcount": 2, "rows": [
            (1, "general", "fact", "tests/test_x.py", d, {"entities": ["Widget"]}),
            (2, "general", "fact", None, d, {}),
        ]},
        # 2. coverage census — _fetch_outbox_created_at over the work items'
        #    pg_ids (own-conn SELECT, so it lands on this same stub).
        {"rowcount": 2, "rows": []},
        # below_density_ids is empty here (both facts gate — pg_ids_all ==
        # all_member_ids), so drop_below_density_refold_rows short-circuits
        # with NO query (its own `if not pg_ids` guard). drop_out_of_scan_
        # refold_rows (C3.1 F1) has no such guard — it always runs, closing
        # 0 rows in this fixture (pg_ids_all is fully in-scan by construction).
        {"rowcount": 0, "rows": []},
        # 3. fold dead-letter counts (own-conn SELECT; empty → no dead-lettering)
        {"rowcount": 0, "rows": []},
        # ⛔ REMOVED (C4): the "previous summary fetch" step — the thematic
        # fold is now zero/low-inference (§3.1) and recomputes the group's
        # FULL current membership fresh every time; there is no cumulative
        # "previous + new" narrative to fetch and merge any more.
        # 4. summary INSERT  5. outbox flip  6. supersession SELECT
        {"rowcount": 1, "rows": [(insert_id,)]},
        {"rowcount": 2, "rows": []},
        {"rowcount": 0, "rows": []},
        # 7. close_ledger_rows DELETE  8. superseded-predecessor purge
        {"rowcount": 2, "rows": [(11, 1), (12, 2)]},
        {"rowcount": 0, "rows": []},
    ]


# v2 FACT GATE (C1): _consolidate_clusters now takes the FLAT rows
# `_find_grounded_fact_groups` returns — no more entity-cluster shape
# (`{"entity":, "aliases":, "contents":, "pg_ids":}`). project="general",
# domain="ops" is an arbitrary REGISTERED-looking (project, domain) pair —
# `eligible_domain_level_clusters` no longer needs a separate registry stub
# to accept it, because `_consolidate_clusters` derives `registered_sections`
# from these SAME rows.
_ROWS = [
    {"pg_id": 1, "content": "The consolidation daemon writes summaries",
     "project": "general", "domain": "ops"},
    {"pg_id": 2, "content": "The outbox worker applies rows",
     "project": "general", "domain": "ops"},
]


def _wire_thematic(monkeypatch, daemon, conn, finish):
    monkeypatch.setattr(cl, "DENSITY_THRESHOLD", 2)
    monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(cl, "_crun_start", lambda ct: 42)
    monkeypatch.setattr(cl, "_crun_finish",
                        lambda *a, **k: finish.update(args=a, kwargs=k))
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)


# ── §3.1 — the thematic fold is a deterministic Zettelkasten index ───────────
# ⛔ REMOVED (C4): `test_preservation_retry_succeeds_with_corrective_prompt`,
# `_second_retry_recovers_what_first_missed`,
# `_double_failure_requeues_and_blocks_tier3` — the LLM-narrative +
# preservation-gate machinery those tests exercised is GONE from the
# thematic path entirely (§3.1: zero/low inference — a structured
# concatenation, never an LLM call, so it structurally cannot drop an
# anchor). The insight path (still LLM-backed, §3.2) keeps its own
# preservation-gate coverage below.

@pytest.mark.asyncio
async def test_thematic_fold_content_is_deterministic_concatenation_no_llm(monkeypatch):
    """§3.1 — content is `fold_record_line` over each constituent's own
    tight text, concatenated — no LLM call at all (MOCK_LLM is irrelevant
    here and deliberately left UNSET to prove nothing tries to reach one)."""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, session = daemon_with_fake_graph()
    conn = StubConn(script=_thematic_conn_script())
    finish = {}
    _wire_thematic(monkeypatch, daemon, conn, finish)

    await daemon._consolidate_clusters(_ROWS)

    insert = next(p for s, p in conn.executed
                  if s.startswith("INSERT INTO community_summaries"))
    content = insert[0]
    assert content == (
        "[FACT kind=tested from=\"tests/test_x.py\" recorded=2026-07-11 pg_id=1] "
        "The consolidation daemon writes summaries\n"
        "[FACT kind=discussion recorded=2026-07-11 pg_id=2] "
        "The outbox worker applies rows"
    )
    meta = json.loads(insert[1])
    assert meta["entities"] == ["Widget"]
    assert "cypher_query" in meta and "1" in meta["cypher_query"] and "2" in meta["cypher_query"]
    # No preservation-gate/truncation counters populate (nothing COULD fail
    # this way any more) — extra() stays byte-identical to the pre-stage-5
    # ledger shape.
    assert finish["kwargs"]["extra"] is None
    assert finish["args"][2:5] == (1, 1, 0)
    assert finish["kwargs"]["eligible_clusters"] == 1
    # graph marking ran
    assert any("consolidated = true" in q for q, _ in session.calls)


@pytest.mark.asyncio
async def test_thematic_fold_embedding_failure_requeues(monkeypatch):
    """The ONE remaining failure mode on this path — vectorisation — still
    requeues the pg_ids exactly as before; nothing about that contract
    changed."""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, session = daemon_with_fake_graph()
    conn = StubConn(script=_thematic_conn_script())
    finish = {}
    _wire_thematic(monkeypatch, daemon, conn, finish)
    daemon.get_embedding = AsyncMock(return_value=None)

    await daemon._consolidate_clusters(_ROWS)

    assert not any(s.startswith("INSERT INTO community_summaries") for s, _ in conn.executed)
    assert session.calls == []
    assert daemon.pending_pg_ids == {1, 2}
    assert finish["args"][2:5] == (1, 0, 1)


@pytest.mark.asyncio
async def test_fact_cycle_dead_lettered_cluster_excluded_from_eligible_census(monkeypatch):
    """D1 (fact:1189, decision:1121/I7): rec.eligible_clusters/extra used to
    be computed over ALL density-gated work_items BEFORE the dead-letter
    filter, so a permanently dead-lettered cluster counted as eligible
    backlog forever and _consolidation_stall_verdict (coordinator.py) could
    never clear. Two (project, domain) groups gate this pass ("ops": pg_ids
    1,2 and "infra": pg_ids 3,4); "infra" is dead-lettered
    (NREM_FOLD_FAIL_CAP reached). eligible_clusters must report 1 (only
    "ops"), and the exclusion is visible separately as
    dead_lettered_clusters=1 — a NEW key, not folded into eligible_clusters'
    own meaning."""
    monkeypatch.setattr(cl, "DENSITY_THRESHOLD", 2)
    monkeypatch.setattr(cl, "NREM_FOLD_FAIL_CAP", 1)
    monkeypatch.delenv("MOCK_LLM", raising=False)
    d = datetime.date(2026, 7, 11)
    rows = [
        {"pg_id": 1, "content": "fact one", "project": "general", "domain": "ops"},
        {"pg_id": 2, "content": "fact two", "project": "general", "domain": "ops"},
        {"pg_id": 3, "content": "fact three", "project": "general", "domain": "infra"},
        {"pg_id": 4, "content": "fact four", "project": "general", "domain": "infra"},
    ]
    # The "infra" group's own content-derived dead-letter identity (decision
    # 882) — computed the same way the code does, so the fixture is exactly
    # what a real ledger row would key on.
    dead_key = cl._fold_identity("fact", [3, 4])

    daemon, session = daemon_with_fake_graph()
    conn = StubConn(script=[
        # 1. _fetch_records — all 4 facts.
        {"rowcount": 4, "rows": [
            (1, "general", "fact", None, d, {}),
            (2, "general", "fact", None, d, {}),
            (3, "general", "fact", None, d, {}),
            (4, "general", "fact", None, d, {}),
        ]},
        # 2. fold dead-letter counts (D1 — moved BEFORE the census): "infra"
        # is at cap, "ops" is not mentioned (defaults to 0).
        {"rowcount": 1, "rows": [(dead_key, 1)]},
        # 3. coverage census (_fetch_outbox_created_at) — over the ELIGIBLE
        # ("ops") group's members only; content irrelevant here.
        {"rowcount": 0, "rows": []},
        # 4. drop_out_of_scan_refold_rows — always runs.
        {"rowcount": 0, "rows": []},
        # 5. INSERT community_summaries — the surviving "ops" group only.
        {"rowcount": 1, "rows": [(90,)]},
        # 6. outbox flip UPDATE
        {"rowcount": 2, "rows": []},
        # 7. supersession SELECT — no old rows to retire.
        {"rowcount": 0, "rows": []},
        # 8. close_ledger_rows DELETE — empty so its purge sub-query never runs.
        {"rowcount": 0, "rows": []},
    ])
    finish = {}
    _wire_thematic(monkeypatch, daemon, conn, finish)
    # _wire_thematic already set DENSITY_THRESHOLD=2; re-assert the cap here
    # is redundant but harmless — keep the fixture self-contained regardless
    # of call order.

    await daemon._consolidate_clusters(rows)

    # Only ONE fold attempted — "infra" was skipped before it ever reached
    # the fold loop.
    assert finish["args"][2:5] == (1, 1, 0)          # attempted, succeeded, failed
    assert finish["kwargs"]["eligible_clusters"] == 1
    assert finish["kwargs"]["extra"]["dead_lettered_clusters"] == 1
    # Both clusters' members DID meet density — dead-lettering is not a
    # below_density drop, so no refold_ledger row should be touched as such.
    assert not any(
        "below_density" in s for s, _ in conn.executed if "UPDATE refold_ledger" in s)


@pytest.mark.asyncio
async def test_insight_preservation_retry_log_uses_per_fold_attempt_number(monkeypatch, caplog):
    """D2 (fact:1189): the preservation-retry log must print THIS FOLD's own
    retry attempt against the per-fold cap (NREM_PRESERVATION_MAX_RETRIES)
    — never cyc.preservation_retries, a CYCLE-GLOBAL counter that keeps
    accumulating across every fold the cycle attempts ('attempt 8/2'
    observed live). Two folds share one _CycleRec, as run_insight_cycle's
    loop does: each fold's OWN first retry must log 'attempt 1/2',
    regardless of how many retries earlier folds in the same cycle already
    burned."""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, _ = daemon_with_fake_graph()
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)
    cyc = _CycleRec()

    def _script(id_a, id_b, word_a, word_b):
        return [
            {"rowcount": 2, "rows": [
                (id_a, f"{word_a}\n\nrationale", "shared-memory-GitHub", "decision", {}),
                (id_b, f"{word_b}\n\nrationale", "shared-memory-GitHub", "decision", {}),
            ]},
            {"rowcount": 0, "rows": []},                       # reversal leg 1
            {"rowcount": 2, "rows": [(101,), (102,)]},          # outbox snapshot
            {"rowcount": 1, "rows": [(77,)]},                   # INSERT
            {"rowcount": 2, "rows": []},                        # ledger flip
            {"rowcount": 0, "rows": []},                        # supersession SELECT
            {"rowcount": 2, "rows": [(101, id_a), (102, id_b)]},  # close DELETE
        ]

    # Fold 1 — burns ONE retry (cyc.preservation_retries: 0 -> 1).
    daemon.generate_insight = AsyncMock(side_effect=[
        "This insight discusses pool contention.",              # missing both anchors
        "This insight discusses Zorbex and Quixotic together.",  # both present
    ])
    conn1 = StubConn(script=_script(245, 267, "Zorbex", "Quixotic"))
    with caplog.at_level("WARNING"):
        ok1 = await daemon._fold_insight(conn1, "OutboxPattern", [245, 267], cyc=cyc)
    assert ok1 is True
    assert cyc.preservation_retries == 1

    # Fold 2 — a SEPARATE fold in the SAME cycle. Also burns ONE retry of
    # its own (cyc.preservation_retries: 1 -> 2), but its OWN attempt count
    # is 1, not 2.
    daemon.generate_insight = AsyncMock(side_effect=[
        "This insight discusses pool contention.",
        "This insight discusses Umbrose and Velvex together.",
    ])
    conn2 = StubConn(script=_script(345, 367, "Umbrose", "Velvex"))
    with caplog.at_level("WARNING"):
        ok2 = await daemon._fold_insight(conn2, "OutboxPattern", [345, 367], cyc=cyc)
    assert ok2 is True
    assert cyc.preservation_retries == 2   # cycle-global total — unchanged meaning

    retry_lines = [m for m in caplog.messages if "corrective retry" in m]
    assert len(retry_lines) == 2
    # ⛔ Both folds' OWN first retry — never the cycle-global running count
    # (which would print "attempt 1/2" then "attempt 2/2").
    assert "attempt 1/2" in retry_lines[0]
    assert "attempt 1/2" in retry_lines[1]


@pytest.mark.asyncio
async def test_insight_preservation_double_failure_no_write(monkeypatch, caplog):
    """The same gate guards generate_insight: two failing drafts → no Postgres
    write, False returned (open ledger rows are the durable requeue)."""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, _ = daemon_with_fake_graph()
    daemon.generate_insight = AsyncMock(side_effect=[
        "irrelevant", "still irrelevant", "still irrelevant again"])
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)
    conn = StubConn(script=_fold_script_two_decisions())
    cyc = _CycleRec()

    with caplog.at_level("WARNING"):
        ok = await daemon._fold_insight(conn, "OutboxPattern", [245, 267], cyc=cyc)

    assert ok is False
    # NREM_PRESERVATION_MAX_RETRIES=2 — initial attempt + 2 corrective retries.
    assert daemon.generate_insight.await_count == 3
    assert daemon.generate_insight.call_args_list[1].kwargs["corrective"]
    assert daemon.generate_insight.call_args_list[2].kwargs["corrective"]
    assert not any(s.startswith("INSERT INTO community_summaries") for s, _ in conn.executed)
    assert cyc.preservation_retries == 2
    assert cyc.preservation_failures == 1
    # Content-derived dead-letter key — sorted qualified refs over the
    # fold's own judgement ids ([245, 267]), both typed 'decision' here.
    assert cyc.preservation_failed == ["decision:245,decision:267"]
    assert any("Preservation gate FAILED after 2 corrective retries for insight" in m
               for m in caplog.messages)


# ── MOCK_LLM end-to-end passes the gate honestly (insight path) ──────────────

@pytest.mark.asyncio
async def test_mock_llm_insight_fold_passes_preservation_gate(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, _ = daemon_with_fake_graph()
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)
    conn = StubConn(script=_fold_script_two_decisions())
    cyc = _CycleRec()

    ok = await daemon._fold_insight(conn, "OutboxPattern", [245, 267], cyc=cyc)

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
        # D1 (fact:1189) — always present once extra() is non-None; 0 when
        # this cycle dead-lettered nothing.
        "dead_lettered_clusters": 0,
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
#
# ⛔ REMOVED (C4): `test_generate_summary_truncated_sets_flag_and_bounds_tokens`,
# `_generate_summary_truncation_retry_succeeds_at_wider_bound`,
# `test_thematic_truncated_summary_off_gate_not_written`,
# `test_fold_key_counted_once_when_corrective_retry_truncates` — all exercised
# `generate_summary` (gone) or the thematic path's truncation handling, which
# no longer exists (§3.1: no LLM call means no truncation mode either). The
# insight path (§3.2, still LLM-backed) keeps its own truncation coverage.

@pytest.mark.asyncio
async def test_insight_truncated_off_gate_not_written(monkeypatch):
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, _ = daemon_with_fake_graph()
    daemon.get_embedding = AsyncMock(return_value=[0.1] * 4)
    conn = StubConn(script=_fold_script_two_decisions())
    cyc = _CycleRec()

    async def _truncated_insight(*a, **k):
        daemon._last_llm_truncated = True
        return ""
    daemon.generate_insight = _truncated_insight

    ok = await daemon._fold_insight(conn, "OutboxPattern", [245, 267], cyc=cyc)

    assert ok is False
    assert cyc.truncation_failures == 1
    assert cyc.preservation_retries == 0 and cyc.preservation_failures == 0


# ── Fold dead-letter identity is content-derived, not label-derived (882) ────

def test_fold_identity_deterministic_regardless_of_order_and_duplicates():
    """Same member set, any input ordering/duplication, must always produce
    the exact same string — the dead-letter ledger is a literal-string
    lookup, so anything less than byte-identical output breaks matching."""
    assert cl._fold_identity("fact", [5, 12, 3]) == cl._fold_identity("fact", [12, 3, 5])
    assert cl._fold_identity("fact", [5, 5, 12, 3, 3]) == cl._fold_identity("fact", [12, 3, 5])


def test_fold_identity_different_record_types_never_collide():
    """A technical_docs id and an unrelated community_summaries id of the same
    integer value must produce different keys — the exact collision decision
    822 diagnosed for bare pg_ids (fact 881: fetch_refold_insights pairs a
    summary id with technical_docs decision ids for one candidate)."""
    fact_key = cl._fold_identity("fact", [5])
    decision_key = cl._fold_identity("decision", [5])
    summary_key = cl._fold_identity("summary", [5])
    assert len({fact_key, decision_key, summary_key}) == 3


def test_fold_identity_changes_when_alias_merge_grows_membership():
    """The bug this replaces: two surface forms of the same entity ('Cloe VM'
    id 10, 'CloeVM' id 20) each independently dead-letter under their own
    label. Once alias resolution merges them, the fold candidate becomes the
    UNION of both — and that union's identity must differ from EITHER
    pre-merge singleton's identity, so it gets a fresh attempt instead of
    inheriting either side's failure count (decision 882)."""
    pre_merge_a = cl._fold_identity("fact", [10])
    pre_merge_b = cl._fold_identity("fact", [20])
    post_merge = cl._fold_identity("fact", [10, 20])
    assert post_merge != pre_merge_a
    assert post_merge != pre_merge_b
    assert len({pre_merge_a, pre_merge_b, post_merge}) == 3
