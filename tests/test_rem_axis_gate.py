"""P14 + P18 — REM writes neither axis, and never advertises that it can.

An axis says which project a record BELONGS TO. It is established at first write
from the client's working directory, or later by the promotion writer. It is not
a topic, and it is never inferred from what a record happens to talk about.

REM violated both rules in the shipped code: `:Project` sat in its writable
label set with `PROJECT_OF` as the default relation and `MENTIONS` also allowed.
The consequences were live and reproducible:

  * ANY fact whose text named another project acquired a SECOND, false
    `PROJECT_OF`. Caught on a fact belonging to `shared-memory-GitHub` whose
    content discussed `shared-memory-monitor` — it ended up claiming both.
  * A retired project alias stayed alive after a merge, because REM kept hanging
    `MENTIONS` on the node after normalisation had removed every real edge.

⚠ These tests assert against the GATE TABLES, and the prompt test derives its
expectation from those tables rather than restating them. A prompt that
advertises a relation the code cannot write teaches the model the wrong
contract, and a hand-written list of "relations the prompt may mention" would be
a second copy free to drift from the first — which is the defect, not the test.

No DB or Neo4j required.
"""
import os
import re
import sys

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

import pytest

rem_loop = pytest.importorskip(
    "rem_loop", reason="rem_loop needs the neo4j driver installed"
)
from ontology import ONT


def test_rem_cannot_mint_a_project_node():
    """`_KNOWN_LABELS` is the Cypher-interpolation allowlist — a label in it is a
    label REM can MERGE. `:Project` in that set made REM a second writer of the
    project axis."""
    assert ONT.project not in rem_loop._KNOWN_LABELS


def test_rem_has_no_default_relation_for_the_project_axis():
    assert ONT.project not in rem_loop._LABEL_DEFAULT_REL


def test_rem_permits_no_relation_at_a_project_node():
    """It used to allow PROJECT_OF *and* MENTIONS: the first made REM a second
    writer of the axis, the second made the axis a topic (P14)."""
    assert ONT.project not in rem_loop._LABEL_ALLOWED_RELS


def test_no_label_may_be_reached_by_the_project_edge():
    """The axis relation must be unreachable from every direction, not just from
    the project label — otherwise removing one entry moves the defect rather
    than fixing it."""
    for label, allowed in rem_loop._LABEL_ALLOWED_RELS.items():
        assert ONT.project_of not in allowed, label
    for label, default in rem_loop._LABEL_DEFAULT_REL.items():
        assert default != ONT.project_of, label


def test_the_prompt_never_advertises_a_relation_rem_cannot_write():
    """Derived from the gate, never restated beside it.

    The vocabulary block is what the model is told it may propose. If it lists a
    relation no allowed-set contains, the model is being invited to produce
    output the gate will silently discard — and if that relation is an AXIS, the
    invitation is to mislabel the record's identity.
    """
    writable = set(rem_loop._LABEL_DEFAULT_REL.values())
    for allowed in rem_loop._LABEL_ALLOWED_RELS.values():
        writable |= set(allowed)

    vocab = rem_loop._ONTOLOGY_VOCAB
    # The vocabulary lists one relation per line, first token on the line.
    advertised = {
        m.group(1) for m in re.finditer(r"^\s{2}([A-Z_]{3,})\s", vocab, re.M)
    }
    # Relations written by branches gated on the TARGET LABEL rather than on the
    # relation itself (decision extras) are legitimately advertised and are not
    # in _LABEL_ALLOWED_RELS; the axis is not one of them.
    assert ONT.project_of not in advertised, (
        "the prompt still offers the project axis as something to propose"
    )
    assert ONT.project_of not in writable


def test_the_entity_registry_offers_no_project_to_reference():
    """The accept-set query builds the list of nodes the model may point at. A
    :Project in it invites proposals the gate now throws away — the same
    reasoning the module already applies to its verification rule."""
    import inspect
    body = inspect.getsource(rem_loop.REMDaemon._fetch_closed_entity_set)
    assert f"n:{ONT.project}" not in body, (
        "the accept set still surfaces :Project nodes to the model"
    )
    # The labels it DOES offer must all be ones REM can actually write.
    for label in (ONT.human, ONT.ai_agent, ONT.decision):
        assert label in rem_loop._KNOWN_LABELS


# ── The paths that actually plan edges, for EVERY record kind ────────────────
#
# The tables above are necessary and not sufficient: plan_edges has two branches
# and they are gated in different places — the relationships branch through
# _resolve_rel, the decision-extras branch on the target label. A decision or a
# retrospective must reach the axis through neither.

def _registry_with_a_project():
    """An accept set that still contains a :Project row — the state the query is
    supposed to prevent, used here to prove the SECOND gate holds on its own."""
    return rem_loop._build_entity_registry([
        {"name": "shared-memory-monitor", "labels": [ONT.project], "pg_id": None},
        {"name": "Coordinator", "labels": [ONT.entity], "pg_id": None},
    ])


def test_an_axis_node_never_enters_the_registry_even_if_the_query_returns_one():
    """The redundant gate, and it must be redundant.

    `_safe_label` COERCES an unknown label to :Entity, so with the axis labels
    removed from _KNOWN_LABELS a :Project reaching the registry would be
    silently reclassified as an ordinary entity and handed MENTIONS — the
    axis-as-topic violation by another road, invisible downstream.
    """
    registry = _registry_with_a_project()
    assert "shared-memory-monitor" not in registry
    assert "Coordinator" in registry


def _plan(kind, rel_name, rel_type=None, extras=None):
    result = {"relationships": [{"name": rel_name, **({"rel_type": rel_type} if rel_type else {})}]}
    if extras:
        result.update(extras)
    manifest = {"kind": kind, "entities": [], "existing_edges": [],
                "fact_kind": None, "source_ref": None, "project": None,
                "decision_title": None, "rating": None, "created_at": None}
    return rem_loop.plan_edges(result, _registry_with_a_project(), kind, manifest)


@pytest.mark.parametrize("kind", [
    rem_loop.KIND_FACT, rem_loop.KIND_DECISION, rem_loop.KIND_RETRO,
])
def test_no_record_kind_can_link_to_a_project(kind):
    """Facts, decisions AND retrospectives. The axis is established at first
    write; no record type may acquire it by being enriched."""
    plan = _plan(kind, "shared-memory-monitor", ONT.project_of)
    assert plan["edges"] == []
    assert "shared-memory-monitor" in plan["mint_dropped"]


@pytest.mark.parametrize("kind", [
    rem_loop.KIND_FACT, rem_loop.KIND_DECISION, rem_loop.KIND_RETRO,
])
def test_no_record_kind_can_mention_a_project_either(kind):
    """P14 — the axis is not a topic. Proposing MENTIONS rather than the axis
    edge must fail the same way, or the fix only moved the defect."""
    plan = _plan(kind, "shared-memory-monitor", ONT.entity_link)
    assert plan["edges"] == []


def test_the_decision_extras_branch_cannot_reach_a_project():
    """CONSIDERED / REJECTED / UNDER_CONDITIONS / PRODUCES_INSIGHT never pass
    through _resolve_rel, so the relationships-branch gate does not cover them."""
    extras = {key: ["shared-memory-monitor"] for key in rem_loop._EXTRA_RESULT_KEYS.values()}
    plan = _plan(rem_loop.KIND_DECISION, "Coordinator", ONT.entity_link, extras=extras)
    assert all(e["name"] != "shared-memory-monitor" for e in plan["edges"])
    assert all(e["rel_type"] != ONT.project_of for e in plan["edges"])


def test_a_legitimate_entity_still_links_on_every_kind():
    """The gate must not be a blanket refusal — enrichment still has to work."""
    for kind in (rem_loop.KIND_FACT, rem_loop.KIND_DECISION, rem_loop.KIND_RETRO):
        plan = _plan(kind, "Coordinator", ONT.entity_link)
        assert [e["name"] for e in plan["edges"]] == ["Coordinator"], kind
