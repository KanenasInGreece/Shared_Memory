"""Item 4 of the v0.9.69 post-first-write hardening plan — a re-save never
moves a record's axes.

Invariant **P1**: a record's project, domains and entities are fixed at its
FIRST write. They change only through a supersession or a ledgered operator
backfill (`fact:1255`) — never through the save path.

WHAT WAS WRONG (`fact:1734` C(a)). `ON CONFLICT (content_hash) DO UPDATE`
replaces the metadata blob WHOLESALE. Re-saving the same words under a
different project therefore relabelled the Postgres row silently, while the
graph kept every edge written the first time and gained the new ones on top —
so the two stores stopped agreeing, and the divergence read later as a defect
in whichever store was inspected second.

Two guards, and the second is the one that counts:

  * a cheap indexed pre-check before the mint and the GPU embedding, so a save
    that is going to be refused does not pay for either
  * an authoritative `SELECT … FOR UPDATE` inside the save transaction, the
    only place a concurrent save of the same content cannot slip between the
    read and the INSERT

Identity — same content AND same axes — is untouched and still idempotent: it
takes the `DO UPDATE` path, which is what repairs a missing embedding (Q1,
settled in the plan).

Mutation checks (RUN, recorded in HANDOFF.md) — each of the three refusal
tests dies when the corresponding comparison is removed from
`_axis_conflict_error`, and ALL of them die when the in-transaction check is
removed from `handle_save`.
"""
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

from coordinator import MemoryCoordinator  # noqa: E402


class _AsyncCtx:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


def _coord(stored_metadata=None):
    """A coordinator whose `technical_docs` already holds one row for the
    content hash under test — `stored_metadata` is that row's metadata."""
    c = MemoryCoordinator()
    c._entity_vocab_resolve_many = AsyncMock(side_effect=lambda names: {n: n for n in names})
    c._entity_vocab_mint = AsyncMock(side_effect=lambda n, agent: n)

    conn = MagicMock()

    async def _fetchval(sql, *args):
        if "content_hash" in sql:
            return stored_metadata
        return 1                      # every registry lookup: "registered"

    conn.fetchval = AsyncMock(side_effect=_fetchval)
    conn.fetchrow = AsyncMock(return_value={"id": 99})
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.transaction = MagicMock(return_value=_AsyncCtx(None))

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    c._pool = pool

    session = AsyncMock()
    session.run = AsyncMock()
    neo4j = MagicMock()
    neo4j.session = MagicMock(return_value=_AsyncCtx(session))
    c._neo4j = neo4j
    return c, conn


def _request(body):
    body = dict(body)
    body.setdefault("agent_id", "claude-code")
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.get = MagicMock(return_value=None)
    req.headers = {}
    return req


async def _save(c, metadata, content="the same words, saved twice"):
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)) as embed:
        resp = await c.handle_save(_request({"content": content, "metadata": metadata}))
    return resp, embed


# ── The three axes ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resave_with_different_project_is_refused():
    c, _ = _coord(stored_metadata={"project": "alpha", "entities": []})
    resp, embed = await _save(c, {"source": "claude-code", "project": "beta"})
    assert resp.status == 409
    body = json.loads(resp.text)
    assert body["error"] == "axis_conflict"
    assert body["axis"] == "project"
    # Names BOTH sides — a refusal a caller cannot act on is a dead end.
    assert "alpha" in body["message"] and "beta" in body["message"]
    embed.assert_not_called()


@pytest.mark.asyncio
async def test_resave_with_different_domains_is_refused():
    c, _ = _coord(stored_metadata={"project": "alpha", "domains": ["architecture"],
                                   "entities": []})
    resp, embed = await _save(
        c, {"source": "claude-code", "project": "alpha", "domains": ["capture"]})
    assert resp.status == 409
    assert json.loads(resp.text)["axis"] == "domains"
    embed.assert_not_called()


@pytest.mark.asyncio
async def test_resave_with_different_entities_is_refused():
    c, _ = _coord(stored_metadata={"project": "alpha", "entities": ["Kubernetes"]})
    resp, embed = await _save(
        c, {"source": "claude-code", "project": "alpha", "entities": ["Prometheus"]})
    assert resp.status == 409
    assert json.loads(resp.text)["axis"] == "entities"
    embed.assert_not_called()


# ── What must NOT be refused ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_identical_resave_is_still_idempotent():
    """Q1, settled: identical content AND identical axes keeps today's
    behaviour — the `DO UPDATE` refresh, which is the embedding repair path."""
    c, _ = _coord(stored_metadata={"project": "alpha", "domains": ["architecture"],
                                   "entities": ["Kubernetes"]})
    resp, _ = await _save(c, {
        "source": "claude-code", "project": "alpha",
        "domains": ["architecture"], "entities": ["Kubernetes"],
    })
    assert resp.status == 200


@pytest.mark.asyncio
async def test_a_first_write_is_never_a_conflict():
    c, _ = _coord(stored_metadata=None)
    resp, _ = await _save(c, {"source": "claude-code", "project": "alpha"})
    assert resp.status == 200


@pytest.mark.asyncio
async def test_domain_order_is_not_a_conflict():
    """The comparison is on SETS: a domain list is a set of sections, never a
    ranking, so a reordered list is the same axis value."""
    c, _ = _coord(stored_metadata={"project": "alpha",
                                   "domains": ["architecture", "capture"],
                                   "entities": []})
    resp, _ = await _save(c, {"source": "claude-code", "project": "alpha",
                              "domains": ["capture", "architecture"]})
    assert resp.status == 200


@pytest.mark.asyncio
async def test_a_legacy_singular_domain_key_does_not_false_conflict():
    """152 live facts carry a singular `domain` string. Both sides resolve
    through `resolve_domains`, never through the literal key, so the legacy
    spelling and the modern list are one value — comparing keys would make
    every one of those records permanently unsaveable."""
    c, _ = _coord(stored_metadata={"project": "alpha", "domain": "architecture",
                                   "entities": []})
    resp, _ = await _save(c, {"source": "claude-code", "project": "alpha",
                              "domains": ["architecture"]})
    assert resp.status == 200


@pytest.mark.asyncio
async def test_a_legacy_decision_with_pg_entities_is_still_resavable():
    """A JUDGEMENT compares project + domains only. 194 live decisions carry
    entities in Postgres; item 3 refuses new ones, so comparing entities here
    would make every one of those permanently unsaveable over a field the
    client no longer even sends."""
    c, _ = _coord(stored_metadata={"decision": {"project": "alpha"},
                                   "entities": ["Kubernetes"]})
    resp, _ = await _save(c, {
        "source": "claude-code",
        "type": "decision",
        "entities": [],
        "decision": {"decided_by": "Xenofon", "project": "alpha",
                     "rationale": "because"},
    })
    assert resp.status == 200


@pytest.mark.asyncio
async def test_a_stored_blob_that_is_not_an_object_is_not_a_conflict():
    """Defensive: a row whose metadata is not a JSON object tells us nothing
    about its axes, and inventing a conflict from it would refuse a save over
    a shape defect somewhere else entirely."""
    c, _ = _coord(stored_metadata="not-an-object")
    resp, _ = await _save(c, {"source": "claude-code", "project": "alpha"})
    assert resp.status == 200


# ── The pure comparison, directly ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_comparison_reads_a_judgement_project_from_the_decision_blob():
    """Same precedence as PROJECT_SQL: the blob wins over the top level, on
    both sides, or a decision's stored and incoming projects are read out of
    different halves of one record."""
    c, _ = _coord(stored_metadata=None)
    c._resolve_project_alias = AsyncMock(return_value=None)
    assert await c._axis_conflict_error(
        {"decision": {"project": "alpha"}, "project": "beta"},
        "alpha", [], [], True) is None
    conflict = await c._axis_conflict_error(
        {"decision": {"project": "alpha"}, "project": "beta"},
        "beta", [], [], True)
    assert conflict is not None and conflict["axis"] == "project"


# ── The race: only the in-transaction guard can see it ────────────────────────

@pytest.mark.asyncio
async def test_a_row_that_appears_between_the_precheck_and_the_insert_is_refused():
    """The pre-check is advisory; THIS is the guard. A concurrent save of the
    same content commits after the pre-check read nothing — the `FOR UPDATE`
    re-read inside the transaction is the only thing standing between that row
    and a wholesale metadata overwrite.

    MUTATION CHECK: delete the in-transaction `SELECT … FOR UPDATE` block from
    `handle_save` and this test fails (the save returns 200 and overwrites);
    every other refusal test in this file keeps passing on the pre-check
    alone, which is exactly why this case has to exist separately."""
    c, conn = _coord(stored_metadata=None)
    answers = iter([None, {"project": "alpha", "entities": []}])

    async def _fetchval(sql, *args):
        if "content_hash" in sql:
            return next(answers)
        return 1

    conn.fetchval = AsyncMock(side_effect=_fetchval)
    resp, embed = await _save(c, {"source": "claude-code", "project": "beta"})
    assert resp.status == 409
    assert json.loads(resp.text)["axis"] == "project"
    # It got as far as the embedding — the pre-check genuinely saw nothing.
    embed.assert_awaited_once()
    # …and nothing was written.
    conn.fetchrow.assert_not_awaited()


# ── R-3 — a record whose entities are ALL shape-noise stays re-savable ────────

@pytest.mark.asyncio
async def test_a_noise_only_entity_list_does_not_false_conflict_on_resave():
    """Review finding (Opus, Required). `sanitize_entity_names(["254"])` is
    empty — a leaked pg_id is I3's gate-exempt SHAPE noise, stored verbatim in
    Tier 1 and dropped at the graph. The gate's no-candidates early return
    handed back a plan whose `canonical` was hard-coded `[]`, so the re-save
    check compared a stored `["254"]` against an incoming `[]` and refused the
    record forever, over an axis that never moved.

    MUTATION CHECK: restore `return None, empty_plan` at the no-candidates
    branch of `_entity_ingress_validate` and this test fails with a 409."""
    c, _ = _coord(stored_metadata={"project": "alpha", "entities": ["254"]})
    resp, _ = await _save(c, {"source": "claude-code", "project": "alpha",
                              "entities": ["254"]})
    assert resp.status == 200


@pytest.mark.asyncio
async def test_noise_mixed_with_a_real_entity_is_also_stable():
    """The mixed case takes the ORDINARY path (there IS a candidate), so it
    exercises the main `canonical` build rather than the early return — both
    must agree with what `_rewrite_entities` actually stores."""
    c, _ = _coord(stored_metadata={"project": "alpha",
                                   "entities": ["254", "Kubernetes"]})
    resp, _ = await _save(c, {"source": "claude-code", "project": "alpha",
                              "entities": ["254", "Kubernetes"]})
    assert resp.status == 200


@pytest.mark.asyncio
async def test_a_noise_only_record_still_conflicts_on_a_real_axis_change():
    """The fix must not have turned the check off for these records: the
    PROJECT still cannot move."""
    c, _ = _coord(stored_metadata={"project": "alpha", "entities": ["254"]})
    resp, _ = await _save(c, {"source": "claude-code", "project": "beta",
                              "entities": ["254"]})
    assert resp.status == 409
    assert json.loads(resp.text)["axis"] == "project"


@pytest.mark.asyncio
async def test_the_plan_s_canonical_matches_what_is_actually_stored_for_noise():
    """The property underneath both: the plan's PREVIEW of the entity list and
    the list the gate really writes are the same value. They are computed by
    one function precisely so they cannot drift."""
    c, _ = _coord(stored_metadata=None)
    metadata = {"source": "claude-code", "project": "alpha",
                "entities": ["254", "0", "  "]}
    _refusal, plan = await c._entity_ingress_validate(metadata)
    assert await c._entity_commit_mints(metadata, "claude-code", plan) is None
    assert plan["canonical"] == metadata["entities"] == ["254", "0", "  "]


# ── R-4 — a RENAME must not freeze every record that predates it ─────────────
#
# Review finding (Opus, Required). The comparison was on the literal spelling.
# A stored blob keeps whatever spelling was canonical when the record was FIRST
# written; the incoming value has just been rewritten to whatever is canonical
# NOW. So the day a project is renamed — or merely respelled — every identical
# re-save of every record that predates it becomes a 409, retrospectively.

@pytest.mark.asyncio
@pytest.mark.parametrize("stored,incoming", [
    ("Shared_Memory", "shared-memory"),
    ("shared memory", "shared-memory"),
    ("SHARED-MEMORY", "shared-memory"),
])
async def test_a_respelling_of_the_same_project_is_not_a_conflict(stored, incoming):
    """Separator/case variants are ONE project in the registry (`axis_key`), so
    they must be one axis value here.

    MUTATION CHECK: restore `existing_project != project` and every case here
    fails with a 409."""
    c, _ = _coord(stored_metadata={"project": stored, "entities": []})
    resp, _ = await _save(c, {"source": "claude-code", "project": incoming})
    assert resp.status == 200


@pytest.mark.asyncio
async def test_a_renamed_project_resolves_through_its_alias_before_refusing():
    """The case the ruling names: stored `Old-Name`, retired and aliased to
    `new-name`; the incoming save carries the canonical. The KEYS differ, so
    the stored spelling is resolved once before anything is refused.

    MUTATION CHECK: delete the `_resolve_project_alias` hop (refuse as soon as
    the keys differ) and this test fails with a 409 — which is the state every
    record predating a real rename would be left in."""
    c, _ = _coord(stored_metadata={"project": "Old-Name", "entities": []})
    c._resolve_project_alias = AsyncMock(return_value="new-name")
    resp, _ = await _save(c, {"source": "claude-code", "project": "new-name"})
    assert resp.status == 200


@pytest.mark.asyncio
async def test_a_genuinely_different_project_is_still_refused():
    """The fix must not have turned the project check into a no-op: a stored
    spelling that resolves to NOTHING, or to something else, still conflicts."""
    c, _ = _coord(stored_metadata={"project": "alpha", "entities": []})
    c._resolve_project_alias = AsyncMock(return_value=None)
    resp, _ = await _save(c, {"source": "claude-code", "project": "beta"})
    assert resp.status == 409
    assert json.loads(resp.text)["axis"] == "project"


@pytest.mark.asyncio
async def test_an_alias_pointing_somewhere_else_is_still_a_conflict():
    """A retired spelling that resolves to a DIFFERENT project than the one
    this save names is the strongest form of the conflict, not a pass."""
    c, _ = _coord(stored_metadata={"project": "Old-Name", "entities": []})
    c._resolve_project_alias = AsyncMock(return_value="some-other-project")
    resp, _ = await _save(c, {"source": "claude-code", "project": "new-name"})
    assert resp.status == 409


@pytest.mark.asyncio
async def test_the_alias_lookup_is_not_paid_for_when_the_keys_agree():
    """The rare path stays rare: an ordinary identical re-save must not add a
    query to the save path."""
    c, _ = _coord(stored_metadata={"project": "alpha", "entities": []})
    c._resolve_project_alias = AsyncMock(return_value=None)
    resp, _ = await _save(c, {"source": "claude-code", "project": "Alpha"})
    assert resp.status == 200
    c._resolve_project_alias.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,incoming", [
    ("Architecture", "architecture"),
    ("data_capture", "data-capture"),
])
async def test_a_respelling_of_the_same_domain_is_not_a_conflict(stored, incoming):
    """Same rule on the section axis.

    MUTATION CHECK: restore `set(existing_domains) != set(domains or [])` and
    both cases fail."""
    c, _ = _coord(stored_metadata={"project": "alpha", "domains": [stored],
                                   "entities": []})
    resp, _ = await _save(c, {"source": "claude-code", "project": "alpha",
                              "domains": [incoming]})
    assert resp.status == 200


@pytest.mark.asyncio
async def test_a_genuinely_different_domain_is_still_refused():
    c, _ = _coord(stored_metadata={"project": "alpha", "domains": ["architecture"],
                                   "entities": []})
    resp, _ = await _save(c, {"source": "claude-code", "project": "alpha",
                              "domains": ["capture"]})
    assert resp.status == 409
    assert json.loads(resp.text)["axis"] == "domains"
