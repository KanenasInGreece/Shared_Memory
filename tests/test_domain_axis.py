"""PR 5 — the domain registry, who controls the axis, and the graph chain.

A domain is a SECTION OF ONE PROJECT (migration 028). The invariants under test:

  D1  a section is project-local — the same name under two projects is two
      sections, and every lookup is qualified by the project
  D2  domain is MULTI-valued; project is single
  D3  WHO CONTROLS WHICH AXIS:
        fact           project OWN · domain OWN · mints its own entities
        decision       project OWN · domain OWN · carries no entities
        retrospective  asserts neither — its belonging is DERIVED ON READ from
                       the decision it judges
  D4  ⛔ RE-RULED by `decision:1736` (was: "a decision that names no section
      INHERITS its grounding facts' sections"). NOTHING WRITES AN INHERITED
      SECTION any more — not a decision's first write, not a retrospective's,
      and not a retrospective re-running the rule onto the decision it judges.
      A record's DOMAIN_OF edges are exactly what its operator asserted on it;
      "what else does this judgement belong to" is a question the READ side
      answers by traversal (`derived_belonging_cypher`), because a value a
      reader can reach by walking is a value nothing should write twice
      (`decision:1032`). The old rule was not wrong about the ANSWER — it was
      wrong about materialising it, after the record's first write, from an
      inference the operator never made.
  D5  an unregistered section is LOUD (400 + proposals), never a new spelling
  D6  no name-keyed :Domain node — no registry identity means NO edge
  D7  :Domain is never a topic: not an :Entity, not in REM's label table, and
      the bare schema word is refused as an entity name

The registry lookups are stubbed. Migration 028's constraints, the trigram
proposals and the emitted Cypher are verified against the LIVE stores
separately — this suite stubs all SQL and all Cypher and proves nothing about
either.
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

from domain_axis import (  # noqa: E402
    DOMAIN_ALIAS_RESOLVE_SQL, DOMAIN_EXISTS_SQL, DOMAIN_PROPOSALS_SQL,
    domain_merge_cypher, names_a_domain, resolve_domains,
)
from ontology import (  # noqa: E402
    ONT, SPINE_LABELS, SPINE_RELATIONSHIPS, DOMAIN_LABELS, sanitize_entity_name,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _coord(registered=(), aliases=None, near=()):
    """A coordinator whose domain registry answers from a fixed set.

    `registered` is a set of (project_id, name); `aliases` maps
    (project_id, alias) → canonical name; `near` is what the confusable query
    returns.
    """
    from coordinator import MemoryCoordinator
    c = MemoryCoordinator()
    c._project_identity = AsyncMock(return_value=6)
    c._project_registered = AsyncMock(return_value=True)
    c._domain_registered = AsyncMock(
        side_effect=lambda pid, n: (pid, n) in set(registered))
    c._resolve_domain_alias = AsyncMock(
        side_effect=lambda pid, n: (aliases or {}).get((pid, n)))
    c._register_domain = AsyncMock()
    c._domain_proposals = AsyncMock(return_value=["operations"])

    # ⚠ DISPATCHES ON THE STATEMENT, not on call order — the section names, the
    # confusable neighbours and the active domain aliases all come off this one
    # connection now, and a single canned list handed the alias reader rows
    # shaped like section names.
    alias_rows = [{"alias": alias, "canonical": canonical}
                  for (_pid, alias), canonical in (aliases or {}).items()]
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=lambda sql, *a: (
        alias_rows if "domain_aliases" in sql
        else [{"name": n} for n in near]
    ))
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="DELETE 0")
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    c._acquire = MagicMock(return_value=acq)
    return c


def _fact(domain=None, project="p", **extra):
    md = {"project": project, **extra}
    if domain is not None:
        md["domain"] = domain
    return md


# ── D2 — multi-valued, and the two places an axis value lives ────────────────

def test_a_bare_string_and_a_list_both_resolve_to_a_list():
    assert resolve_domains({"domain": "operations"}) == ["operations"]
    assert resolve_domains({"domains": ["operations", "llm"]}) == ["operations", "llm"]


def test_blanks_and_duplicates_are_dropped_and_order_is_kept():
    assert resolve_domains({"domains": ["llm", "  ", "llm", "ops"]}) == ["llm", "ops"]


def test_the_decision_blob_wins_over_the_top_level():
    """Same precedence as PROJECT_SQL. A decision carries its axis values inside
    the blob, and project and domain must resolve out of the SAME place — or a
    decision's section can come from one half of the record and its project from
    the other."""
    md = {"domain": "top", "decision": {"project": "p", "domain": "blob"}}
    assert resolve_domains(md) == ["blob"]


def test_a_record_naming_no_domain_resolves_to_nothing_not_an_error():
    assert resolve_domains({"project": "p"}) == []
    assert resolve_domains("not a dict") == []


# ── D3 — presence of the KEY is the signal, not its content ──────────────────

def test_an_empty_domain_value_still_counts_as_naming_one():
    """A retrospective sending "domain": "" has reached for the field. Answering
    "no domain here" would let the one shape that needs telling apart — an agent
    that believes judgements carry domains — pass unremarked."""
    assert names_a_domain({"domain": ""}) is True
    assert names_a_domain({"domains": []}) is True
    assert names_a_domain({"project": "p"}) is False


def test_a_domain_hidden_in_the_decision_blob_is_still_named():
    assert names_a_domain({"decision": {"domain": "ops"}}) is True


@pytest.mark.asyncio
async def test_a_retrospective_naming_a_domain_is_refused():
    """D3. It controls neither axis — project and domain both come from the
    decision it judges."""
    c = _coord()
    err = await c._domain_ingress_error(
        {"type": "retrospective", "project": "p", "domain": "operations"}, "claude")
    assert err is not None
    assert err["error"] == "domain_not_allowed_on_judgement"


@pytest.mark.asyncio
async def test_a_retrospective_naming_no_domain_passes():
    c = _coord()
    assert await c._domain_ingress_error(
        {"type": "retrospective", "project": "p"}, "claude") is None


@pytest.mark.asyncio
async def test_a_decision_may_name_its_own_registered_domain():
    """D3 — a decision is an axis-asserting record, not an inheriting one. This
    is the case that separates it from a retrospective."""
    c = _coord(registered={(6, "architecture")})
    err = await c._domain_ingress_error(
        {"type": "decision", "decision": {"project": "p", "domain": "architecture"}},
        "claude")
    assert err is None


# ── D5 — an unregistered section is loud ─────────────────────────────────────

@pytest.mark.asyncio
async def test_an_unregistered_domain_is_refused_with_proposals():
    c = _coord(registered={(6, "operations")})
    err = await c._domain_ingress_error(_fact("opperations"), "claude")
    assert err["error"] == "domain_unknown"
    assert err["proposals"] == ["operations"]


@pytest.mark.asyncio
async def test_a_registered_domain_passes():
    c = _coord(registered={(6, "operations")})
    assert await c._domain_ingress_error(_fact("operations"), "claude") is None


@pytest.mark.asyncio
async def test_the_second_submission_registers_it():
    """⚠ RE-RULED at v0.9.72 (P4′): the section is ACCEPTED here and the row is
    written by `_commit_axis_registrations`, after every gate — so a save
    refused later leaves `project_domains` untouched."""
    c = _coord(registered=set())
    report = {}
    err = await c._domain_ingress_error(
        dict(_fact("telemetry"), new_domain=True), "claude", report)
    assert err is None
    c._register_domain.assert_not_awaited()
    assert [d["name"] for d in report["pending_registrations"]["domains"]] \
        == ["telemetry"]
    await c._commit_axis_registrations(report, "claude")
    c._register_domain.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_new_domain_that_is_a_spelling_of_an_existing_one_is_refused():
    """The naming guard, scoped to one project's sections: separators and case
    are not a new section, and no confirmation can make them one."""
    c = _coord(registered=set(), near=["ui-ux"])
    err = await c._domain_ingress_error(
        dict(_fact("UI_UX"), new_domain=True), "claude")
    assert err["error"] == "domain_spelling_variant"
    c._register_domain.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_confusable_new_domain_is_held_until_confirmed():
    c = _coord(registered=set(), near=["operations"])
    err = await c._domain_ingress_error(
        dict(_fact("operatons"), new_domain=True), "claude")
    assert err["error"] == "domain_confusable"

    # A report, because the ACCEPTING branch defers its registration into one
    # and refuses to run without it (v0.9.72, R4 — a missing report is a
    # coding error, never a quietly dropped registration).
    report = {}
    ok = await c._domain_ingress_error(
        dict(_fact("operatons"), new_domain=True,
             confirm_distinct_from=["operations"]), "claude", report)
    assert ok is None
    assert [d["name"] for d in report["pending_registrations"]["domains"]] \
        == ["operatons"]


@pytest.mark.asyncio
async def test_a_retired_spelling_resolves_and_the_record_is_stored_canonical():
    c = _coord(registered={(6, "operations")}, aliases={(6, "ops"): "operations"})
    md = _fact("ops")
    assert await c._domain_ingress_error(md, "claude") is None
    assert md["domain"] == "operations"


@pytest.mark.asyncio
async def test_an_alias_inside_a_decision_blob_is_rewritten_there_too():
    """The rewriter must reach every place the RESOLVER reads, or the retired
    spelling survives in the half nobody rewrote."""
    c = _coord(registered={(6, "operations")}, aliases={(6, "ops"): "operations"})
    md = {"type": "decision", "decision": {"project": "p", "domain": "ops"}}
    assert await c._domain_ingress_error(md, "claude") is None
    assert md["decision"]["domain"] == "operations"


@pytest.mark.asyncio
async def test_a_domain_on_a_parked_record_is_refused():
    """A section of no project is not a section."""
    from project_axis import SENTINEL
    c = _coord()
    err = await c._domain_ingress_error(_fact("operations", project=SENTINEL), "claude")
    assert err["error"] == "domain_without_project"


# ── D1 — every registry statement is qualified by the project ────────────────

def test_no_registry_lookup_resolves_a_domain_by_name_alone():
    """D1. The one way this axis reproduces the project axis' original defect is
    by letting a name answer on its own, so the absence of an unqualified lookup
    is itself the invariant."""
    for sql in (DOMAIN_EXISTS_SQL, DOMAIN_PROPOSALS_SQL, DOMAIN_ALIAS_RESOLVE_SQL):
        assert "project_id" in sql


def test_proposals_match_descriptions_as_well_as_names():
    """An operator typing one word should reach a section named another whose
    DESCRIPTION carries the meaning — no name similarity ever connects those."""
    assert "description" in DOMAIN_PROPOSALS_SQL


# ── D6 — no name-keyed node ──────────────────────────────────────────────────

def test_the_domain_merge_is_keyed_on_the_identity_and_never_on_a_name():
    """D6. A name-keyed :Domain would re-ship, on a brand-new axis, the identity
    defect migration 027 removed — and silently, because it looks correct until
    two projects use the same section name."""
    cypher = domain_merge_cypher()
    assert "domain_id" in cypher
    assert "name" not in cypher


@pytest.mark.asyncio
async def test_an_unresolvable_domain_writes_no_edge_and_keeps_the_value():
    """D6. The project axis falls back to a name-keyed node so the write is never
    lost; this axis deliberately does not, because it gates nothing yet and the
    value survives in Postgres either way."""
    c = _coord()
    c._domain_identity = AsyncMock(return_value=None)
    assert await c._domain_identities(1, 6, ["operations"]) == []


@pytest.mark.asyncio
async def test_a_resolvable_domain_yields_its_identity_and_label():
    c = _coord()
    c._domain_identity = AsyncMock(return_value=41)
    assert await c._domain_identities(1, 6, ["operations"]) == [
        {"id": 41, "name": "operations"}]


@pytest.mark.asyncio
async def test_a_project_without_an_identity_writes_no_domain_edge():
    c = _coord()
    assert await c._domain_identities(1, None, ["operations"]) == []


# ── D7 — the axis is not a topic ─────────────────────────────────────────────

def test_domain_is_spine_and_never_configurable_vocabulary():
    """An amendment to decision 550: the fold gate moves onto this axis, so a
    renameable :Domain would falsify ontology.yaml's own promise."""
    assert ONT.domain in SPINE_LABELS
    assert ONT.domain_of in SPINE_RELATIONSHIPS
    assert ONT.domain not in DOMAIN_LABELS


def test_the_bare_schema_word_is_refused_as_an_entity_name():
    """D7 — `Domain:` was already refused as an axis DECLARATION; this is the
    bare word, the same leak `project` has been guarded against."""
    assert sanitize_entity_name("domain") is None
    assert sanitize_entity_name("Domain") is None
    assert sanitize_entity_name("DOMAIN_OF") is None


# ── D4 — the repair tool asserts, and never derives ──────────────────────────

# ⛔ RE-RULED (`decision:1736`). What stood here was
# `test_inherit_mode_clears_only_inherited_edges_never_an_assertion`: it
# asserted that a `domain_of` outbox row carrying `inherit: true` deletes the
# edges stamped `inherited`, leaves bare ones alone, and then RE-DERIVES the
# judgement's sections from what it grounds in. Every clause of that is a
# statement about a write path that no longer exists, so the test could only be
# kept by keeping the defect. Replaced, not deleted: the guarantee that matters
# now is that a row asking for the retired mode writes NOTHING — and that is a
# stronger property than the one it replaces, because live rows enqueued before
# this shipped can still arrive.


@pytest.mark.asyncio
async def test_a_retired_inherit_row_writes_nothing_and_is_dropped():
    """D4. A pending `inherit: true` row carries NO `domains` key, so falling
    through to the explicit branch would run its unconditional `DELETE stale`
    and write nothing back — stripping the record's real, asserted sections.
    Recognising the retired mode and dropping the row is the difference between
    a no-op and silent data loss."""
    c = _coord()
    queries = []

    async def run(q, **kw):
        queries.append((q, kw))
        res = MagicMock()
        res.single = AsyncMock(return_value={"n": 1})
        return res

    session = MagicMock()
    session.run = run
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    c._neo4j = MagicMock()
    c._neo4j.session = MagicMock(return_value=ctx)

    await c._apply_domain_of_outbox_row(
        1, 42, {"type": "domain_of", "inherit": True, "anchor": ONT.retrospective})

    assert queries == [], "the retired mode must touch the graph not at all"
    deleted = c._acquire.return_value.__aenter__.return_value.execute
    deleted.assert_awaited()
    assert "DELETE FROM neo4j_outbox" in deleted.call_args.args[0]


@pytest.mark.asyncio
async def test_an_explicit_row_still_replaces_the_asserted_set():
    """The half that STAYS. `explicit` is now the only mode: it clears every
    DOMAIN_OF edge and writes what Postgres says, because the record's own
    assertion is the whole answer."""
    c = _coord()
    c._domain_identity = AsyncMock(return_value=41)
    queries = []

    async def run(q, **kw):
        queries.append((q, kw))
        res = MagicMock()
        res.single = AsyncMock(return_value={"n": 1})
        return res

    session = MagicMock()
    session.run = run
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    c._neo4j = MagicMock()
    c._neo4j.session = MagicMock(return_value=ctx)

    await c._apply_domain_of_outbox_row(
        1, 42, {"type": "domain_of", "project": "p", "domains": ["architecture"]})

    deletes = [q for q, _ in queries if "DELETE stale" in q]
    assert deletes, "explicit mode replaces the set it manages"
    for q in deletes:
        assert "asserted_by" not in q, (
            "the stamp is gone — nothing may filter a delete on it any more")
    assert any(f"-[:{ONT.domain_of}]->(d)" in q for q, _ in queries)


def test_the_backfill_never_gives_a_retrospective_an_asserted_row():
    """D3 + D4. A retrospective does not control this axis, however its stored
    metadata reads — an older corpus predates the rule and a bulk operation can
    set the field without meaning to. Reading it here would let the repair tool
    write, through the back door, the edge ingress refuses at the front."""
    import backfill_domain_of as b
    rows = [
        (1, "fact", {"project": "p", "domain": "ops"}),
        (2, "retrospective", {"project": "p", "domain": "ops"}),
        (3, "decision", {"decision": {"project": "p", "domain": "ops"}}),
    ]
    explicit, _unreg, _parked = b.plan(rows, {("p", "ops")}, set())
    assert sorted(pg for pg, _p, _d in explicit) == [1, 3]
    # `plan` lost its `retro_rows` argument with the inherit population it fed
    # (`decision:1736`); a retrospective now gets no row of any kind.
    assert not hasattr(b, "RETRO_SQL")


def test_rem_can_never_mint_a_domain_node():
    """D7, now settled structurally rather than by a label table: REM writes no
    edges and no labels at all (`decision:1664`), so there is no MERGE anywhere
    in it for a Domain — or anything else — to be created by."""
    import inspect
    import rem_loop
    assert "MERGE" not in inspect.getsource(rem_loop), (
        "rem_loop builds a MERGE again — REM must write no node and no edge")
    assert not hasattr(rem_loop, "_KNOWN_LABELS")


# ── Regressions found by checking LIVE data after the release ────────────────

def test_save_decision_domain_flag_reaches_the_record():
    """A field the CLI accepts and the record never carries is the capture defect
    that hides longest. `--domain` parsed cleanly for one release while nothing
    threaded it into the metadata, so a decision silently fell back to inheriting
    its evidence's sections — and read as correct, because the inherited answer
    happened to match what was asked for."""
    import memory_bridge as mb
    _content, metadata = mb.build_decision_metadata(
        title="t", decided_by="X", project="p", rationale="r",
        domains=["architecture", "schema"], new_domain=True)
    assert metadata["decision"]["domains"] == ["architecture", "schema"]
    assert metadata["new_domain"] is True
    # Same place the gateway resolves a judgement's project from — or a
    # decision's two axes come from different halves of one record.
    assert resolve_domains(metadata) == ["architecture", "schema"]


def test_no_domain_flag_leaves_the_decision_with_no_section_at_all():
    """RENAMED from `..._free_to_inherit` (`decision:1736`): there is no
    inheritance left for it to be free for. A decision that names no section
    HAS none — the record is honest, and where it belongs is answered on read."""
    import memory_bridge as mb
    _content, metadata = mb.build_decision_metadata(
        title="t", decided_by="X", project="p", rationale="r")
    assert resolve_domains(metadata) == []
    assert "new_domain" not in metadata


@pytest.mark.asyncio
async def test_a_spelling_variant_below_the_similarity_floor_is_still_refused():
    """The guard read the TRIGRAM NEIGHBOURS, which made an EXACT rule
    conditional on a FUZZY one. Measured live: `testing` vs `Test_Ing` scores
    0.545 against a floor of 0.6, so the variant never reached the spelling check
    and registered as a brand-new section — the exact event the guard exists to
    prevent. `near` here is EMPTY on purpose: that is what a below-floor
    confusable query returns."""
    c = _coord(registered={(6, "testing")}, near=[])
    # Dispatched on the statement, never on call order — the refusal reads the
    # active domain aliases too now, and an ordered side_effect would have made
    # ADDING a query look like a broken guard.
    c._acquire.return_value.__aenter__.return_value.fetch = AsyncMock(
        side_effect=lambda sql, *a: (
            [] if "similarity" in sql or "domain_aliases" in sql
            else [{"name": "testing"}]
        ))
    err = await c._domain_ingress_error(
        dict(_fact("Test_Ing"), new_domain=True), "claude")
    assert err["error"] == "domain_spelling_variant"
    c._register_domain.assert_not_awaited()


async def _decision_cypher(domains, monkeypatch_id=41):
    """Capture the Cypher a decision projection emits, with domains resolvable."""
    c = _coord()
    c._domain_identity = AsyncMock(return_value=monkeypatch_id)
    captured = []

    async def run(q, **kw):
        captured.append((q, kw))
        res = MagicMock()
        res.single = AsyncMock(return_value={"n": 0})
        return res

    session = MagicMock()
    session.run = run
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    c._neo4j = MagicMock()
    c._neo4j.session = MagicMock(return_value=ctx)
    await c._apply_decision_outbox_row(1, 77, {
        "decision": {"title": "t", "rationale": "r", "date": "d",
                     "decided_by": "X", "project": "p", "assisted_by": []},
        "domains": domains, "grounded": [], "grounded_in": [],
    })
    return captured


@pytest.mark.asyncio
async def test_a_decision_with_an_explicit_domain_writes_its_OWN_edge():
    """D4 — and this is the test that kills 'looks right by inheritance'. The
    section must arrive on the decision's own projection statement, unstamped,
    rather than via the inheritance pass, whose edges are stamped `inherited`.
    A decision whose evidence happens to sit in the same section produces the
    same NAME either way; only the provenance tells the two apart."""
    captured = await _decision_cypher(["architecture"])
    projection = captured[0][0]
    assert f"MERGE (dm:{ONT.domain}" in projection
    assert f"-[:{ONT.domain_of}]->(dm)" in projection
    # Unstamped: nothing on the projection marks these as inherited.
    assert "asserted_by" not in projection.split("FOREACH (row IN $domains")[1]
    assert captured[0][1]["domains"] == [{"id": 41, "name": "architecture"}]


# ⛔ RE-RULED (`decision:1736`). Two tests stood here:
#
#   `test_a_decision_with_no_domain_writes_no_edge_and_leaves_inheritance_to_run`
#       asserted the projection carries no section AND that "inheritance must
#       still run" — the second half is now the defect, so the test is replaced
#       by its inverse below rather than weakened.
#   `test_inheritance_declines_when_the_record_asserted_its_own_sections`
#       pinned the `NOT EXISTS { ... asserted_by IS NULL }` guard that let the
#       inheritance query be re-run from anywhere. With no inheritance query
#       there is nothing to decline, and the property it protected — an
#       asserted section survives — is now protected by construction: nothing
#       else writes this axis. `test_a_decision_with_an_explicit_domain_writes
#       _its_OWN_edge` above is the anchor for that half.


@pytest.mark.asyncio
async def test_decision_apply_writes_no_inherited_domain_edge():
    """D4 (re-ruled). A decision that names no section gets NO DOMAIN_OF edge —
    not from its own projection, and not from a second statement deriving one
    from the facts it grounds in. The old behaviour is visible in what this
    asserts is absent: a `MERGE (a)-[m:DOMAIN_OF]->` carrying an `asserted_by`
    stamp, issued after the grounding."""
    captured = await _decision_cypher([])
    projection = captured[0][0]
    assert "FOREACH (row IN $domains" not in projection
    for q, _kw in captured:
        assert "MERGE (a)-[m:" not in q, (
            "the inheritance MERGE is back — a decision's sections are its own")
        assert "inherited" not in q, "nothing stamps an edge as inherited any more"
    assert not any(ONT.domain_of in q for q, _ in captured), (
        "a decision that asserted no section must write no DOMAIN_OF at all")


async def _retro_cypher(*, target_pg_id=42, grounded=None):
    """Capture the Cypher a v2 retrospective projection emits."""
    c = _coord()
    captured = []

    async def run(q, **kw):
        captured.append((q, kw))
        res = MagicMock()
        res.single = AsyncMock(return_value={"n": 0})
        return res

    session = MagicMock()
    session.run = run
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    c._neo4j = MagicMock()
    c._neo4j.session = MagicMock(return_value=ctx)
    await c._apply_retrospective_outbox_row(11, 913, {
        "v": 2, "type": "retrospective", "target_pg_id": target_pg_id,
        "retrospective": {"rating": "validated", "date": "2026-08-27"},
        "content_snippet": "held up", "source": "claude",
        "fact_kind": "tested",
        "grounded": grounded if grounded is not None else [
            {"pg_id": 601, "rel": "GROUNDED_IN",
             "asserted_by": "operator", "label": "Fact"}],
    })
    return captured


@pytest.mark.asyncio
async def test_retrospective_apply_does_not_touch_target_decision_domains():
    """P2. A retrospective's save is a write about the RETROSPECTIVE. It used to
    re-run the decision's inheritance on the way past, on the reasoning that an
    ungrounded decision first reaches facts through its verdict — a
    post-first-write mutation of another record's belonging axis, inferred and
    never asserted (`fact:1671`). No statement this apply issues may name the
    target's pg_id and the domain axis together."""
    captured = await _retro_cypher(target_pg_id=42)
    for q, kw in captured:
        if ONT.domain_of in q:
            assert 42 not in kw.values(), (
                "the retrospective is writing sections onto its decision again")
    assert not any(ONT.domain_of in q for q, _ in captured)


@pytest.mark.asyncio
async def test_retrospective_apply_writes_no_domain_edge_of_its_own():
    """P2, the other half. The verdict took its decision's sections for itself,
    stamped `inherited`. It now carries none: a retrospective asserts no domain
    at ingress, so it has nothing to write, and its belonging is derived on read
    from the decision it judges plus the facts it grounds in."""
    captured = await _retro_cypher()
    for q, _kw in captured:
        assert ONT.domain_of not in q
        assert "inherited" not in q
    # The projection it DOES owe is untouched — this is a removal, not a
    # regression in what a retrospective materialises.
    assert any(f"MERGE (r:{ONT.retrospective}" in q for q, _ in captured)
    assert any(ONT.had_outcome in q for q, _ in captured)
