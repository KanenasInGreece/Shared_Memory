"""
Tests for coordinator.py — Phase A: decision provenance validation and outbox dispatch.

Coverage:
  - handle_save: decision ingress validation (missing fields → 400)
  - handle_save: plain fact saves unchanged (no regression)
  - handle_save: valid decision save passes validation
  - _apply_outbox_row: dispatches to _apply_decision_outbox_row for type=decision
  - _apply_outbox_row: standard Fact path unchanged for plain facts
  - _apply_decision_outbox_row: writes correct Neo4j nodes and marks outbox applied
"""

import asyncio
import importlib.util
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ── Dynamic import (mirrors test_vector_skill.py pattern) ─────────────────────

def load_coordinator():
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    path = os.path.join(scripts_dir, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator", path)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules["coordinator"] = mod
    spec.loader.exec_module(mod)
    return mod

coordinator_mod = load_coordinator()
MemoryCoordinator = coordinator_mod.MemoryCoordinator
ONT = coordinator_mod.ONT


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_request(body: dict, authenticated_agent: str | None = None,
                  principal: dict | None = None) -> MagicMock:
    """Minimal aiohttp Request mock with an async .json() method.

    authenticated_agent: simulates the value set by auth_middleware after token validation.
    principal: the kernel-attested person identity dict (auth_middleware sets this from
        SO_PEERCRED). Key-specific so .get('principal') doesn't collide with
        .get('authenticated_agent'). Defaults to None (e.g. TCP transport).
    """
    state = {"authenticated_agent": authenticated_agent, "principal": principal}
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.rel_url.query.get = MagicMock(return_value=None)
    req.get = MagicMock(side_effect=lambda k, d=None: state.get(k, d))
    req.__getitem__ = MagicMock(side_effect=lambda k: state.get(k))
    return req


def _coordinator_with_mocks():
    """Return a MemoryCoordinator whose pool and neo4j are mocked out."""
    c = MemoryCoordinator()

    # asyncpg connection mock — transaction() must return an async ctx manager
    mock_conn = AsyncMock()
    mock_conn.fetchrow   = AsyncMock(return_value={"id": 99})
    # asyncpg's execute() returns the COMMAND STATUS STRING ("DELETE 3"), and
    # callers here parse it for a row count — the outbox recovery at startup and
    # the alternatives reconciler both do. A bare AsyncMock returns a mock whose
    # .split() is another mock, so a stub that omits this makes faithful code
    # look broken.
    mock_conn.execute    = AsyncMock(return_value="DELETE 0")
    mock_conn.fetch      = AsyncMock(return_value=[])
    mock_conn.transaction = MagicMock(return_value=_async_ctx(None))

    # asyncpg pool mock — acquire() must return an async ctx manager
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_async_ctx(mock_conn))
    c._pool = mock_pool

    # neo4j mock
    mock_session = AsyncMock()
    mock_session.run = AsyncMock()
    mock_neo4j = MagicMock()
    mock_neo4j.session = MagicMock(return_value=_async_ctx(mock_session))
    c._neo4j = mock_neo4j

    return c, mock_conn, mock_session


class _async_ctx:
    """Minimal async context manager wrapping a value."""
    def __init__(self, val):
        self._val = val
    async def __aenter__(self):
        return self._val
    async def __aexit__(self, *_):
        pass


# ── Ingress validation ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_decision_save_missing_all_required_fields_returns_400():
    c = MemoryCoordinator()
    req = _make_request({
        "content": "some decision content",
        "metadata": {
            "source": "claude-code",
            "type": "decision",
            "decision": {},          # missing decided_by, project, rationale
        },
    })
    resp = await c.handle_save(req)
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["status"] == "error"
    assert "decided_by" in body["message"]
    assert "project"    in body["message"]
    assert "rationale"  in body["message"]


@pytest.mark.asyncio
async def test_decision_save_missing_one_field_names_it_in_error():
    c = MemoryCoordinator()
    req = _make_request({
        "content": "decision without rationale",
        "metadata": {
            "source": "claude-code",
            "type": "decision",
            "decision": {"decided_by": "Xenofon", "project": "shared_memory"},
        },
    })
    resp = await c.handle_save(req)
    assert resp.status == 400
    body = json.loads(resp.text)
    # The dynamic missing-fields list should contain only 'rationale'
    assert "['rationale']" in body["message"]


@pytest.mark.asyncio
async def test_plain_fact_save_skips_decision_validation():
    """A save without type=decision must not be blocked by decision validation."""
    c, mock_conn, _ = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "plain fact content",
            "metadata": {"project": "shared-memory-GitHub", "source": "claude-code", "entities": ["SharedMemory"]},
        })
        resp = await c.handle_save(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["status"] == "success"


@pytest.mark.asyncio
async def test_handle_save_rejects_non_list_entities():
    """metadata.entities must be a list. A string silently iterated per-
    character (nonsensical, one lock per char); a non-iterable (e.g. an int)
    raised an unhandled TypeError from set(entities) — a bare 500 instead of
    the clean 400 every other malformed-metadata path in this handler returns.
    Must 400 before ever reaching the embedder."""
    c, _, _ = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)) as mock_embed:
        for bad in ("SharedMemory", 42, {"a": 1}):
            req = _make_request({
                "content": "some content",
                "metadata": {"source": "claude", "entities": bad},
            })
            resp = await c.handle_save(req)
            assert resp.status == 400, f"entities={bad!r} should 400, got {resp.status}"
        mock_embed.assert_not_called()


@pytest.mark.asyncio
async def test_valid_decision_save_passes_validation():
    c, mock_conn, _ = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "We decided to add a consolidation daemon.",
            "metadata": {
                "source": "claude-code",
                "type": "decision",
                "entities": ["Consolidator", "SharedMemory"],
                "decision": {
                    "title": "Add consolidation daemon",
                    "decided_by": "Xenofon",
                    "project": "shared_memory",
                    "rationale": "simulate dreaming; reduce hot-path latency",
                    "assisted_by": ["claude-code"],
                    "date": "2026-05-20",
                },
            },
        })
        resp = await c.handle_save(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["status"] == "success"
    assert "pg_id" in body


# ── Outbox dispatch ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_outbox_row_dispatches_decision_type():
    """_apply_outbox_row must delegate to _apply_decision_outbox_row for type=decision."""
    c = MemoryCoordinator()
    c._pool  = MagicMock()
    c._neo4j = MagicMock()

    params = {
        "type": "decision",
        "decision": {
            "decided_by": "Xenofon",
            "project": "shared_memory",
            "rationale": "simulate dreaming",
            "assisted_by": ["claude-code"],
        },
        "entities": ["Consolidator"],
        "source": "claude-code",
        "content_snippet": "We decided to add a consolidation daemon.",
    }

    with patch.object(c, "_apply_decision_outbox_row", new=AsyncMock()) as mock_dec:
        await c._apply_outbox_row(outbox_id=1, pg_id=42, params=params, retries=0)
        mock_dec.assert_awaited_once_with(1, 42, params)


@pytest.mark.asyncio
async def test_apply_outbox_row_plain_fact_does_not_call_decision_path():
    """_apply_outbox_row must NOT call _apply_decision_outbox_row for plain facts."""
    c, mock_conn, mock_session = _coordinator_with_mocks()

    params = {
        "type": "fact",
        "entities": ["SharedMemory"],
        "source": "claude-code",
        "content_snippet": "plain fact",
    }

    with patch.object(c, "_apply_decision_outbox_row", new=AsyncMock()) as mock_dec:
        await c._apply_outbox_row(outbox_id=2, pg_id=43, params=params, retries=0)
        mock_dec.assert_not_awaited()
        mock_session.run.assert_awaited()   # standard path ran


# ── Fact custody provenance (decision 915) ────────────────────────────────────
# NOTE: these pin the Python/params contract. The Cypher itself is STUBBED here
# (mock_session.run never executes it), so the delegation-pair semantics and the
# CASE gating (no edges for a 'coordinator' fallback source / absent person /
# absent project) are verified LIVE against Neo4j — a green unit proves the wiring,
# not the query. See the first-write custody verification (decision 915).

@pytest.mark.asyncio
async def test_handle_save_carries_fact_custody_into_outbox_params():
    """Ingress must stamp the DERIVED custody axes into the fact's outbox
    cypher_params: person from the kernel-attested principal, project from the
    (normalised) folder. These feed the WAS_ATTRIBUTED_TO / ACTED_ON_BEHALF_OF /
    PROJECT_OF edges the apply-path writes."""
    c, mock_conn, _ = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request(
            {
                "content": "a plain fact from a session",
                "metadata": {
                    "source": "claude",
                    "entities": ["SharedMemory"],
                    "project": "shared-memory-GitHub",
                },
            },
            principal={"user": "xenofon", "uid": 1000, "pid": 42},
        )
        resp = await c.handle_save(req)
    assert resp.status == 200
    # Find the neo4j_outbox INSERT and inspect the cypher_params dict it stored.
    outbox_params = None
    for call_obj in mock_conn.execute.await_args_list:
        sql = call_obj.args[0] if call_obj.args else ""
        if "neo4j_outbox" in sql:
            outbox_params = call_obj.args[2]
            break
    assert outbox_params is not None, "no neo4j_outbox INSERT was issued"
    assert outbox_params["person"] == "xenofon"          # kernel principal, not a client claim
    assert outbox_params["project"] == "shared-memory-GitHub"


@pytest.mark.asyncio
async def test_apply_outbox_row_fact_writes_custody_delegation_pair():
    """The standard fact apply-path Cypher must mint the delegation pair:
    (fact)-[WAS_ATTRIBUTED_TO]->(AIAgent), (AIAgent)-[ACTED_ON_BEHALF_OF]->(Human),
    (fact)-[PROJECT_OF]->(Project) — reading person/project/source from params.
    WAS_ATTRIBUTED_TO must NOT point at the human (decision 915: that would read
    as authorship, not custody)."""
    c, mock_conn, mock_session = _coordinator_with_mocks()

    params = {
        "type": "fact",
        "entities": ["SharedMemory"],
        "source": "claude",
        "person": "xenofon",
        "project": "shared-memory-GitHub",
        "content_snippet": "a plain fact",
    }
    await c._apply_outbox_row(outbox_id=7, pg_id=71, params=params, retries=0)

    # First session.run is the Fact MERGE (+ custody). Inspect its query + kwargs.
    first = mock_session.run.await_args_list[0]
    query = first.args[0]
    kwargs = first.kwargs
    assert "WAS_ATTRIBUTED_TO" in query
    assert "ACTED_ON_BEHALF_OF" in query
    assert "PROJECT_OF" in query
    # The attribution target is the AIAgent, and the human is reached via delegation.
    assert "MERGE (a:AIAgent {name: $source})" in query
    assert "-[:WAS_ATTRIBUTED_TO]->(a)" in query
    assert "(a)-[:ACTED_ON_BEHALF_OF]->(h)" in query
    # Never attribute a fact directly to the human (custody ≠ authorship).
    assert "-[:WAS_ATTRIBUTED_TO]->(h)" not in query
    assert kwargs["person"] == "xenofon"
    assert kwargs["project"] == "shared-memory-GitHub"
    assert kwargs["source"] == "claude"


# ── Decision Neo4j writes ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_decision_outbox_row_writes_correct_nodes():
    c, mock_conn, mock_session = _coordinator_with_mocks()

    params = {
        "decision": {
            "title": "Add consolidation daemon",
            "decided_by": "Xenofon",
            "project": "shared_memory",
            "rationale": "simulate dreaming",
            "assisted_by": ["claude-code"],
            "date": "2026-05-20",
        },
        "entities": ["Consolidator", "SharedMemory"],
        "source": "claude-code",
        "content_snippet": "We decided to add a consolidation daemon.",
    }

    await c._apply_decision_outbox_row(outbox_id=1, pg_id=42, params=params)

    # Three statements now: the Decision projection, then entity INHERITANCE,
    # then the DEFAULT-SECTION pass (028). The third runs unconditionally and
    # guards itself — a decision that asserted its own sections already carries
    # a bare DOMAIN_OF edge and the query declines; this one asserted none, so
    # it takes its grounding facts'.
    assert mock_session.run.await_count == 3
    cypher_call = mock_session.run.call_args_list[0]
    cypher = cypher_call.args[0]
    assert "Decision" in cypher
    assert "Human"    in cypher
    assert "Project"  in cypher
    assert "AIAgent"  in cypher
    assert "WAS_ATTRIBUTED_TO" in cypher
    assert "PROJECT_OF"        in cypher
    assert "WAS_ASSISTED_BY"   in cypher

    # Kwargs should carry all required values
    kwargs = cypher_call.kwargs
    assert kwargs["decided_by"] == "Xenofon"
    assert kwargs["project"]    == "shared_memory"
    assert kwargs["rationale"]  == "simulate dreaming"
    assert kwargs["assisted_by"] == ["claude-code"]

    # A decision MINTS NO ENTITIES: the caller's names are kept in Postgres but
    # never projected, and the projection can no longer create an :Entity node.
    assert "entities" not in kwargs
    assert f"MERGE (e:{ONT.entity}" not in cypher

    # Outbox row should be marked applied
    mock_conn.execute.assert_awaited()
    execute_sql = mock_conn.execute.call_args.args[0]
    assert "applied" in execute_sql


@pytest.mark.asyncio
async def test_a_decisions_payload_is_not_copied_into_the_graph():
    """confidence + alternatives are PAYLOAD: nothing walks on them, so the
    node carries the pg_id and Postgres carries the values.

    A second copy of a value nobody filters or orders on buys nothing the
    pg_id does not already give, and guarantees a divergence class — measured
    live before this shipped as Postgres 236 decisions with a confidence
    against the graph's 85, a clean cutover with no writer able to close it.
    Both the SET clause and the parameter must be gone: a parameter passed to
    a query that no longer names it is the residue that invites the clause
    back.
    """
    c, _, mock_session = _coordinator_with_mocks()
    await c._apply_decision_outbox_row(outbox_id=3, pg_id=60, params={
        "decision": {
            "title": "Dereference the payload",
            "decided_by": "Xenofon",
            "project": "shared-memory-GitHub",
            "rationale": "the copy earns nothing the pg_id does not give",
            "confidence": "high",
            "alternatives": ["keep projecting it from the node"],
        },
        "source": "claude-code",
        "content_snippet": "We decided to dereference the payload.",
    })
    cypher = mock_session.run.call_args_list[0].args[0]
    kwargs = mock_session.run.call_args_list[0].kwargs
    assert "d.alternatives" not in cypher
    assert "d.confidence" not in cypher
    assert "alternatives" not in kwargs
    assert "confidence" not in kwargs
    # The identity the payload is reached BY is still written.
    assert "pg_id: $pg_id" in cypher
    assert kwargs["pg_id"] == 60


@pytest.mark.asyncio
async def test_apply_decision_outbox_row_handles_empty_assisted_by():
    """Empty assisted_by must not crash (FOREACH handles empty lists in Cypher)."""
    c, mock_conn, mock_session = _coordinator_with_mocks()

    params = {
        "decision": {
            "decided_by": "Xenofon",
            "project": "shared_memory",
            "rationale": "test",
            "assisted_by": [],
        },
        "entities": [],
        "source": "Xenofon",
        "content_snippet": "manual decision",
    }

    await c._apply_decision_outbox_row(outbox_id=2, pg_id=50, params=params)
    # projection + entity inheritance + default-section pass (028)
    assert mock_session.run.await_count == 3
    mock_conn.execute.assert_awaited()


# ── decided_by normalisation onto the attested principal ─────────────────────

_norm = coordinator_mod._normalise_decided_by


def _decision_meta(decided_by, principal="xenofon"):
    md = {"type": "decision", "decision": {"decided_by": decided_by,
                                           "project": "p", "rationale": "r"}}
    if principal is not None:
        md["principal"] = principal
    return md


@pytest.mark.parametrize("claimed", [
    "Xenofon",                      # case variant
    "Xenofon + Antigravity",        # operator compounded with the assisting agent
    "Xenofon & Antigravity",
    "xenofon & Cloe & Gemini",
    "Antigravity",                  # the agent claimed the decision outright
    "  Xenofon  ",                  # stray whitespace
])
def test_decided_by_collapses_onto_the_principal(claimed):
    """Every spelling of one operator must land on the SAME person, or a single
    operator's decisions report as several distinct humans in Tier-3 provenance."""
    md = _decision_meta(claimed)
    assert _norm(md) is True
    assert md["decision"]["decided_by"] == "xenofon"
    assert md["decision"]["decided_by_claimed"] == claimed.strip()


def test_decided_by_already_canonical_is_left_untouched():
    """No rewrite, no audit field, no log line when the claim already matches."""
    md = _decision_meta("xenofon")
    assert _norm(md) is False
    assert md["decision"]["decided_by"] == "xenofon"
    assert "decided_by_claimed" not in md["decision"]


def test_decided_by_without_a_principal_is_never_guessed():
    """TCP transport carries no kernel credential. The claim stands as given —
    the same 'honestly unknown, never guessed' rule _apply_principal follows."""
    md = _decision_meta("Xenofon + Antigravity", principal=None)
    assert _norm(md) is False
    assert md["decision"]["decided_by"] == "Xenofon + Antigravity"
    assert "decided_by_claimed" not in md["decision"]


def test_normalisation_ignores_non_decision_records():
    """Facts and retrospectives carry no decided_by — the principal is their whole
    person axis, and this must not invent a decision object on them."""
    md = {"type": "retrospective", "principal": "xenofon"}
    assert _norm(md) is False
    assert md == {"type": "retrospective", "principal": "xenofon"}


@pytest.mark.asyncio
async def test_non_string_decided_by_is_rejected_not_silently_destroyed():
    """A list is truthy, so it used to pass the required-field check and then be
    overwritten by the principal — and _normalise_decided_by can only preserve a
    STRING claim, so the operator's wording was lost with no trace. Refuse it
    while the caller still holds the value."""
    c, _, _ = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request(
            {"content": "x",
             "metadata": {"source": "claude", "type": "decision",
                          "decision": {"decided_by": ["Xenofon"],
                                       "project": "p", "rationale": "r"}}},
            principal={"user": "xenofon"},
        )
        resp = await c.handle_save(req)
    assert resp.status == 400
    assert "decided_by" in resp.text


# ── supersession is the FACT lifecycle — judgements are refused ──────────────

_sup_err = coordinator_mod._supersession_target_error


def test_a_decision_may_not_be_superseded_directly():
    """Overturning a decision goes through a retrospective rated 'reversed',
    which marks it superseded as the CONSEQUENCE of a verdict that stays in the
    graph. A direct retract would delete the reasoning instead of recording that
    it was overturned — and leave no verdict a successor could ground on."""
    msg = _sup_err(42, "decision")
    assert msg and "reversed" in msg


def test_a_retrospective_may_not_be_superseded():
    """A retrospective is dated to when it was made; a changed outcome is a NEW
    retrospective. Inheritance already prefers the latest live verdict, so
    nothing needs retracting for the newer judgement to take effect."""
    msg = _sup_err(900, "retrospective")
    assert msg and "new retrospective" in msg.lower()


@pytest.mark.parametrize("kind", [None, "fact", "", "  Fact  "])
def test_plain_facts_remain_supersedable(kind):
    """Supersession is the fact lifecycle and must stay open for facts —
    including legacy rows whose type is NULL."""
    assert _sup_err(7, kind) is None


@pytest.mark.asyncio
async def test_handle_supersede_refuses_a_decision_target():
    """The guard is enforced at the ROUTE, not merely available as a helper."""
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetchrow = AsyncMock(return_value={"superseded": False, "type": "decision"})
    resp = await c.handle_supersede(_make_request({"pg_id": 42}))
    assert resp.status == 400
    assert "reversed" in resp.text


@pytest.mark.asyncio
async def test_missing_decided_by_still_400s_rather_than_being_filled_in():
    """Order matters: normalisation runs AFTER validation, so an agent that omits
    the field is told so instead of having the socket answer for it."""
    c, _, _ = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request(
            {"content": "x",
             "metadata": {"source": "claude", "type": "decision",
                          "decision": {"project": "p", "rationale": "r"}}},
            principal={"user": "xenofon"},
        )
        resp = await c.handle_save(req)
    assert resp.status == 400
    assert "decided_by" in resp.text


# ── Decision entity INHERITANCE (decisions traverse to facts, never mint) ────

class _TieredSession:
    """Neo4j session stub returning a scripted per-call `n` from .single().

    `yields` is one entry per session.run() call, in order. Every executed
    Cypher string is recorded so the tier that fired can be identified.
    """
    def __init__(self, yields):
        self._yields = list(yields)
        self.cyphers = []

    async def run(self, cypher, **kwargs):
        self.cyphers.append(cypher)
        n = self._yields.pop(0) if self._yields else 0
        result = AsyncMock()
        result.single = AsyncMock(return_value={"n": n})
        return result


@pytest.mark.asyncio
async def test_inheritance_prefers_operator_grounding_and_stops_there():
    """Tier 1 (operator-asserted GROUNDED_IN) wins outright — the weaker tiers
    must not even be queried, or a system_default edge could dilute the topics
    an operator explicitly grounded the decision in."""
    session = _TieredSession([4])
    n = await MemoryCoordinator._inherit_entities_from_facts(session, 42)

    assert n == 4
    assert len(session.cyphers) == 1
    assert "g.asserted_by = 'operator'" in session.cyphers[0]


@pytest.mark.asyncio
async def test_inheritance_falls_back_to_system_then_retrospective():
    """A decision with no operator grounding falls to system_default/legacy
    edges; one with neither reaches facts only through its retrospective."""
    session = _TieredSession([0, 3])
    assert await MemoryCoordinator._inherit_entities_from_facts(session, 42) == 3
    assert len(session.cyphers) == 2
    assert "coalesce(g.asserted_by, '') <> 'operator'" in session.cyphers[1]

    session = _TieredSession([0, 0, 2])
    assert await MemoryCoordinator._inherit_entities_from_facts(session, 42) == 2
    assert len(session.cyphers) == 3
    retro_cypher = session.cyphers[2]
    assert ONT.had_outcome in retro_cypher
    assert ONT.retrospective in retro_cypher
    # Ties break on the retro's OWN id — the decision's id cannot disambiguate
    # its own retrospectives — and a dateless retro must sort LAST, which is not
    # where a bare `o.date DESC` would put null.
    assert "ORDER BY coalesce(o.date, '') DESC, o.pg_id DESC" in retro_cypher
    # The ordering runs AFTER the topic match, so the newest retrospective that
    # actually reaches facts wins. Ordering first would let an ungrounded newest
    # verdict be picked and blank the tier while a grounded sibling sat there.
    assert retro_cypher.index("collect(DISTINCT [e,") < retro_cypher.index("ORDER BY")
    # Forward guard only: nothing sets `superseded` on a :Retrospective today
    # (the reversal marks the DECISION), so this filter is currently inert — it
    # is not evidence that a retracted verdict is excluded.
    assert "coalesce(o.superseded, false) = false" in retro_cypher


@pytest.mark.asyncio
async def test_retrospective_inherits_by_the_same_rule_hopping_the_other_way():
    """Retrospectives mint no entities either, and share the writer. Their
    outcome tier traverses HAD_OUTCOME BACKWARDS — a retrospective judges one
    decision, so there is nothing to order or supersede-filter on that hop."""
    session = _TieredSession([0, 0, 5])
    n = await MemoryCoordinator._inherit_entities_from_facts(
        session, 900, ONT.retrospective)

    assert n == 5
    for cypher in session.cyphers:
        assert f"MATCH (a:{ONT.retrospective} {{pg_id: $pg_id}})" in cypher
        assert f"MERGE (e:{ONT.entity}" not in cypher
    outcome_cypher = session.cyphers[2]
    assert f"<-[:{ONT.had_outcome}]-(o:{ONT.decision})" in outcome_cypher
    # the decision-only ordering must NOT leak onto this hop
    assert "ORDER BY" not in outcome_cypher
    # A reversing verdict marks its target decision superseded moments earlier in
    # the same projection. Filtering the judgement here would blank the topics of
    # the very record doing the reversing, so this hop is deliberately unfiltered.
    assert "o.superseded" not in outcome_cypher


@pytest.mark.asyncio
async def test_inheritance_stamps_the_copy_it_writes():
    """A judgement's copy of its evidence's topic is STAMPED (989). It used to
    be written bare — and a bare MENTIONS is exactly the signature first write
    leaves when the OPERATOR names a concept on a fact. The two were therefore
    indistinguishable, which is how machine-added names came to read as
    first-write namings and re-qualified themselves as link targets.

    Standing carries across rather than being re-derived: any operator source
    (null confidence) makes the copy operator-grade; otherwise it takes the
    strongest machine confidence among its sources."""
    session = _TieredSession([0, 0, 0])
    await MemoryCoordinator._inherit_entities_from_facts(session, 42)

    for cypher in session.cyphers:
        assert f"asserted_by = '{coordinator_mod.RELATION_ASSERTED_INHERITED}'" in cypher
        # the source edge is bound and its confidence collected per entity
        assert "fe.confidence" in cypher
        # ⚠ Found on the LIVE graph, not here: `collect()` DISCARDS nulls, so an
        # all-operator source set collects EMPTY and a null-member test never
        # fires — every operator naming then inherited confidence 0.0, which is
        # numeric, machine-grade and below every threshold. The null signal must
        # come from comparing counts, never from inspecting the collected list.
        assert "count(*) AS srcs" in cypher and "size(cs) < srcs THEN null" in cypher
        assert "IN cs WHERE z IS NULL" not in cypher
        # ON CREATE only — an edge already there (an operator's own, or one from
        # an earlier projection) is never rewritten or downgraded
        assert "ON CREATE SET" in cypher


@pytest.mark.asyncio
async def test_inheritance_walks_every_grounding_role_not_just_grounded_in():
    """The defect this closes: four of the six role words produce a relationship
    that is NOT GROUNDED_IN, and INFORMED_BY is what a discussion-kind fact
    defaults to when the operator names no role at all — the bare-pg_id path the
    skill documents. Matching GROUNDED_IN alone made a decision that cited its
    evidence inherit nothing and never reach consolidation."""
    from ontology import GROUNDING_ROLES, GROUNDING_RELATIONS

    session = _TieredSession([0, 0, 0])
    await MemoryCoordinator._inherit_entities_from_facts(session, 42)

    # every relationship any role can produce must appear in every tier
    assert set(GROUNDING_RELATIONS) >= set(GROUNDING_ROLES.values())
    for cypher in session.cyphers:
        for rel in GROUNDING_RELATIONS:
            assert rel in cypher, f"{rel} missing — a {rel} grounding donates nothing"
    for rel in ("CONSIDERED", "REJECTED", "UNDER_CONDITIONS", "INFORMED_BY"):
        assert rel in GROUNDING_RELATIONS


@pytest.mark.asyncio
async def test_inheritance_passes_through_a_cited_judgement_to_its_facts():
    """Provenance allows grounding a decision on an earlier decision or on the
    retrospective that overturned it. That citation must still reach topics —
    through the cited record's OWN facts, never by copying the labels it carries
    (REM may have added those). `*0..1` is the whole mechanism: zero hops when
    the target is the fact itself, one when it is a judgement."""
    session = _TieredSession([0, 0, 0])
    await MemoryCoordinator._inherit_entities_from_facts(session, 42)

    for cypher in session.cyphers:
        assert "*0..1" in cypher
        # the walk always TERMINATES on a fact — judgements only pass through
        assert f"(f:{ONT.fact})" in cypher


@pytest.mark.asyncio
async def test_inheritance_filters_superseded_facts_but_not_judgements():
    """A retracted FACT must stop being a cluster key for everything that cited
    it. A superseded JUDGEMENT is different: a decision is overturned by a
    reversing retrospective, and a successor grounded on the decision it replaces
    is still ABOUT what that decision was about."""
    session = _TieredSession([0, 0, 0])
    await MemoryCoordinator._inherit_entities_from_facts(session, 42)

    for cypher in session.cyphers:
        assert "coalesce(f.superseded, false) = false" in cypher
        # no filter on the pass-through target
        assert "coalesce(t.superseded" not in cypher


@pytest.mark.asyncio
async def test_inheritance_never_mints_an_entity_on_any_tier():
    """The whole point of the rule: every tier MATCHes existing Entity nodes and
    MERGEs only the relationship. A `MERGE (e:Entity {name: ...})` on any tier
    would reopen the free-text faucet this change closed."""
    session = _TieredSession([0, 0, 0])
    assert await MemoryCoordinator._inherit_entities_from_facts(session, 42) == 0
    assert len(session.cyphers) == 3
    for cypher in session.cyphers:
        assert f"MERGE (e:{ONT.entity}" not in cypher
        assert f"MERGE (x:{ONT.entity}" not in cypher
        assert f"({ONT.entity} {{name:" not in cypher
        # topics are only ever read off facts
        assert f"(f:{ONT.fact})" in cypher


@pytest.mark.asyncio
async def test_decision_inheritance_runs_after_grounding_is_written():
    """Order matters: the traversal reads GROUNDED_IN edges, so it must run
    after _write_typed_grounding — otherwise a first write inherits nothing."""
    c, _, mock_session = _coordinator_with_mocks()
    calls = []
    with patch.object(c, "_write_typed_grounding",
                      new=AsyncMock(side_effect=lambda *a, **k: calls.append("grounding"))), \
         patch.object(c, "_inherit_entities_from_facts",
                      new=AsyncMock(side_effect=lambda *a, **k: calls.append("inherit"))):
        await c._apply_decision_outbox_row(
            outbox_id=1, pg_id=42,
            params={"decision": {"decided_by": "X", "project": "p"},
                    "grounded": [{"pg_id": 7, "rel": ONT.grounded_in,
                                  "asserted_by": "operator", "label": ONT.fact}]},
        )
    assert calls == ["grounding", "inherit"]


@pytest.mark.asyncio
async def test_retrospective_projection_inherits_for_itself_and_its_target():
    """The outcome tier can never fire at decision first write — no
    retrospective exists yet. The retrospective projection is the moment it
    becomes possible, so it must run inheritance twice: once for itself, once
    for the decision it judges."""
    c, _, mock_session = _coordinator_with_mocks()
    with patch.object(c, "_inherit_entities_from_facts", new=AsyncMock()) as inherit:
        await c._apply_retrospective_outbox_row(
            outbox_id=1, pg_id=900,
            params={"v": 2, "target_pg_id": 42, "entities": ["Ignored"],
                    "retrospective": {"rating": "held", "date": "2026-07-30"}},
        )
    assert [(call.args[1], call.args[2]) for call in inherit.await_args_list] == [
        (900, ONT.retrospective),   # itself, from its own grounding
        (42,  ONT.decision),        # then the decision it judges
    ]


@pytest.mark.asyncio
async def test_retrospective_projection_mints_no_entities():
    """The caller may still send `entities` (older clients do); the graph must
    ignore them exactly as the decision path now does."""
    c, _, mock_session = _coordinator_with_mocks()
    with patch.object(c, "_inherit_entities_from_facts", new=AsyncMock()):
        await c._apply_retrospective_outbox_row(
            outbox_id=1, pg_id=900,
            params={"v": 2, "target_pg_id": 42,
                    "entities": ["FreeTextName", "AnotherOne"],
                    "retrospective": {"rating": "held", "date": "2026-07-30"}},
        )
    node_call = mock_session.run.call_args_list[0]
    assert f"MERGE (r:{ONT.retrospective}" in node_call.args[0]
    assert f"MERGE (e:{ONT.entity}" not in node_call.args[0]
    assert "entities" not in node_call.kwargs


# ── Retrospective outbox dispatch and Neo4j writes (Phase C) ─────────────────

@pytest.mark.asyncio
async def test_apply_outbox_row_dispatches_retrospective_type():
    """_apply_outbox_row must delegate to _apply_retrospective_outbox_row for type=retrospective."""
    c = MemoryCoordinator()
    c._pool  = MagicMock()
    c._neo4j = MagicMock()

    params = {
        "type": "retrospective",
        "target_pg_id": 42,
        "retrospective": {"rating": "high", "date": "2026-05-29", "notes": "Held up well."},
        "source": "claude-code",
    }

    with patch.object(c, "_apply_retrospective_outbox_row", new=AsyncMock()) as mock_retro:
        await c._apply_outbox_row(outbox_id=10, pg_id=42, params=params, retries=0)
        mock_retro.assert_awaited_once_with(10, 42, params)


@pytest.mark.asyncio
async def test_apply_retrospective_outbox_row_creates_had_outcome():
    """_apply_retrospective_outbox_row must issue a HAD_OUTCOME CREATE and mark the outbox row applied."""
    c, mock_conn, mock_session = _coordinator_with_mocks()

    params = {
        "type": "retrospective",
        "target_pg_id": 42,
        "retrospective": {"rating": "high", "date": "2026-05-29", "notes": "Held up well."},
        "source": "claude-code",
    }

    await c._apply_retrospective_outbox_row(outbox_id=10, pg_id=42, params=params)

    assert mock_session.run.await_count == 1
    cypher_call = mock_session.run.call_args
    cypher = cypher_call.args[0]
    assert "Decision" in cypher
    assert "HAD_OUTCOME" in cypher
    assert "CREATE" in cypher

    kwargs = cypher_call.kwargs
    assert kwargs["pg_id"]  == 42
    assert kwargs["rating"] == "high"
    assert kwargs["notes"]  == "Held up well."

    mock_conn.execute.assert_awaited()
    execute_sql = mock_conn.execute.call_args.args[0]
    assert "applied" in execute_sql


@pytest.mark.asyncio
async def test_apply_retrospective_v2_creates_node_and_trigger_edge():
    """v2 payload (retro-as-record): MERGE the :Retrospective node under the
    retro's OWN pg_id, link the target Decision via HAD_OUTCOME, and write the
    typed grounding ROLE edges via the shared writer."""
    c, mock_conn, mock_session = _coordinator_with_mocks()

    params = {
        "v": 2,
        "type": "retrospective",
        "target_pg_id": 42,
        "retrospective": {"rating": "validated", "date": "2026-07-15"},
        "content_snippet": "held up well",
        "source": "claude",
        "entities": ["OutboxPattern"],
        "fact_kind": "tested",
        "grounded": [{"pg_id": 601, "rel": "GROUNDED_IN",
                      "asserted_by": "operator", "label": "Fact"}],
    }

    await c._apply_retrospective_outbox_row(outbox_id=11, pg_id=913, params=params)

    cyphers = [call.args[0] for call in mock_session.run.await_args_list]
    node_q = cyphers[0]
    assert "MERGE (r:Retrospective {pg_id: $pg_id}" in node_q
    assert "r.rating" in node_q and "r.fact_kind" in node_q
    # The node projection no longer mints topics — a retrospective inherits them
    # from its facts like a decision does (see the inheritance tests above).
    assert "MENTIONS" not in node_q
    edge_q = cyphers[1]
    assert "MATCH (d:Decision {pg_id: $target}" in edge_q
    assert "HAD_OUTCOME" in edge_q and "MERGE" in edge_q
    assert not any("CREATE (d)-[:HAD_OUTCOME" in q for q in cyphers), \
        "v2 must never write the legacy self-loop"
    grounding_q = cyphers[2]
    assert "apoc.merge.relationship" in grounding_q and "Retrospective" in grounding_q

    kwargs = mock_session.run.await_args_list[1].kwargs
    assert kwargs["target"] == 42 and kwargs["pg_id"] == 913


@pytest.mark.asyncio
async def test_apply_retrospective_v2_reversal_marks_decision_superseded():
    c, _, mock_session = _coordinator_with_mocks()
    params = {
        "v": 2, "type": "retrospective", "target_pg_id": 42,
        "retrospective": {"rating": "reversed", "date": "2026-07-15",
                          "superseded": True},
        "content_snippet": "withdrawn", "source": "claude", "entities": [],
    }
    await c._apply_retrospective_outbox_row(outbox_id=12, pg_id=914, params=params)
    edge_q = mock_session.run.await_args_list[1].args[0]
    assert "SET d.superseded = true" in edge_q


@pytest.mark.asyncio
async def test_handle_retrospective_missing_fields_returns_400():
    """handle_retrospective must return 400 when rating or notes are absent."""
    c, mock_conn, _ = _coordinator_with_mocks()

    req = _make_request({"pg_id": 42, "rating": "", "notes": ""})
    resp = await c.handle_retrospective(req)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["status"] == "error"


@pytest.mark.asyncio
async def test_handle_retrospective_rejects_bool_pg_id():
    """bool is an int subclass in Python (isinstance(True, int) is True) — every
    sibling handler (handle_save's supersedes, handle_supersede's pg_id/by,
    handle_review_hold's summary_id/pg_id) explicitly excludes it. This one
    didn't: {"pg_id": true, ...} must 400, not reach the DB with a bool bound
    to an integer column."""
    c, mock_conn, _ = _coordinator_with_mocks()

    req = _make_request({"pg_id": True, "rating": "validated", "notes": "n"})
    resp = await c.handle_retrospective(req)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["status"] == "error"
    mock_conn.fetchval.assert_not_called()


@pytest.mark.asyncio
async def test_handle_retrospective_rejects_non_list_entities():
    """Same class of gap as handle_save: entities must be a list. A bare int
    (or any non-iterable) would otherwise raise an unhandled TypeError from
    the `for e in (raw_entities or [])` comprehension — a bare 500 instead
    of the clean 400 every other malformed-field path returns."""
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetchval = AsyncMock(return_value=1)   # pg_id exists, if reached

    req = _make_request({
        "pg_id": 42, "rating": "validated", "notes": "n", "entities": 42,
    })
    resp = await c.handle_retrospective(req)
    assert resp.status == 400
    mock_conn.fetchval.assert_not_called()


@pytest.mark.asyncio
async def test_handle_supersede_rejects_already_superseded_successor():
    """A stale multi-hop chain (A -> B -> C, where B is itself already
    superseded) must be rejected: handle_supersede checked `by` exists but
    never checked whether IT was already superseded, unlike the parallel check
    on `pg_id` two lines above and handle_save's `supersedes` validation."""
    c, mock_conn, _ = _coordinator_with_mocks()
    # First fetchrow: the pg_id (A) being retracted — not yet superseded.
    # Second fetchrow: the successor (B) being pointed at — ALREADY superseded.
    mock_conn.fetchrow = AsyncMock(side_effect=[
        {"superseded": False, "type": None},
        {"superseded": True},
    ])

    req = _make_request({"pg_id": 10, "by": 20})
    resp = await c.handle_supersede(req)

    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["status"] == "error"
    assert "already superseded" in body["message"]
    mock_conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_handle_supersede_accepts_live_successor():
    """Sanity: a successor that is NOT superseded must still be accepted (no
    regression from the new check)."""
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetchrow = AsyncMock(side_effect=[
        {"superseded": False, "type": None},
        {"superseded": False},
    ])
    mock_conn.fetchval = AsyncMock(return_value=None)   # ride-along probe: no live outbox row
    mock_conn.fetch    = AsyncMock(return_value=[])     # purge query

    req = _make_request({"pg_id": 10, "by": 20})
    resp = await c.handle_supersede(req)

    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["status"] == "success"
    assert body["superseded"] == 10
    assert body["superseded_by"] == 20


# ── Recency-aware retrieval (retro-as-record stage 4) ─────────────────────────

def test_rerank_doc_text_prepends_date_for_outcome_records():
    from datetime import datetime
    ts = datetime(2026, 7, 14, 12, 0)
    out = coordinator_mod._rerank_doc_text(
        "held up well", {"type": "retrospective"}, ts)
    assert out.startswith("[retrospective recorded 2026-07-14]")
    out_d = coordinator_mod._rerank_doc_text(
        "we chose X", {"type": "decision"}, ts)
    assert out_d.startswith("[decision recorded 2026-07-14]")


def test_rerank_doc_text_leaves_facts_untouched():
    from datetime import datetime
    assert coordinator_mod._rerank_doc_text(
        "a plain fact", {"type": None, "source": "x"}, datetime(2026, 1, 1)
    ) == "a plain fact"
    assert coordinator_mod._rerank_doc_text(
        "no date", {"type": "decision"}, None) == "no date"


def test_order_retros_latest_first_same_decision_only():
    """Several retros of the SAME decision reorder newest-first in the
    positions they occupy; everything else keeps the reranker's order."""
    results = [
        {"tier": "fact", "metadata": {"type": None}, "created_at": "2026-01-01"},
        {"tier": "fact", "metadata": {"type": "retrospective", "target_pg_id": 42},
         "created_at": "2026-06-01", "content": "old verdict"},
        {"tier": "fact", "metadata": {"type": "retrospective", "target_pg_id": 99},
         "created_at": "2026-05-01", "content": "other decision"},
        {"tier": "fact", "metadata": {"type": "retrospective", "target_pg_id": 42},
         "created_at": "2026-07-14", "content": "new verdict"},
    ]
    out = coordinator_mod._order_retros_latest_first(results)
    assert out[0] is results[0]                       # non-retro untouched
    assert out[1]["content"] == "new verdict"         # newest takes the earlier slot
    assert out[2] is results[2]                       # different decision untouched
    assert out[3]["content"] == "old verdict"
    assert coordinator_mod._order_retros_latest_first([]) == []


# ── Pure function tests — Fix 1: retrieval visibility ─────────────────────────

def test_sigmoid_midpoint():
    assert coordinator_mod._sigmoid(0.0) == pytest.approx(0.5)


def test_sigmoid_large_positive_approaches_one():
    assert coordinator_mod._sigmoid(10.0) > 0.99


def test_sigmoid_large_negative_approaches_zero():
    assert coordinator_mod._sigmoid(-10.0) < 0.01


def test_sigmoid_score_2_is_in_unit_interval():
    v = coordinator_mod._sigmoid(2.0)
    assert 0.0 < v < 1.0


def test_matched_entities_single_match():
    meta = {"entities": ["OutboxPattern", "Neo4j", "Postgres"]}
    result = coordinator_mod._matched_entities("query about Neo4j consolidation", meta)
    assert result == ["Neo4j"]


def test_matched_entities_empty_entity_list():
    assert coordinator_mod._matched_entities("anything", {"entities": []}) == []


def test_matched_entities_none_metadata():
    assert coordinator_mod._matched_entities("anything", None) == []


def test_matched_entities_missing_key():
    assert coordinator_mod._matched_entities("anything", {}) == []


def test_matched_entities_multiple_matches():
    meta = {"entities": ["Neo4j", "Postgres", "BGE-M3"]}
    result = coordinator_mod._matched_entities("Neo4j and Postgres together", meta)
    assert "Neo4j"   in result
    assert "Postgres" in result
    assert "BGE-M3"  not in result


def test_matched_entities_case_insensitive():
    meta = {"entities": ["SharedMemory"]}
    result = coordinator_mod._matched_entities("query about sharedmemory", meta)
    assert result == ["SharedMemory"]


# ── source_ref propagation — Fix 3: lineage ──────────────────────────────────

@pytest.mark.asyncio
async def test_save_propagates_source_ref_to_outbox_cypher_params():
    """source_ref in metadata must appear in the outbox cypher_params JSON."""
    c, mock_conn, _ = _coordinator_with_mocks()

    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "Fact with sub-document reference",
            "metadata": {"project": "shared-memory-GitHub", 
                "source": "claude-code",
                "entities": ["SharedMemory"],
                "source_ref": "design-doc.pdf#p12",
            },
        })
        resp = await c.handle_save(req)

    assert resp.status == 200

    # Find the outbox INSERT among the execute() calls and verify source_ref is present.
    outbox_call = next(
        (c for c in mock_conn.execute.call_args_list
         if "neo4j_outbox" in c.args[0]),
        None,
    )
    assert outbox_call is not None, "outbox INSERT not found in execute() calls"
    # args: (sql, pg_id, cypher_params) — cypher_params is args[2], bound as a dict
    params = outbox_call.args[2]   # bound as a dict; asyncpg jsonb codec serialises it
    assert params["source_ref"] == "design-doc.pdf#p12"


@pytest.mark.asyncio
async def test_save_without_source_ref_stores_none_in_outbox():
    """Saves without source_ref must not crash — outbox params carry source_ref=None."""
    c, mock_conn, _ = _coordinator_with_mocks()

    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "Plain fact with no source reference",
            "metadata": {"project": "shared-memory-GitHub", "source": "claude-code", "entities": ["SharedMemory"]},
        })
        resp = await c.handle_save(req)

    assert resp.status == 200

    outbox_call = next(
        (c for c in mock_conn.execute.call_args_list
         if "neo4j_outbox" in c.args[0]),
        None,
    )
    assert outbox_call is not None
    params = outbox_call.args[2]   # bound as a dict; asyncpg jsonb codec serialises it
    assert params["source_ref"] is None


# ── Search response shape — Fix 1: retrieval visibility ──────────────────────

class _AsyncIter:
    """Minimal async iterable that yields zero items — simulates empty Neo4j result."""
    def __aiter__(self):
        return self
    async def __anext__(self):
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_search_rejects_non_integer_limit():
    """A non-integer limit (e.g. a string, or a client-sent None) must 400, not
    raise an unhandled TypeError from int(None)/int('abc') and surface as a
    bare 500 — every other malformed-field path in this file returns a clean
    400 instead."""
    c, _, _ = _coordinator_with_mocks()
    for bad in ("abc", None, [5], 5.5, True):
        req = _make_request({"query": "anything", "limit": bad})
        resp = await c.handle_search(req)
        assert resp.status == 400, f"limit={bad!r} should 400, got {resp.status}"


@pytest.mark.asyncio
async def test_search_response_fact_carries_tier_and_normalized_score():
    """Fact results must include tier='fact', score_normalized in (0,1), matched_entities list."""
    c, mock_conn, mock_session = _coordinator_with_mocks()

    # Tier 3: fetchrow is called twice — nearest insight (none here), then
    # the top thematic community summary (metadata + source_pg_ids).
    mock_conn.fetchrow = AsyncMock(side_effect=[None, {
        "content": "Global context summary",
        "metadata": {"entity": "Neo4j", "domain": "general"},
        "source_pg_ids": [10, 11, 12],
    }])
    # Tier 1: one candidate
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content": "fact about Neo4j outbox", "metadata": {"entities": ["Neo4j"], "source": "claude-code"}},
    ])
    # Neo4j expansion: no related nodes (empty async iterator)
    mock_session.run = AsyncMock(return_value=_AsyncIter())

    mock_reranker = MagicMock()
    mock_reranker.raise_for_status = MagicMock()
    mock_reranker.json = MagicMock(return_value={
        "results": [{"index": 0, "relevance_score": 2.0}]
    })

    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_reranker)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_cls.return_value.__aexit__  = AsyncMock(return_value=None)

            req = _make_request({"query": "Neo4j outbox", "limit": 5})
            resp = await c.handle_search(req)

    assert resp.status == 200
    body    = json.loads(resp.text)
    results = body["results"]

    # Community summary is prepended
    assert results[0]["tier"] == "community_summary"
    assert results[0]["graph_context"] == []

    # Fact result shape
    fact = results[1]
    assert fact["tier"] == "fact"
    assert isinstance(fact["score_normalized"], float)
    assert 0.0 < fact["score_normalized"] < 1.0
    assert isinstance(fact["matched_entities"], list)
    assert "Neo4j" in fact["matched_entities"]
    assert isinstance(fact["graph_context"], list)


@pytest.mark.asyncio
async def test_search_response_community_summary_has_tier_field():
    """The community summary prepended to results must carry tier='community_summary'."""
    c, mock_conn, mock_session = _coordinator_with_mocks()

    mock_conn.fetchrow = AsyncMock(side_effect=[None, {
        "content": "A community narrative",
        "metadata": {"entity": "Anything", "domain": "general"},
        "source_pg_ids": [1, 2, 3],
    }])
    # Provide one candidate so the early-return path is not taken
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content": "fact content", "metadata": {"entities": [], "source": "claude-code"}},
    ])
    mock_session.run = AsyncMock(return_value=_AsyncIter())

    mock_reranker = MagicMock()
    mock_reranker.raise_for_status = MagicMock()
    mock_reranker.json = MagicMock(return_value={
        "results": [{"index": 0, "relevance_score": 0.0}]
    })

    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_reranker)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_cls.return_value.__aexit__  = AsyncMock(return_value=None)

            req = _make_request({"query": "anything", "limit": 5})
            resp = await c.handle_search(req)

    assert resp.status == 200
    results = json.loads(resp.text)["results"]
    assert results[0]["tier"] == "community_summary"


@pytest.mark.asyncio
async def test_search_community_summary_surfaces_traceback_pointers():
    """The Tier-3 community summary result must surface source_pg_ids and metadata
    so agents can trace the narrative back to its source facts (issue d)."""
    c, mock_conn, mock_session = _coordinator_with_mocks()

    mock_conn.fetchrow = AsyncMock(side_effect=[None, {
        "content": "Synthesised narrative about the outbox pattern",
        "metadata": {"entity": "OutboxPattern", "domain": "shared-memory"},
        "source_pg_ids": [42, 43, 44],
    }])
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content": "fact", "metadata": {"entities": [], "source": "claude-code"}},
    ])
    mock_session.run = AsyncMock(return_value=_AsyncIter())

    mock_reranker = MagicMock()
    mock_reranker.raise_for_status = MagicMock()
    mock_reranker.json = MagicMock(return_value={"results": [{"index": 0, "relevance_score": 1.0}]})

    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_reranker)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_cls.return_value.__aexit__  = AsyncMock(return_value=None)

            req = _make_request({"query": "outbox pattern", "limit": 5})
            resp = await c.handle_search(req)

    assert resp.status == 200
    cs = json.loads(resp.text)["results"][0]
    assert cs["tier"] == "community_summary"
    # Trace-back pointers are now present (previously dropped — metadata was None)
    assert cs["source_pg_ids"] == [42, 43, 44]
    assert cs["metadata"]["entity"] == "OutboxPattern"
    assert cs["metadata"]["domain"] == "shared-memory"


# ── Auth source overwrite — Phase 2C ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_save_source_overwritten_by_authenticated_agent():
    """When auth is active the coordinator must stamp source with the verified agent name,
    not the value the client supplied."""
    c, mock_conn, _ = _coordinator_with_mocks()

    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request(
            {
                "content": "some content",
                "metadata": {"project": "shared-memory-GitHub", "source": "imposter", "entities": ["Entity1"]},
            },
            authenticated_agent="claude",
        )
        resp = await c.handle_save(req)

    assert resp.status == 200

    # The outbox INSERT carries the server-verified source, not "imposter"
    outbox_call = next(
        (c for c in mock_conn.execute.call_args_list
         if "neo4j_outbox" in c.args[0]),
        None,
    )
    assert outbox_call is not None
    params = outbox_call.args[2]   # bound as a dict; asyncpg jsonb codec serialises it
    assert params["source"] == "claude"


@pytest.mark.asyncio
async def test_handle_save_agent_id_stamped_from_verified_identity():
    """The agent_id COLUMN must be the verified token identity, not the client's
    script-name default ('memory_bridge'). Regression: authenticated saves were
    all recorded under the placeholder because only source was overwritten."""
    c, mock_conn, _ = _coordinator_with_mocks()

    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request(
            {
                "content": "some content",
                "metadata": {"project": "shared-memory-GitHub", "source": "claude", "entities": ["Entity1"]},
                "agent_id": "memory_bridge",   # client default
            },
            authenticated_agent="grok",
        )
        resp = await c.handle_save(req)

    assert resp.status == 200
    insert_call = next(
        c_ for c_ in mock_conn.fetchrow.call_args_list
        if "INSERT INTO technical_docs" in c_.args[0]
    )
    # agent_id is the 5th bound param (content, metadata, embedding, hash, agent_id, ...)
    assert insert_call.args[5] == "grok"
    assert "memory_bridge" not in insert_call.args


@pytest.mark.asyncio
async def test_handle_save_agent_id_falls_back_to_body_without_auth():
    """Auth disabled (no authenticated_agent) → keep the client-supplied agent_id."""
    c, mock_conn, _ = _coordinator_with_mocks()

    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "some content",
            "metadata": {"project": "shared-memory-GitHub", "source": "claude", "entities": ["Entity1"]},
            "agent_id": "claude_code",
        })  # authenticated_agent defaults to None
        resp = await c.handle_save(req)

    assert resp.status == 200
    insert_call = next(
        c_ for c_ in mock_conn.fetchrow.call_args_list
        if "INSERT INTO technical_docs" in c_.args[0]
    )
    assert insert_call.args[5] == "claude_code"


# ── Read-role route gating — _read_role_permits ──────────────────────────────

def test_read_role_permits_allows_exact_allowlisted_routes():
    """The fixed _READ_ROLE_ROUTES entries (telemetry, graph) are always reachable
    by a read-only role, with or without a trailing slash."""
    for path in ("/memory/telemetry", "/memory/telemetry/"):
        req = MagicMock(method="GET", path=path)
        assert coordinator_mod._read_role_permits(req) is True
    req = MagicMock(method="POST", path="/memory/graph")
    assert coordinator_mod._read_role_permits(req) is True


def test_read_role_permits_allows_memory_status_with_pg_id():
    """GET /memory/status/{pg_id} — the one path-param route a read role may
    reach — is granted for a real single-segment id, trailing slash or not."""
    for path in ("/memory/status/123", "/memory/status/123/"):
        req = MagicMock(method="GET", path=path)
        assert coordinator_mod._read_role_permits(req) is True


def test_read_role_permits_rejects_crafted_extra_segment_path():
    """Security regression: a crafted path like "/memory/status/1/x" must NOT be
    granted. aiohttp's real /memory/status/{pg_id} route has a single-segment
    dynamic pattern (`[^{}/]+`, never spanning a "/") and does not match this
    path, so it falls through to the catch-all proxy passthrough — a bare
    `startswith("/memory/status/")` check let a read-only role token (e.g. the
    monitor's) reach that passthrough anyway, bypassing the role gate entirely."""
    for path in ("/memory/status/1/anything", "/memory/status/1/x/y", "/memory/status/"):
        req = MagicMock(method="GET", path=path)
        assert coordinator_mod._read_role_permits(req) is False


def test_read_role_permits_rejects_non_get_and_unrelated_paths():
    """A read role may not reach /memory/status/{pg_id} via any method but GET,
    nor any path outside the allowlist."""
    assert coordinator_mod._read_role_permits(
        MagicMock(method="DELETE", path="/memory/status/123")) is False
    assert coordinator_mod._read_role_permits(
        MagicMock(method="GET", path="/memory/save")) is False
    assert coordinator_mod._read_role_permits(
        MagicMock(method="POST", path="/v1/embeddings")) is False


# ── BoundedKeyedLocks — undocumented CPython internal dependency ─────────────

def test_asyncio_lock_still_exposes_waiters_attribute():
    """Code-review finding: BoundedKeyedLocks._evict_idle reads asyncio.Lock's
    private `_waiters` via getattr(..., None), which degrades SAFELY if the
    attribute is ever renamed (a missing attribute reads as "no waiters",
    permitting eviction) — but that degrade-path is silent, not loud, and
    would quietly re-open the exact race the per-entity lock exists to
    prevent (two callers believing they hold exclusive access to the same
    entity key). This test is the suggested self-test: it fails LOUDLY, at
    test time, the moment a CPython release ever removes or renames the
    attribute this class depends on — instead of the fragility only showing
    up as a silent behavior change in production."""
    lock = asyncio.Lock()
    assert hasattr(lock, "_waiters"), (
        "asyncio.Lock no longer exposes _waiters — "
        "shared-memory/scripts/coordinator.py:BoundedKeyedLocks._evict_idle "
        "depends on it (see the comment at that call site) and needs a new "
        "eviction-safety check for this Python version."
    )


# ── JSONB double-encoding regression (v0.4.2) ────────────────────────────────
#
# The asyncpg pool registers a jsonb codec with encoder=json.dumps, so jsonb
# params must be bound as Python objects, never pre-serialised strings. A manual
# json.dumps() here double-encodes the value into a string scalar
# (jsonb_typeof='string'), which makes metadata->>'key' return NULL. These tests
# pin the contract: handle_save / handle_retrospective bind dicts, and a client
# that sends metadata as a JSON string is coerced back to an object.

def _outbox_params(mock_conn):
    call = next(
        (c for c in mock_conn.execute.call_args_list if "neo4j_outbox" in c.args[0]),
        None,
    )
    assert call is not None, "outbox INSERT not found"
    return call.args[2]


@pytest.mark.asyncio
async def test_save_binds_metadata_as_object_not_stringified():
    """Regression: the technical_docs INSERT and the outbox INSERT must bind
    dicts, not JSON strings — otherwise the codec double-encodes them."""
    c, mock_conn, _ = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "fact for encoding check",
            "metadata": {"project": "shared-memory-GitHub", "source": "claude-code", "entities": ["SharedMemory"]},
        })
        resp = await c.handle_save(req)
    assert resp.status == 200

    # technical_docs INSERT: (sql, content, metadata, embedding, hash, ...)
    metadata_arg = mock_conn.fetchrow.await_args.args[2]
    assert isinstance(metadata_arg, dict), (
        f"metadata must bind as a dict, got {type(metadata_arg).__name__} "
        "(a str would be double-encoded by the jsonb codec)"
    )
    assert isinstance(_outbox_params(mock_conn), dict)


@pytest.mark.asyncio
async def test_save_coerces_stringified_metadata_to_object():
    """A client that sends metadata as a JSON string must still be stored as a
    queryable object, not a jsonb string scalar."""
    c, mock_conn, _ = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "fact with stringified metadata",
            "metadata": json.dumps({"source": "grok", "project": "shared-memory-GitHub", "entities": ["X"]}),
        })
        resp = await c.handle_save(req)
    assert resp.status == 200
    metadata_arg = mock_conn.fetchrow.await_args.args[2]
    assert isinstance(metadata_arg, dict)
    assert metadata_arg["source"] == "grok"
    assert metadata_arg["entities"] == ["X"]


@pytest.mark.asyncio
async def test_retrospective_binds_cypher_params_as_object():
    """Regression: the retrospective outbox payload must bind as a dict.
    v2: the handler embeds the notes, mints the retro's own record, and queues
    the outbox row under the retro's own pg_id with v=2 + target_pg_id."""
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetchrow = AsyncMock(side_effect=[
        {"id": 240, "type": "decision", "project": "shared-memory-GitHub"},  # target
        {"id": 911},                                                          # retro row
    ])
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({"pg_id": 240, "rating": "Validated", "notes": "held up well",
                                "grounded_in": [700]})
        resp = await c.handle_retrospective(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["pg_id"] == 911 and body["target_pg_id"] == 240
    params = _outbox_params(mock_conn)
    assert isinstance(params, dict)
    assert params["type"] == "retrospective"
    assert params["v"] == 2
    assert params["target_pg_id"] == 240
    assert params["retrospective"]["rating"] == "validated"   # normalised


@pytest.mark.asyncio
async def test_retrospective_rejects_non_enum_rating():
    """The rating is a closed outcome-state enum at the gateway — free-text
    grades get a 400 that lists the vocabulary."""
    c, mock_conn, _ = _coordinator_with_mocks()
    req = _make_request({"pg_id": 240, "rating": "high", "notes": "held up well"})
    resp = await c.handle_retrospective(req)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert "validated" in body["message"] and "reversed" in body["message"]


@pytest.mark.asyncio
async def test_retrospective_v2_inherits_target_project_and_stores_record():
    """The retro's technical_docs row inherits the target decision's project so
    domain-scoped reads see the retro beside its decision."""
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetchrow = AsyncMock(side_effect=[
        {"id": 240, "type": "decision", "project": "tier3-cloe"},
        {"id": 912},
    ])
    # _resolve_typed_grounding looks the grounded fact up in technical_docs
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 601, "type": None, "source_ref": "tests/test_outbox_ledger.py"},
    ])
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({"pg_id": 240, "rating": "mixed", "notes": "partly held",
                             "grounded_in": [601], "elicited": True})
        resp = await c.handle_retrospective(req)
    assert resp.status == 200
    # technical_docs INSERT metadata (2nd fetchrow call): row content + metadata
    meta = mock_conn.fetchrow.await_args_list[1].args[2]
    assert meta["type"] == "retrospective"
    assert meta["project"] == "tier3-cloe"
    assert meta["target_pg_id"] == 240
    assert meta["rating"] == "mixed"
    assert meta["grounded_in"] == [601]
    assert meta["elicited"] is True
    # Identity includes the target decision: same notes on another decision is
    # a DIFFERENT record (and can never collide with a plain fact's hash).
    import hashlib as _hl
    assert mock_conn.fetchrow.await_args_list[1].args[4] == _hl.sha256(
        b"retrospective:240:partly held").hexdigest()


# ── Graph-integrity telemetry (decision 928) ──────────────────────────────────

def _integrity_result(rows):
    """Stub the `await (await session.run(...)).data()` shape used by the
    Neo4j telemetry helpers."""
    res = AsyncMock()
    res.data = AsyncMock(return_value=rows)
    return res


@pytest.mark.asyncio
async def test_graph_integrity_reports_clean_when_no_invalid_nodes():
    """0 invalid nodes is the expected steady state, and must be reported as an
    affirmative `clean`, not as an absent field a dashboard could misread."""
    c, _, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_integrity_result([]))

    out = await c._graph_integrity()

    assert out == {"invalid_nodes": 0, "by_reason": {}, "by_label": {},
                   "clean": True}


@pytest.mark.asyncio
async def test_graph_integrity_groups_by_reason_and_label():
    """REM's verdict is surfaced grouped, so the SHAPE of the write-path defect
    is legible without a follow-up query — which label got written, and what it
    should have been. This is the signal that existed for weeks and that nothing
    read: three defects were each diagnosed here and found only by hand."""
    c, _, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_integrity_result([
        {"label": "Fact", "reason": "label_mismatch:Fact!=Decision", "c": 7},
        {"label": "Fact", "reason": "label_mismatch:Fact!=Retrospective", "c": 2},
    ]))

    out = await c._graph_integrity()

    assert out["invalid_nodes"] == 9
    assert out["clean"] is False
    # Ordered most-common first so the dominant defect leads.
    assert list(out["by_reason"]) == ["label_mismatch:Fact!=Decision",
                                      "label_mismatch:Fact!=Retrospective"]
    assert out["by_reason"]["label_mismatch:Fact!=Decision"] == 7
    assert out["by_label"] == {"Fact": 9}


@pytest.mark.asyncio
async def test_graph_integrity_tolerates_null_reason_and_label():
    """A node flagged before the reason field existed must still be counted —
    an integrity probe that drops rows it cannot label understates the defect."""
    c, _, mock_session = _coordinator_with_mocks()
    mock_session.run = AsyncMock(return_value=_integrity_result([
        {"label": None, "reason": None, "c": 3},
    ]))

    out = await c._graph_integrity()

    assert out["invalid_nodes"] == 3
    assert out["by_reason"] == {"unspecified": 3}
    assert out["by_label"] == {"unlabelled": 3}


# ── Grounding-target label resolution (bug 578's shape, for every record type) ─

@pytest.mark.asyncio
@pytest.mark.parametrize("record_type,expected_label", [
    ("decision", "Decision"),
    ("retrospective", "Retrospective"),
    ("fact", "Fact"),
    (None, "Fact"),          # plain facts carry no explicit type
    ("unheard_of", "Fact"),  # unknown type still lands on a real label
])
async def test_resolve_typed_grounding_labels_every_record_type(
    record_type, expected_label
):
    """A grounding target's label must be resolved from its ACTUAL record type.

    Reproduces the shadow-node defect from the logic alone: the resolver used to
    be a binary `Decision if type == 'decision' else Fact`, so a RETROSPECTIVE
    target fell through to Fact. The writer then MERGEd a hollow :Fact stub at
    the retrospective's pg_id while the real :Retrospective node stayed unlinked
    — bug 578 all over again, and it silently discards exactly the lineage that
    grounds a successor decision on the retrospective which drove it.
    """
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 849, "type": record_type, "source_ref": None},
    ])

    out = await c._resolve_typed_grounding(mock_conn, [849], {})

    assert len(out) == 1
    assert out[0]["pg_id"] == 849
    assert out[0]["label"] == expected_label


@pytest.mark.asyncio
async def test_resolve_typed_grounding_retrospective_keeps_operator_role():
    """The operator's asserted role survives on a retrospective-typed target —
    label resolution must not disturb the advisory-gate outcome (decision 582)."""
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 851, "type": "retrospective", "source_ref": None},
    ])

    out = await c._resolve_typed_grounding(mock_conn, [851], {"851": "considered"})

    assert out[0] == {"pg_id": 851, "rel": "CONSIDERED",
                      "asserted_by": "operator", "label": "Retrospective"}


# ── GET /memory/telemetry rollup ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_telemetry_rolls_up_postgres_and_neo4j():
    """handle_telemetry returns a combined Postgres + Neo4j operational snapshot."""
    c, mock_conn, mock_session = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=[
        {"status": "applied", "n": 10}, {"status": "rem_reviewed", "n": 3},
    ])
    mock_conn.fetchval = AsyncMock(return_value=171)
    mock_conn.fetchrow = AsyncMock(side_effect=[
        {"total": 171, "superseded": 4},                  # technical_docs rollup
        {"total": 2, "superseded": 0, "insight": 0},      # community_summaries rollup
    ])

    def _result(rows):
        r = MagicMock(); r.data = AsyncMock(return_value=rows); return r
    mock_session.run = AsyncMock(side_effect=[
        _result([{"rem": True, "con": True, "superseded": False, "n": 96},
                  {"rem": False, "con": False, "superseded": False, "n": 1},
                  # Superseded facts are permanently excluded from REM's own
                  # candidacy query — they must not inflate facts_rem_pending
                  # with a backlog REM will never touch (the bug this covers).
                  {"rem": False, "con": False, "superseded": True, "n": 12}]),
        _result([{"rem": True, "superseded": False, "n": 4},
                  {"rem": False, "superseded": False, "n": 71},
                  {"rem": False, "superseded": True, "n": 2}]),
        # rem_attempts/rem_passed_over distribution over pending records
        _result([{"a": 0, "p": 0, "n": 60}, {"a": 2, "p": 1, "n": 9},
                  {"a": 5, "p": 3, "n": 3}]),
    ])

    resp = await c.handle_telemetry(_make_request({}))
    assert resp.status == 200
    t = json.loads(resp.text)["telemetry"]
    assert t["postgres"]["technical_docs"] == 171
    assert t["postgres"]["technical_docs_superseded"] == 4
    assert t["postgres"]["outbox"] == {"applied": 10, "rem_reviewed": 3}
    assert t["postgres"]["community_summaries"]["insight"] == 0
    assert t["neo4j"]["facts_total"] == 109
    assert t["neo4j"]["facts_rem_pending"] == 1       # the 12 superseded-pending facts are excluded
    assert t["neo4j"]["facts_unconsolidated"] == 0   # only rem=True & con=False counts; here 96 are consolidated
    assert t["neo4j"]["decisions_total"] == 77
    assert t["neo4j"]["decisions_rem_pending"] == 71  # the 2 superseded-pending decisions are excluded
    # F5: records REM has given up on are visible, not just silently absent
    # from its queue while still counted as "pending".
    assert t["neo4j"]["rem_dead_lettered"] == 3
    assert t["neo4j"]["rem_failing"] == 9
    assert t["neo4j"]["rem_max_attempts"] == 5
    # STEP 3 (decision 890) — fairness gauge: total = sum(n*p), starved = rows
    # at/above REM_STARVED_THRESHOLD (default 3).
    assert t["neo4j"]["rem_passed_over_total"] == 60 * 0 + 9 * 1 + 3 * 3
    assert t["neo4j"]["rem_starved_pending"] == 3


@pytest.mark.asyncio
async def test_handle_telemetry_survives_partial_backend_failure():
    """A Postgres error must not sink the Neo4j section (and vice versa)."""
    c, mock_conn, mock_session = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(side_effect=Exception("pg down"))

    def _result(rows):
        r = MagicMock(); r.data = AsyncMock(return_value=rows); return r
    mock_session.run = AsyncMock(side_effect=[
        _result([{"rem": True, "con": False, "superseded": False, "n": 5}]),
        _result([{"rem": False, "superseded": False, "n": 2}]),
        _result([{"a": 0, "p": 0, "n": 2}]),
    ])

    resp = await c.handle_telemetry(_make_request({}))
    t = json.loads(resp.text)["telemetry"]
    assert "error" in t["postgres"]
    assert t["neo4j"]["facts_total"] == 5


# ── Phase 3a — insight elevation, reversal hook, project normalisation ───────

@pytest.mark.asyncio
async def test_search_insight_elevated_above_thematic_summary():
    """When an active kind='insight' summary exists, it surfaces FIRST with
    tier='insight_summary'; the thematic summary follows (decision 276)."""
    c, mock_conn, mock_session = _coordinator_with_mocks()

    mock_conn.fetchrow = AsyncMock(side_effect=[
        {  # nearest insight
            "content": "Cross-project principle about outbox ledgers",
            "metadata": {"kind": "insight", "entity": "OutboxPattern",
                         "projects": ["shared-memory-GitHub", "tier3-cloe"]},
            "source_pg_ids": [245, 267],
        },
        {  # nearest thematic summary
            "content": "Thematic narrative",
            "metadata": {"entity": "OutboxPattern", "domain": "general"},
            "source_pg_ids": [1, 2, 3],
        },
    ])
    mock_conn.fetch = AsyncMock(return_value=[
        {"id": 1, "content": "fact", "metadata": {"entities": [], "source": "claude-code"}},
    ])
    mock_session.run = AsyncMock(return_value=_AsyncIter())

    mock_reranker = MagicMock()
    mock_reranker.raise_for_status = MagicMock()
    mock_reranker.json = MagicMock(return_value={"results": [{"index": 0, "relevance_score": 1.0}]})

    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_reranker)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_cls.return_value.__aexit__  = AsyncMock(return_value=None)

            resp = await c.handle_search(_make_request({"query": "outbox", "limit": 5}))

    results = json.loads(resp.text)["results"]
    assert results[0]["tier"] == "insight_summary"
    assert results[0]["source_pg_ids"] == [245, 267]   # decision ids
    assert results[1]["tier"] == "community_summary"
    assert results[2]["tier"] == "fact"


@pytest.mark.asyncio
async def test_retrospective_reversed_marks_decision_superseded():
    """rating='reversed' is the one structural rating: technical_docs row gets
    superseded=true and the outbox payload carries the graph-side flag."""
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetchrow = AsyncMock(side_effect=[
        {"id": 42, "type": "decision", "project": "p1"},   # target FOR SHARE
        {"id": 915},                                        # retro's own row
    ])

    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({"pg_id": 42, "rating": "Reversed",
                             "notes": "approach withdrawn", "agent_id": "claude_code",
                             "grounded_in": [700]})
        resp = await c.handle_retrospective(req)

    assert resp.status == 200
    executes = [c_.args for c_ in mock_conn.execute.call_args_list]
    outbox_sql, _, params = executes[0]
    assert "INSERT INTO neo4j_outbox" in outbox_sql
    assert params["retrospective"]["superseded"] is True
    update_sql = executes[1][0]
    assert "SET superseded = true" in update_sql


@pytest.mark.asyncio
async def test_retrospective_normal_rating_has_no_reversal_side_effects():
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetchrow = AsyncMock(side_effect=[
        {"id": 42, "type": "decision", "project": "p1"},
        {"id": 916},
    ])

    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        resp = await c.handle_retrospective(_make_request(
            {"pg_id": 42, "rating": "validated", "notes": "held up",
             "agent_id": "claude_code", "grounded_in": [700]}
        ))

    assert resp.status == 200
    executes = [c_.args for c_ in mock_conn.execute.call_args_list]
    # outbox INSERT + pg_notify wake — but NO superseded UPDATE
    assert not any("SET superseded = true" in e[0] for e in executes)
    assert "superseded" not in executes[0][2]["retrospective"]


@pytest.mark.asyncio
async def test_apply_retrospective_with_superseded_sets_graph_flag():
    """The outbox apply mirrors the reversal onto the Decision node so the
    fresh-cluster gate can exclude it with pure graph state."""
    c, mock_conn, mock_session = _coordinator_with_mocks()

    await c._apply_retrospective_outbox_row(7, 42, {
        "type": "retrospective",
        "retrospective": {"rating": "reversed", "date": "2026-06-11",
                          "notes": "withdrawn", "superseded": True},
    })

    cypher = mock_session.run.call_args.args[0]
    assert "SET d.superseded = true" in cypher
    assert "HAD_OUTCOME" in cypher


@pytest.mark.asyncio
async def test_save_normalizes_project_aliases_at_ingress():
    """PROJECT_ALIASES rewrites metadata.project and decision.project before
    the row and outbox params are written (decision 276: canonical = folder name)."""
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetchrow = AsyncMock(return_value={"id": 7})

    with patch.dict(coordinator_mod.PROJECT_ALIASES,
                    {"shared_memory": "shared-memory-GitHub"}, clear=True):
        req = _make_request({
            "content": "decision text",
            "metadata": {
                "source": "claude_code",
                "project": "shared_memory",
                "type": "decision",
                "decision": {"decided_by": "X", "project": "shared_memory",
                             "rationale": "because"},
            },
        })
        with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
            resp = await c.handle_save(req)

    assert resp.status == 200
    saved_metadata = mock_conn.fetchrow.call_args.args[2]
    assert saved_metadata["project"] == "shared-memory-GitHub"
    assert saved_metadata["decision"]["project"] == "shared-memory-GitHub"


def test_embed_truncates_oversized_input():
    """BGE-M3 8192-ctx guard: embedding input over EMBED_MAX_CHARS is truncated."""
    import asyncio
    coord = load_coordinator()
    captured = {}
    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"data": [{"embedding": [0.1, 0.2]}]}
    class FakeClient:
        async def post(self, url, json=None, timeout=None):
            captured["len"] = len(json["input"]); captured["timeout"] = timeout
            return FakeResp()
    long = "x" * (coord.EMBED_MAX_CHARS + 5000)
    out = asyncio.run(coord.MemoryCoordinator._embed(None, long, FakeClient()))
    assert captured["len"] == coord.EMBED_MAX_CHARS and out == [0.1, 0.2]
    # The timeout is sized on the CLAMPED length, and a maximally-sized input
    # must get the full-context ceiling — the shared client default (30s) was
    # not even enough for this function's own clamp.
    assert captured["timeout"] == coord.embed_ceiling(coord.EMBED_MAX_CHARS)
    assert captured["timeout"] > 30.0


def test_embed_passes_short_input_untruncated():
    import asyncio
    coord = load_coordinator()
    captured = {}
    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"data": [{"embedding": [0.0]}]}
    class FakeClient:
        async def post(self, url, json=None, timeout=None):
            captured["len"] = len(json["input"]); captured["timeout"] = timeout
            return FakeResp()
    asyncio.run(coord.MemoryCoordinator._embed(None, "short text", FakeClient()))
    assert captured["len"] == len("short text")
    # A short save sits on the floor — the derivation must not make small
    # writes wait longer than they used to.
    assert captured["timeout"] == coord.EMBED_TIMEOUT_FLOOR_S
