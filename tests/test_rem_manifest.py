"""
REM rebuild Stage ③ tests — capture-manifest input, delta prompting, k=3
self-consistency confidence, universal edge provenance, evidential proposals
(decisions 718 / 726 / 727).

All Neo4j / Postgres / LLM I/O is mocked; no live infrastructure required.
"""

import asyncio
import importlib.util
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── Dynamic import (mirrors test_rem_loop.py pattern) ─────────────────────────

def load_rem_loop():
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    path = os.path.join(scripts_dir, "rem_loop.py")
    spec = importlib.util.spec_from_file_location("rem_loop", path)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules["rem_loop"] = mod
    spec.loader.exec_module(mod)
    return mod

rem_mod = load_rem_loop()
ONT = rem_mod.ONT
rc  = rem_mod.rc


# ── Helpers ───────────────────────────────────────────────────────────────────

class _async_ctx:
    def __init__(self, val):  self._val = val
    async def __aenter__(self): return self._val
    async def __aexit__(self, *_): pass


def _make_daemon(data_rows=None):
    """REMDaemon with the driver mocked; session.run returns a result whose
    .data() yields `data_rows` (default empty)."""
    d = rem_mod.REMDaemon.__new__(rem_mod.REMDaemon)
    d.is_running = True
    mock_result = MagicMock()
    mock_result.data = AsyncMock(return_value=data_rows or [])
    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=mock_result)
    mock_driver = MagicMock()
    mock_driver.session = MagicMock(return_value=_async_ctx(mock_session))
    d.driver = mock_driver
    return d, mock_session


class _Cur:
    """Cursor stub recording executed SQL (context-manager compatible).
    fetchone returns None so the optional audit-log path (module-level
    AUDIT_LOG_PATH may leak from other test files' environment) is a no-op."""
    def __init__(self, log): self._log = log
    def execute(self, sql, params=None): self._log.append((" ".join(sql.split()), params))
    def fetchone(self): return None
    def fetchall(self): return []
    def __enter__(self): return self
    def __exit__(self, *exc): return False


def _make_conn():
    executed = []
    conn = MagicMock()
    conn.cursor = MagicMock(side_effect=lambda: _Cur(executed))
    return conn, executed


_RICH_ROW = {
    "content": "The coordinator now routes embeddings via the gateway.",
    "kind": "decision",
    "created_at": datetime(2026, 7, 11, tzinfo=timezone.utc),
    "source_ref": "tests/test_coordinator.py",
    "entities": ["coordinator", "Neo4j"],
    "project": "shared-memory",
    "decision_title": "Route embeddings through the gateway",
    "rating": None,
}

_RICH_EDGES = [
    {"rel_type": "MENTIONS", "target": "coordinator", "target_pg_id": None,
     "asserted_by": "operator"},
    {"rel_type": "GROUNDED_IN", "target": None, "target_pg_id": 542,
     "asserted_by": "operator"},
]


# ── Manifest assembly ─────────────────────────────────────────────────────────

def test_build_manifest_rich():
    m = rem_mod.build_manifest(_RICH_ROW, _RICH_EDGES)
    assert m["kind"] == "decision"
    assert m["fact_kind"] == "tested"                 # derived from source_ref
    assert m["entities"] == ["coordinator", "Neo4j"]
    assert m["project"] == "shared-memory"
    assert m["decision_title"] == "Route embeddings through the gateway"
    assert len(m["existing_edges"]) == 2


def test_build_manifest_empty_row_degenerates():
    """Era-gating is structural: an old record with no metadata simply yields
    an empty manifest — no flag, no legacy branch."""
    m = rem_mod.build_manifest({"content": "old fact", "kind": "fact"}, None)
    assert m["kind"] == "fact"
    # An un-source_ref'd record falls to the floor, which is `discussion`.
    assert m["fact_kind"] == "discussion"
    assert m["entities"] == [] and m["existing_edges"] == []
    block = rem_mod._manifest_block(m)
    assert "nothing captured yet" in block            # delta → full extraction


def test_manifest_block_renders_edges_roles_and_asserted_by():
    m = rem_mod.build_manifest(_RICH_ROW, _RICH_EDGES)
    block = rem_mod._manifest_block(m)
    assert "fact_kind: tested" in block
    assert "title: Route embeddings through the gateway" in block
    assert "recorded: 2026-07-11" in block
    # _RICH_ROW is a DECISION, and a decision mints nothing from `entities`, so
    # the block must not claim those names are captured (issue #180).
    assert "operator entity hints (NOT captured" in block
    assert "coordinator, Neo4j" in block
    # The real-edge block keeps its wording; only the entities line changes.
    assert "operator entities (already captured)" not in block
    assert "-[MENTIONS]-> coordinator (asserted_by=operator)" in block
    assert "-[GROUNDED_IN]-> record pg_id 542 (asserted_by=operator)" in block
    assert "nothing captured yet" not in block


def test_existing_edge_set_reads_entities_only_on_facts():
    """Issue #180 — the novelty gate's other half. `existing_edges` comes from
    the graph and always counts; `entities` is a claim about what first write
    did, true only for a fact. On a judgement, a name with no graph edge must
    stay PROPOSABLE: since v0.8.26 first write mints nothing from it, so
    counting it as captured left the edge uncreatable from either side.
    """
    ent_rel = rem_mod.ONT.entity_link
    fact = rem_mod.build_manifest(dict(_RICH_ROW, kind="fact"), _RICH_EDGES)
    assert ("Neo4j", ent_rel) in rem_mod._existing_edge_set(fact)

    for kind in ("decision", "retrospective"):
        m = rem_mod.build_manifest(dict(_RICH_ROW, kind=kind), _RICH_EDGES)
        got = rem_mod._existing_edge_set(m)
        # "Neo4j" is named in `entities` but has no edge → still proposable.
        assert ("Neo4j", ent_rel) not in got
        # "coordinator" has a REAL MENTIONS edge → captured, whatever the type.
        assert ("coordinator", ent_rel) in got


def test_manifest_block_fact_entities_are_still_captured():
    """The type split, from the other side: first write DOES materialise a
    fact's `entities` as MENTIONS, so on a fact the claim is true and the
    wording must stay — a fact's names are not re-proposed."""
    row = dict(_RICH_ROW, kind="fact")
    block = rem_mod._manifest_block(rem_mod.build_manifest(row, _RICH_EDGES))
    assert "operator entities (already captured): coordinator, Neo4j" in block


def test_batch_fetch_content_selects_manifest_fields():
    """The one Postgres query must carry every manifest field (726 §1)."""
    daemon, _ = _make_daemon()
    conn, executed = _make_conn()

    async def _go():
        await daemon._batch_fetch_content([1], conn, asyncio.get_running_loop())
    asyncio.run(_go())

    sql, _ = executed[0]
    for frag in ("metadata->>'source_ref'", "metadata->'entities'",
                 "metadata->>'project'", "metadata->'decision'->>'title'",
                 "metadata->>'rating'", "created_at"):
        assert frag in sql, f"manifest field missing from SELECT: {frag}"


@pytest.mark.asyncio
async def test_fetch_existing_edges_one_query_grouped_by_pg_id():
    rows = [
        {"pg_id": 7, "rel_type": "MENTIONS", "target": "Neo4j",
         "target_pg_id": None, "asserted_by": None},
        {"pg_id": 7, "rel_type": "GROUNDED_IN", "target": None,
         "target_pg_id": 42, "asserted_by": "operator"},
        {"pg_id": 9, "rel_type": "PROJECT_OF", "target": "shared-memory",
         "target_pg_id": None, "asserted_by": "operator"},
    ]
    daemon, mock_session = _make_daemon(rows)

    out = await daemon._fetch_existing_edges([7, 9])

    assert mock_session.run.call_count == 1           # ONE query per batch
    assert len(out[7]) == 2 and len(out[9]) == 1
    assert out[7][1]["asserted_by"] == "operator"


# ── Registry: sub-label state + Decision pg_id ───────────────────────────────

def test_registry_carries_typed_flag_and_decision_pg_id():
    closed_set = [
        {"name": "coordinator", "labels": ["Entity", "Component"], "pg_id": None},
        {"name": "loose-idea",  "labels": ["Entity"],              "pg_id": None},
        {"name": "Route embeddings", "labels": ["Decision"],       "pg_id": 550},
        {"name": "Xenofon",     "labels": ["Human"],               "pg_id": None},
    ]
    reg = rem_mod._build_entity_registry(closed_set)
    assert reg["coordinator"]["typed"] is True        # already sub-labelled
    assert reg["loose-idea"]["typed"] is False        # still untyped
    assert reg["Xenofon"]["typed"] is True            # non-Entity → never sub-typed
    assert reg["Route embeddings"]["pg_id"] == 550    # evidential ledger endpoint


def test_entity_lines_marks_untyped_only():
    lines = rem_mod._entity_lines([
        {"name": "coordinator", "labels": ["Entity", "Component"]},
        {"name": "loose-idea",  "labels": ["Entity"]},
        {"name": "Xenofon",     "labels": ["Human"]},
    ])
    assert "Entity: coordinator" in lines and "coordinator [untyped]" not in lines
    assert "Entity: loose-idea [untyped]" in lines
    assert "Human: Xenofon" in lines and "Xenofon [untyped]" not in lines


# ── Prompt gating ─────────────────────────────────────────────────────────────

def test_single_prompt_short_record_no_summary_requested():
    p = rem_mod.build_single_prompt("short fact", rem_mod.KIND_FACT, [], {})
    assert '"summary"' not in p.split("Respond with ONLY")[1]   # not in the JSON shape
    assert "Do NOT include a \"summary\" field" in p
    assert "CAPTURE MANIFEST" in p
    assert "DELTA" in p


def test_single_prompt_long_record_requests_summary():
    long = "x" * (rem_mod.REM_SUMMARY_THRESHOLD + 1)
    p = rem_mod.build_single_prompt(long, rem_mod.KIND_FACT, [], {})
    assert "summary: one paragraph, at most 5 sentences" in p
    assert '"summary": "<paragraph>"' in p


def test_single_prompt_empty_manifest_degenerates_to_full_extraction():
    """No era flag, no legacy branch: the same delta prompt with an empty
    manifest simply instructs full extraction."""
    m = rem_mod.build_manifest({"content": "c", "kind": "fact"}, None)
    p = rem_mod.build_single_prompt("c", rem_mod.KIND_FACT, [], m)
    assert "nothing captured yet — extract referenced entities in full" in p
    assert "DELTA" in p            # same framing, degenerate delta


def test_single_prompt_decision_includes_extras_tasks():
    p = rem_mod.build_single_prompt("d", rem_mod.KIND_DECISION, [], {})
    assert "considered/rejected alternatives" in p
    assert '"produces_insight"' in p


# ── plan_edges: delta, novelty, gates ────────────────────────────────────────

def _registry():
    return {
        "Neo4j":     {"label": ONT.entity,   "default_rel": ONT.entity_link,
                      "typed": True,  "pg_id": None},
        "Xenofon":   {"label": ONT.human,    "default_rel": ONT.was_attributed_to,
                      "typed": True,  "pg_id": None},
        "prior-dec": {"label": ONT.decision, "default_rel": ONT.informed_by,
                      "typed": True,  "pg_id": 550},
    }


def test_plan_edges_novelty_excludes_manifest_existing():
    # Every name here is registry-known: since 937 an unknown name is dropped
    # before novelty is ever scored, so novelty is a question about EXISTING
    # nodes only.
    registry = _registry() | {
        "coordinator": {"label": ONT.entity, "default_rel": ONT.entity_link,
                        "typed": True, "pg_id": None},
        "BGE-M3":      {"label": ONT.entity, "default_rel": ONT.entity_link,
                        "typed": True, "pg_id": None},
    }
    manifest = {"entities": ["coordinator"],
                "existing_edges": [{"rel_type": ONT.entity_link, "target": "Neo4j",
                                    "asserted_by": "operator"}]}
    result = {"relationships": [
        {"name": "Neo4j",       "rel_type": ONT.entity_link},   # existing edge
        {"name": "coordinator", "rel_type": ONT.entity_link},   # operator entity
        {"name": "BGE-M3",      "rel_type": ONT.entity_link},   # new EDGE, known node
    ]}
    plan = rem_mod.plan_edges(result, registry, rem_mod.KIND_FACT, manifest)
    by_name = {e["name"]: e for e in plan["edges"]}
    assert by_name["Neo4j"]["novel"] is False         # operator edge never re-scored
    assert by_name["coordinator"]["novel"] is False
    assert by_name["BGE-M3"]["novel"] is True


def test_plan_edges_unknown_name_is_dropped_not_minted():
    """937: REM links but never mints. An unknown name used to fall back to a
    brand-new generic :Entity via MENTIONS (727 §1) — the one path every
    fragment entity in the graph came from. It is now dropped instead."""
    plan = rem_mod.plan_edges(
        {"relationships": [{"name": "BrandNewThing", "rel_type": "WAS_ATTRIBUTED_TO"}]},
        _registry(), rem_mod.KIND_FACT, {})
    assert plan["edges"] == []
    assert plan["mint_dropped"] == ["BrandNewThing"]


def test_plan_edges_known_name_still_links_under_the_mint_gate():
    """The gate must block CREATION only — linking to an existing node is REM's
    whole job and has to keep working."""
    plan = rem_mod.plan_edges(
        {"relationships": [{"name": "Neo4j", "rel_type": ONT.entity_link}]},
        _registry(), rem_mod.KIND_FACT, {})
    (e,) = plan["edges"]
    assert e["name"] == "Neo4j" and e["rel_type"] == ONT.entity_link
    assert plan["mint_dropped"] == []


def test_plan_edges_mint_gate_is_env_overridable(monkeypatch):
    """Setting REM_MAY_MINT_ENTITIES restores the pre-937 fallback exactly, so a
    deployment whose capture surface never names entities can opt back in."""
    monkeypatch.setattr(rem_mod, "REM_MAY_MINT_ENTITIES", True)
    plan = rem_mod.plan_edges(
        {"relationships": [{"name": "BrandNewThing", "rel_type": "WAS_ATTRIBUTED_TO"}]},
        _registry(), rem_mod.KIND_FACT, {})
    (e,) = plan["edges"]
    assert e["label"] == ONT.entity and e["rel_type"] == ONT.entity_link
    assert e["novel"] is True
    assert plan["mint_dropped"] == []


def test_plan_edges_mint_gate_is_per_name_not_all_or_nothing():
    """One unknown name in a batch must not cost the known ones their edges —
    the gate is a per-name refusal, not a whole-result rejection."""
    result = {"relationships": [
        {"name": "BrandNewThing", "rel_type": ONT.entity_link, "type": "Component"},
        {"name": "Neo4j",         "rel_type": ONT.entity_link, "type": "System"},
        {"name": "Xenofon",       "rel_type": ONT.was_attributed_to},
    ]}
    plan = rem_mod.plan_edges(result, _registry(), rem_mod.KIND_FACT, {})
    assert sorted(e["name"] for e in plan["edges"]) == ["Neo4j", "Xenofon"]
    assert plan["mint_dropped"] == ["BrandNewThing"]


def test_plan_edges_grounded_in_remapped_to_informed_by():
    """GROUNDED_IN is never machine-mintable — a suggestion of it resolves to
    INFORMED_BY (for a Decision target) and is reported for logging."""
    plan = rem_mod.plan_edges(
        {"relationships": [{"name": "prior-dec", "rel_type": ONT.grounded_in}]},
        _registry(), rem_mod.KIND_DECISION, {})
    (e,) = plan["edges"]
    assert e["rel_type"] == ONT.informed_by
    assert e["evidential"] is True and e["tgt_pg_id"] == 550
    assert plan["grounded_in_remaps"] == ["prior-dec"]
    assert not any(x["rel_type"] == ONT.grounded_in for x in plan["edges"])


def test_plan_edges_evidential_only_for_decision_or_retro_anchor():
    rel = {"relationships": [{"name": "prior-dec", "rel_type": ONT.informed_by}]}
    fact_plan  = rem_mod.plan_edges(rel, _registry(), rem_mod.KIND_FACT, {})
    retro_plan = rem_mod.plan_edges(rel, _registry(), rem_mod.KIND_RETRO, {})
    assert fact_plan["edges"][0]["evidential"] is False
    assert retro_plan["edges"][0]["evidential"] is True


def test_plan_edges_extras_gate_registry_known_only():
    """718: decision-extras targets are minted ONLY when already registry-known;
    unknown free phrases are counted as drops, never minted."""
    result = {"relationships": [],
              "considered": ["Neo4j", "some free phrase nobody registered"],
              "rejected": [], "under_conditions": [],
              "produces_insight": ["another loose thought"]}
    plan = rem_mod.plan_edges(result, _registry(), rem_mod.KIND_DECISION, {})
    assert [e["name"] for e in plan["edges"]] == ["Neo4j"]
    assert plan["edges"][0]["rel_type"] == ONT.considered
    assert sorted(plan["extras_dropped"]) == [
        "another loose thought", "some free phrase nobody registered"]


def test_plan_edges_sanitize_gate_still_applies():
    plan = rem_mod.plan_edges(
        {"relationships": [{"name": "254", "rel_type": ONT.entity_link},
                           {"name": "true", "rel_type": ONT.entity_link}]},
        {}, rem_mod.KIND_FACT, {})
    assert plan["edges"] == []
    assert sorted(plan["dropped_names"]) == ["254", "true"]


# ── k=3 self-consistency ──────────────────────────────────────────────────────

def test_verify_novel_edges_mock_deterministic(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, _ = _make_daemon()
    votes, k = asyncio.run(daemon._verify_novel_edges(
        "content", [{"name": "X", "rel_type": "MENTIONS"}], 1))
    assert votes == [3] and k == 3


def test_verify_novel_edges_degrades_on_call_failure(monkeypatch):
    """A failed verification call reduces k instead of blocking enrichment."""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, _ = _make_daemon()
    proposed = [{"name": "A", "rel_type": "MENTIONS"},
                {"name": "B", "rel_type": "MENTIONS"}]
    calls = iter([None, {0: True, 1: False}])   # 1st call fails, 2nd confirms A only

    async def _fake_call(prompt, pg_id, n_edges=1):
        return next(calls)
    daemon._llm_verify_call = _fake_call

    votes, k = asyncio.run(daemon._verify_novel_edges("content", proposed, 1))
    assert k == 2                     # 1 main + 1 SUCCEEDED verification
    assert votes == [2, 1]            # A confirmed, B denied


def test_verify_novel_edges_all_calls_fail_degrades_to_one_vote(monkeypatch):
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, _ = _make_daemon()

    async def _fake_call(prompt, pg_id, n_edges=1):
        return None
    daemon._llm_verify_call = _fake_call

    votes, k = asyncio.run(daemon._verify_novel_edges(
        "content", [{"name": "A", "rel_type": "MENTIONS"}], 1))
    assert (votes, k) == ([1], 1)     # degraded, never blocked


def test_build_verify_prompt_compact():
    p = rem_mod._build_verify_prompt("c" * 5000,
                                     [{"name": "Neo4j", "rel_type": "MENTIONS"}])
    assert '0. -[MENTIONS]-> "Neo4j"' in p
    assert '{"idx": <n>, "confirm": true|false}' in p
    # content capped for cheapness
    assert "c" * (rem_mod.VERIFY_CONTENT_CAP + 1) not in p


# ── _apply_fact_result wiring: confidence, provenance, ledger ─────────────────

def _known(*names):
    """Registry in which each name is an already-existing :Entity. Since 937 REM
    refuses to mint, so any _apply test about EDGE behaviour must hand it nodes
    that exist — otherwise the mint gate drops the name first and the test
    silently stops exercising what it claims to."""
    return {n: {"label": ONT.entity, "default_rel": ONT.entity_link,
                "typed": True, "pg_id": None} for n in names}


def _apply(daemon, conn, kind, result, registry, manifest, monkeypatch=None,
           original="the decision rationale", model="test-model", run_id="run-1"):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(daemon._apply_fact_result(
            42, kind, result, registry, conn, loop,
            original_content=original, manifest=manifest,
            model=model, run_id=run_id))
    finally:
        loop.close()


def test_apply_stamps_rem_provenance_with_vote_confidence(monkeypatch):
    """MOCK_LLM verification (votes=k=3) → confidence = vote_confidence(3,3,fk)
    and full edge_properties on the minted edge rows."""
    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, mock_session = _make_daemon()
    conn, _ = _make_conn()
    manifest = {"fact_kind": "tested", "entities": [], "existing_edges": []}
    result = {"relationships": [{"name": "BGE-M3", "rel_type": ONT.entity_link}]}

    ok = _apply(daemon, conn, rem_mod.KIND_DECISION, result, _known("BGE-M3"), manifest)

    assert ok is True
    merge_calls = [c for c in mock_session.run.call_args_list
                   if "ON CREATE SET" in c.args[0]]
    assert merge_calls, "novel edge must be minted with ON CREATE provenance"
    props = merge_calls[0].kwargs["rows"][0]["props"]
    assert props["asserted_by"] == rc.ASSERTED_REM
    assert props["confidence"] == rc.vote_confidence(3, 3, "tested")
    assert props["model"] == "test-model" and props["run_id"] == "run-1"


def test_apply_discussion_votes_one_skips_edge(monkeypatch):
    """fact_kind='discussion' + denied by both verifications (votes 1/3) →
    the edge is NOT minted (logged); everything else about the record proceeds."""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, mock_session = _make_daemon()
    conn, _ = _make_conn()

    async def _fake_verify(content, proposed, pg_id):
        return [1] * len(proposed), 3
    daemon._verify_novel_edges = _fake_verify

    manifest = {"fact_kind": "discussion", "entities": [], "existing_edges": []}
    result = {"relationships": [{"name": "SpeculativeThing", "rel_type": ONT.entity_link}]}

    ok = _apply(daemon, conn, rem_mod.KIND_DECISION, result, _known("SpeculativeThing"), manifest)

    assert ok is True
    cyphers = [c.args[0] for c in mock_session.run.call_args_list]
    assert not any("ON CREATE SET" in c for c in cyphers)     # edge skipped
    assert "rem_processed" in cyphers[-1]                     # record still done


def test_apply_low_vote_non_discussion_still_minted(monkeypatch):
    """Non-discussion low-vote edges are still minted with low confidence —
    consumption gating is NREM's job."""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, mock_session = _make_daemon()
    conn, _ = _make_conn()

    async def _fake_verify(content, proposed, pg_id):
        return [1] * len(proposed), 3
    daemon._verify_novel_edges = _fake_verify

    manifest = {"fact_kind": "observation", "entities": [], "existing_edges": []}
    result = {"relationships": [{"name": "WeakEdgeTarget", "rel_type": ONT.entity_link}]}

    ok = _apply(daemon, conn, rem_mod.KIND_DECISION, result, _known("WeakEdgeTarget"), manifest)

    assert ok is True
    merge_calls = [c for c in mock_session.run.call_args_list
                   if "ON CREATE SET" in c.args[0]]
    assert merge_calls
    props = merge_calls[0].kwargs["rows"][0]["props"]
    assert props["confidence"] == rc.vote_confidence(1, 3, "observation")


def test_apply_evidential_ledger_row_rem_k3_and_cap(monkeypatch):
    """A Decision anchor INFORMED_BY a registry-known Decision → edge capped
    below the evidential consumption threshold + ledger row (method rem_k3)."""
    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, mock_session = _make_daemon()
    conn, _ = _make_conn()

    captured = {}
    def _fake_upsert(c, **kwargs):
        captured.update(kwargs)
        return 1
    monkeypatch.setattr(rem_mod.rc, "upsert_adjudication", _fake_upsert)

    registry = _registry()
    manifest = {"fact_kind": "tested", "entities": [], "existing_edges": []}
    result = {"relationships": [{"name": "prior-dec", "rel_type": ONT.informed_by}]}

    ok = _apply(daemon, conn, rem_mod.KIND_DECISION, result, registry, manifest)

    assert ok is True
    assert captured["family"] == rc.FAMILY_EVIDENTIAL
    assert captured["method"] == "rem_k3"
    assert captured["verdict"] == "accept"
    assert captured["rel_type"] == ONT.informed_by
    assert captured["src_pg_id"] == 42 and captured["tgt_pg_id"] == 550
    assert captured["signals"] == {"votes": 3, "k": 3, "fact_kind": "tested"}
    assert captured["run_id"] == "run-1"
    # born-below rule: edge + ledger confidence never reach the consumption threshold
    assert captured["confidence"] <= rc.EVIDENTIAL_BORN_BELOW_CAP
    merge_calls = [c for c in mock_session.run.call_args_list
                   if "ON CREATE SET" in c.args[0]]
    assert merge_calls[0].kwargs["rows"][0]["props"]["confidence"] \
        <= rc.EVIDENTIAL_BORN_BELOW_CAP


def test_apply_evidential_without_pg_id_skips_ledger(monkeypatch):
    """Target Decision with no pg_id in the registry: graph edge minted,
    ledger row skipped (logged)."""
    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, mock_session = _make_daemon()
    conn, _ = _make_conn()

    called = []
    monkeypatch.setattr(rem_mod.rc, "upsert_adjudication",
                        lambda c, **kw: called.append(kw) or 1)

    registry = {"legacy-dec": {"label": ONT.decision, "default_rel": ONT.informed_by,
                               "typed": True, "pg_id": None}}
    manifest = {"fact_kind": "observation", "entities": [], "existing_edges": []}
    result = {"relationships": [{"name": "legacy-dec", "rel_type": ONT.informed_by}]}

    ok = _apply(daemon, conn, rem_mod.KIND_DECISION, result, registry, manifest)

    assert ok is True
    assert called == []                                       # no ledger row
    assert any("ON CREATE SET" in c.args[0]
               for c in mock_session.run.call_args_list)      # edge still minted


def test_apply_existing_edges_not_rewritten(monkeypatch):
    """Delta principle: a proposed edge already in the manifest's existing set
    is neither verified nor re-written (operator assertions untouched)."""
    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, mock_session = _make_daemon()
    conn, _ = _make_conn()
    manifest = {"fact_kind": "observation", "entities": [],
                "existing_edges": [{"rel_type": ONT.entity_link, "target": "Neo4j",
                                    "asserted_by": "operator"}]}
    result = {"relationships": [{"name": "Neo4j", "rel_type": ONT.entity_link}]}

    ok = _apply(daemon, conn, rem_mod.KIND_DECISION, result, _registry(), manifest)

    assert ok is True
    assert not any("ON CREATE SET" in c.args[0]
                   for c in mock_session.run.call_args_list)


def test_apply_required_summary_missing_fails(monkeypatch):
    """A record over REM_SUMMARY_THRESHOLD whose result carries no summary is
    skipped (retries next cycle) — the summary was requested."""
    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, _ = _make_daemon()
    conn, _ = _make_conn()
    long = "x" * (rem_mod.REM_SUMMARY_THRESHOLD + 1)

    ok = _apply(daemon, conn, rem_mod.KIND_DECISION,
                {"relationships": []}, {}, {"fact_kind": "observation"},
                original=long)
    assert ok is False


def test_apply_unsolicited_summary_dropped(monkeypatch):
    """A short record never stores a volunteered summary — rem_summary must not
    appear anywhere in the write."""
    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, mock_session = _make_daemon()
    conn, _ = _make_conn()

    ok = _apply(daemon, conn, rem_mod.KIND_DECISION,
                {"relationships": [], "summary": "volunteered anyway"},
                {}, {"fact_kind": "observation"}, original="short")

    assert ok is True
    assert not any("rem_summary" in c.args[0]
                   for c in mock_session.run.call_args_list)


def test_apply_subtypes_only_untyped_entities(monkeypatch):
    """Delta sub-typing: an entity already carrying a sub-label is never
    reclassified; an untyped one is."""
    monkeypatch.setenv("MOCK_LLM", "1")
    daemon, mock_session = _make_daemon()
    conn, _ = _make_conn()
    registry = {
        "typedone":  {"label": ONT.entity, "default_rel": ONT.entity_link,
                      "typed": True, "pg_id": None},
        "untypedone": {"label": ONT.entity, "default_rel": ONT.entity_link,
                       "typed": False, "pg_id": None},
    }
    result = {"relationships": [
        {"name": "typedone",   "rel_type": ONT.entity_link, "type": ONT.system},
        {"name": "untypedone", "rel_type": ONT.entity_link, "type": ONT.component},
    ]}

    ok = _apply(daemon, conn, rem_mod.KIND_DECISION, result, registry,
                {"fact_kind": "observation"})

    assert ok is True
    sublabel_calls = [c for c in mock_session.run.call_args_list
                      if "SET e:" in c.args[0]]
    names = [n for c in sublabel_calls for n in c.kwargs["names"]]
    assert names == ["untypedone"]


# ── MOCK end-to-end cycle ─────────────────────────────────────────────────────

def test_process_fact_mock_end_to_end(monkeypatch):
    """Full _process_fact path under MOCK_LLM for a fact anchor: manifest in,
    verbatim content out, rem_processed set, outbox marked, NREM notified."""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setattr(rem_mod, "AUDIT_LOG_PATH", None)   # isolate from env leakage
    original = "a short curated fact"
    daemon, mock_session = _make_daemon([{"content": original}])  # consistency read
    conn, executed = _make_conn()

    manifest = rem_mod.build_manifest(
        {"content": original, "kind": "fact", "entities": ["Neo4j"],
         "source_ref": None}, [])

    loop = asyncio.new_event_loop()
    try:
        ok = loop.run_until_complete(daemon._process_fact(
            7, original, rem_mod.KIND_FACT, [], {}, conn, loop,
            manifest=manifest, run_id="cycle-run"))
    finally:
        loop.close()

    assert ok is True
    cyphers = [c.args[0] for c in mock_session.run.call_args_list]
    assert any("f.content = $orig" in c and "rem_processed" in c for c in cyphers)
    assert not any("rem_summary" in c for c in cyphers)       # short → no summary
    sqls = [s for s, _ in executed]
    assert any("rem_reviewed" in s for s in sqls)             # outbox marked
    assert any("pg_notify" in s for s in sqls)                # NREM notified
