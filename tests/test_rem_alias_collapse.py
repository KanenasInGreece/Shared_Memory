"""
REM alias collapse — the accept set is a set of CONCEPTS, not of spellings.

The alias layer (alias_graph.py, GDS wcc) already stamps `Entity.alias_component`
on every surface form of one concept, and NREM and search both group on it. REM
did not: its registry was keyed on the raw `name`, so the prompt offered four
separate "known nodes" for LM Studio / LMStudio / LM_Studio / lm_studio, the model
proposed several, and the verifier confirmed each — four true edges to one
concept. No confidence floor can reach that; the same question was asked four
times.

These tests pin the four places a spelling could still leak through: what the
prompt SHOWS, what the recall slice SPENDS its budget on, what the novelty gate
COMPARES, and what the plan WRITES.

All Neo4j / Postgres / LLM I/O is mocked; no live infrastructure required.
"""

import asyncio
import importlib.util
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest


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

collapse = rem_mod.collapse_alias_components


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _lm_studio_component():
    """The live component 146, verbatim from the graph: four spellings, one
    concept, one of them sub-typed and most-named."""
    return [
        {"labels": [ONT.activity], "name": "LM Studio", "pg_id": None,
         "alias_component": 146, "namings": 6},
        {"labels": [ONT.activity], "name": "LMStudio", "pg_id": None,
         "alias_component": 146, "namings": 1},
        {"labels": [ONT.activity, ONT.system], "name": "LM_Studio", "pg_id": None,
         "alias_component": 146, "namings": 14},
        {"labels": [ONT.activity], "name": "lm_studio", "pg_id": None,
         "alias_component": 146, "namings": 1},
    ]


class _async_ctx:
    def __init__(self, val):  self._val = val
    async def __aenter__(self): return self._val
    async def __aexit__(self, *_): pass


def _make_daemon(data_rows=None):
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


# ── The collapse itself ───────────────────────────────────────────────────────

def test_one_component_collapses_to_one_concept():
    rows = collapse(_lm_studio_component())
    assert len(rows) == 1
    assert rows[0]["name"] == "LM_Studio"
    assert sorted(rows[0]["aliases"]) == ["LM Studio", "LMStudio", "lm_studio"]


def test_canonical_is_the_most_operator_named_spelling():
    """Not the alphabetically first, and not the one the model happened to say:
    the spelling PEOPLE use. `namings` counts first-write namings by live facts
    only — REM's own edges carry asserted_by and are excluded — so the canonical
    moves toward human usage and never drifts on machine output."""
    rows = collapse(_lm_studio_component())
    assert rows[0]["name"] == "LM_Studio"          # 14 namings
    assert "LM Studio" in rows[0]["aliases"]       # 6, and alphabetically first


def test_canonical_is_stable_regardless_of_row_order():
    """An unstable canonical would write a fresh duplicate node every time it
    flipped — the exact defect this collapse exists to remove. Ordering of the
    Neo4j result must not be able to move it."""
    members = _lm_studio_component()
    names = [collapse(list(reversed(members)))[0]["name"],
             collapse(members[2:] + members[:2])[0]["name"],
             collapse(members)[0]["name"]]
    assert set(names) == {"LM_Studio"}


def test_equal_namings_break_alphabetically():
    """Ties must resolve deterministically or the whole stability argument is
    only true of the data that happens to be in the graph today."""
    rows = collapse([
        {"labels": [ONT.entity], "name": "beta",  "alias_component": 7, "namings": 2},
        {"labels": [ONT.entity], "name": "alpha", "alias_component": 7, "namings": 2},
    ])
    assert rows[0]["name"] == "alpha"


def test_a_concept_is_typed_if_any_spelling_is():
    """Sub-labels live on nodes, so one spelling carrying :System is enough for
    the concept to be typed. Asking the model to re-classify it would be exactly
    the waste the delta principle exists to avoid."""
    (row,) = collapse(_lm_studio_component())
    assert ONT.system in row["labels"]
    reg = rem_mod._build_entity_registry([row])
    assert reg["LM_Studio"]["typed"] is True
    assert reg["lm_studio"]["typed"] is True      # via the same entry


def test_rows_without_a_component_stay_separate_concepts():
    """The alias layer stamps nothing until it has ALIAS edges to work with, and
    spine nodes never get a component at all. Every such row must remain its own
    concept — a null component is 'not grouped', never 'all one thing'."""
    rows = collapse([
        {"labels": [ONT.entity],  "name": "Neo4j",   "alias_component": None, "namings": 3},
        {"labels": [ONT.entity],  "name": "Postgres", "alias_component": None, "namings": 2},
        {"labels": [ONT.human],   "name": "Xenofon",  "alias_component": None, "namings": 0},
    ])
    assert sorted(r["name"] for r in rows) == ["Neo4j", "Postgres", "Xenofon"]
    assert all(r["aliases"] == [] for r in rows)


def test_nameless_rows_are_dropped():
    """Decision nodes carry `title`, not `name` (727). They reach the accept set
    and have always been discarded at registry build; the collapse must not
    resurrect them as a nameless concept."""
    assert collapse([{"labels": [ONT.decision], "name": None, "pg_id": 550}]) == []


# ── What the registry does with the alternates ────────────────────────────────

def test_every_spelling_matches_and_all_of_them_write_to_one_node():
    """The whole point, in one assertion: matching widened, writing did not."""
    reg = rem_mod._build_entity_registry(collapse(_lm_studio_component()))
    for spelling in ("LM Studio", "LMStudio", "LM_Studio", "lm_studio"):
        assert spelling in reg
        assert rem_mod.canonical_name(spelling, reg) == "LM_Studio"


def test_an_alias_never_shadows_a_concept_of_its_own():
    """If a name is the head of its own concept, a later concept listing it as an
    alias must not steal the key — otherwise a stale alias edge could silently
    redirect writes away from a live entity."""
    reg = rem_mod._build_entity_registry([
        {"labels": [ONT.entity], "name": "Tier3", "aliases": []},
        {"labels": [ONT.entity], "name": "Tier-3", "aliases": ["Tier3"]},
    ])
    assert rem_mod.canonical_name("Tier3", reg) == "Tier3"


def test_a_spine_node_keeps_its_own_name_against_an_entity_alias():
    """Live collision this decides: two nodes are named `lm_studio` — the
    :Entity in the LM Studio component, and the :AIAgent the MCP client
    authenticates as. The registry is name-keyed and can hold one; which one used
    to depend on Cypher's row order. The head of a concept wins, so a proposal
    naming `lm_studio` resolves to the agent (WAS_ASSISTED_BY), which is what the
    name means when a person writes it."""
    reg = rem_mod._build_entity_registry(
        collapse(_lm_studio_component())
        + [{"labels": [ONT.ai_agent], "name": "lm_studio", "aliases": []}])
    assert reg["lm_studio"]["label"] == ONT.ai_agent
    assert reg["lm_studio"]["default_rel"] == ONT.was_assisted_by
    assert rem_mod.canonical_name("LM Studio", reg) == "LM_Studio"   # unaffected


def test_unknown_names_canonicalise_to_themselves():
    """canonical_name has to be TOTAL — the novelty gate calls it on graph edge
    targets that may sit outside the accept set entirely."""
    assert rem_mod.canonical_name("never-seen", {}) == "never-seen"


# ── What the prompt shows ─────────────────────────────────────────────────────

def test_prompt_names_the_concept_once_and_lists_its_other_spellings():
    """The alternates are SHOWN, not hidden: the prompt's standing rule is to
    match a known name exactly, so a record spelling it 'LM Studio' must still be
    able to find a concept canonicalised as 'LM_Studio'."""
    line = rem_mod._entity_lines(collapse(_lm_studio_component()))
    assert line.count("\n") == 0                   # one concept, one line
    assert "LM_Studio" in line
    assert "also written as" in line
    for alt in ("LM Studio", "LMStudio", "lm_studio"):
        assert alt in line


def test_a_concept_with_no_alternates_renders_unchanged():
    """The no-alias case is the overwhelming majority of the set and must not
    acquire noise."""
    line = rem_mod._entity_lines([
        {"labels": [ONT.entity, ONT.system], "name": "Neo4j", "aliases": []}])
    assert line == f"  {ONT.entity}: Neo4j"


# ── What the recall slice spends its budget on ────────────────────────────────

def test_ranked_alias_resolves_instead_of_being_discarded_as_a_ghost():
    """entity_embeddings is keyed on the surface name and is deliberately left
    alone — it is the recall layer and should keep matching whatever a record
    actually says. So the slice must resolve a ranked ALIAS to its concept rather
    than dropping it for having no row of its own."""
    closed = collapse(_lm_studio_component())
    rows, mode = rem_mod.select_prompt_slice(closed, ["lm_studio"], 5, 10)
    assert mode == "knn"
    assert [r["name"] for r in rows] == ["LM_Studio"]


def test_four_spellings_consume_one_slot_not_four():
    """The k budget must buy k distinct CONCEPTS. Before the collapse, a record
    about LM Studio could spend its whole show-set on four ways of writing it."""
    closed = collapse(_lm_studio_component()) + [
        {"labels": [ONT.entity], "name": "Neo4j", "aliases": []},
        {"labels": [ONT.entity], "name": "Postgres", "aliases": []},
    ]
    ranked = ["LM Studio", "LMStudio", "LM_Studio", "lm_studio", "Neo4j", "Postgres"]
    rows, mode = rem_mod.select_prompt_slice(closed, ranked, 3, 10)
    assert mode == "knn"
    assert [r["name"] for r in rows] == ["LM_Studio", "Neo4j", "Postgres"]


# ── What the novelty gate compares, and what the plan writes ──────────────────

def _collapsed_registry():
    return rem_mod._build_entity_registry(
        collapse(_lm_studio_component())
        + [{"labels": [ONT.activity], "name": "Neo4j", "aliases": []}])


def test_a_proposal_is_written_to_the_canonical_node():
    plan = rem_mod.plan_edges(
        {"relationships": [{"name": "lm_studio", "rel_type": ONT.entity_link}]},
        _collapsed_registry(), rem_mod.KIND_FACT, {})
    assert [(e["name"], e["novel"]) for e in plan["edges"]] == [("LM_Studio", True)]


def test_several_spellings_collapse_to_ONE_proposal_before_verification():
    """Dedupe keys on the canonical name, so the four proposals become one edge —
    and, just as importantly, one k=3 verification round-trip instead of four."""
    plan = rem_mod.plan_edges(
        {"relationships": [
            {"name": "LM Studio", "rel_type": ONT.entity_link},
            {"name": "LMStudio",  "rel_type": ONT.entity_link},
            {"name": "LM_Studio", "rel_type": ONT.entity_link},
            {"name": "lm_studio", "rel_type": ONT.entity_link},
        ]},
        _collapsed_registry(), rem_mod.KIND_FACT, {})
    assert [e["name"] for e in plan["edges"]] == ["LM_Studio"]


def test_an_edge_under_a_SIBLING_spelling_makes_the_proposal_not_novel():
    """The collapse would have produced the very duplication it exists to
    prevent, one spelling later: an anchor already carrying -[MENTIONS]->(LM
    Studio) would see a proposal resolving to LM_Studio as novel and add the
    second edge. Both sides of the novelty comparison are canonicalised."""
    manifest = {"kind": rem_mod.KIND_FACT, "entities": [],
                "existing_edges": [{"rel_type": ONT.entity_link,
                                    "target": "LM Studio"}]}
    plan = rem_mod.plan_edges(
        {"relationships": [{"name": "lm_studio", "rel_type": ONT.entity_link}]},
        _collapsed_registry(), rem_mod.KIND_FACT, manifest)
    (edge,) = plan["edges"]
    assert edge["name"] == "LM_Studio"
    assert edge["novel"] is False       # nothing to write


def test_an_operator_entity_under_a_SIBLING_spelling_also_suppresses_it():
    """Same rule via the other arm of the novelty set: on a FACT the operator's
    `entities` list is a true claim about materialised edges, so a spelling
    there counts as captured for the whole concept."""
    manifest = {"kind": rem_mod.KIND_FACT, "entities": ["LMStudio"],
                "existing_edges": []}
    plan = rem_mod.plan_edges(
        {"relationships": [{"name": "LM Studio", "rel_type": ONT.entity_link}]},
        _collapsed_registry(), rem_mod.KIND_FACT, manifest)
    assert [e["novel"] for e in plan["edges"]] == [False]


def test_an_unrelated_concept_is_still_novel():
    """The novelty gate must not become a blanket suppressor: collapsing changes
    which names are the SAME, not whether anything is new."""
    manifest = {"kind": rem_mod.KIND_FACT, "entities": [],
                "existing_edges": [{"rel_type": ONT.entity_link,
                                    "target": "LM Studio"}]}
    plan = rem_mod.plan_edges(
        {"relationships": [{"name": "Neo4j", "rel_type": ONT.entity_link}]},
        _collapsed_registry(), rem_mod.KIND_FACT, manifest)
    assert [(e["name"], e["novel"]) for e in plan["edges"]] == [("Neo4j", True)]


# ── The accept set feeds the collapse the two fields it needs ─────────────────

def test_accept_set_selects_the_component_and_the_naming_count():
    """Cypher is stubbed everywhere in this repo, so this pins the contract the
    live run verified rather than proving the query. Without these two
    projections the collapse silently degenerates: every row looks like its own
    ungrouped concept and the canonical falls to alphabetical order."""
    d, session = _make_daemon([])
    asyncio.run(d._fetch_closed_entity_set())
    accept_q = session.run.call_args_list[0][0][0]
    assert "alias_component" in accept_q
    assert "AS namings" in accept_q
    assert "COUNT {" in accept_q
