"""`derived_belonging_cypher` — where a judgement belongs, READ, never written.

`decision:1736` moved a judgement's belonging from the write side to the read
side: a decision and a retrospective now carry only the sections their operator
asserted on them, and everything they belong to by virtue of what they REST ON
is derived at the moment it is asked for. This file pins the derivation.

⚠ WHAT THESE TESTS PROVE, AND WHAT THEY DO NOT. Nothing in this suite executes
Cypher — `test_domain_axis.py` says the same thing about its own subject, and it
is the honest limit of a fully-mocked suite. So the worked example from
`fact:1735`

    facts `architecture@A` → decision D `literature@B` → retro R grounded on
    fact `tests@B`   ⇒   D = {literature}, R = {literature, tests},
                         `architecture@A` attaches to NEITHER

is pinned here CLAUSE BY CLAUSE, as the set of structural properties the query
would each have to lose for the example to come out wrong: the anchor rule, the
single project node, the multi-hop grounding walk, the supersession filter, and
the set union. Every one of them is mutation-killable on its own. The example
END TO END is a LIVE verification (`fact:1194`), owed against the running graph
and recorded as owed in the PR — a stub cannot answer a traversal question.
"""
import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

from ontology import (  # noqa: E402
    GROUNDING_RELATIONS, ONT, derived_belonging_cypher,
)


def _q(**kw) -> str:
    return derived_belonging_cypher(**kw)


# ── The clause the worked example turns on: A's sections reach nothing in B ───

def test_both_halves_bind_to_the_same_project_node():
    """⛔ THE CLAUSE THAT EXCLUDES `architecture@A`. A domain is a SECTION OF A
    PROJECT, so the derivation may not cross a project boundary — and the only
    safe way to say that is NODE IDENTITY. Two projects can carry the same
    section name (`architecture` exists under several here), so a name
    comparison would silently merge them, and the merge would look right until
    the day two projects share a section name — which is already true.

    Both the own-sections pattern and the grounded-sections pattern must
    terminate at the SAME bound variable `p`, and `p` must come from the
    ANCHOR's PROJECT_OF, not from a second lookup."""
    q = _q()
    assert f" MATCH (a)-[:{ONT.project_of}]->(p:{ONT.project})" in q, (
        "the project must be bound ONCE, from the anchor")
    own_end = f"(od:{ONT.domain})"
    grounded_end = f"(gd:{ONT.domain})"
    assert own_end in q and grounded_end in q
    for tail in (own_end, grounded_end):
        after = q.split(tail, 1)[1]
        # The next thing either domain pattern does is prove it belongs to p.
        assert after.lstrip().startswith(f"-[:{ONT.project_of}]->(p)"), (
            f"{tail} is not bound to the anchor's project node")
    # And never by name: a `p.name =` comparison anywhere is the defect.
    assert "p.name =" not in q and "p.name=" not in q


# ── The anchor rule: a verdict belongs where what it judges belongs ──────────

def test_a_retrospective_is_anchored_on_the_decision_it_judges():
    """R's project and R's `literature` both come from D. The edge is written
    FROM the decision (`(d)-[:HAD_OUTCOME]->(r)`), so the walk is backwards; a
    forward hop here would find nothing and every retrospective would resolve to
    no belonging at all."""
    q = _q()
    assert f"OPTIONAL MATCH (j)<-[:{ONT.had_outcome}]-(dec:{ONT.decision})" in q
    # A decision is its own anchor — otherwise D itself resolves to nothing.
    assert f"CASE WHEN j:{ONT.decision} THEN j ELSE dec END AS a" in q
    # Both the record and its anchor contribute sections: R must still be able
    # to carry its own, and D's must reach R.
    assert "CASE WHEN j = a THEN [a] ELSE [j, a] END AS anchors" in q


def test_only_a_judgement_is_answered():
    """A fact's belonging is its own bare edges — it needs no derivation and
    must not receive one."""
    q = _q()
    assert f"WHERE j:{ONT.decision} OR j:{ONT.retrospective}" in q
    assert f"j:{ONT.fact}" not in q
    assert f"j:{ONT.community_summary}" not in q


# ── The grounding walk: `tests@B` reaches R ─────────────────────────────────

def test_the_walk_matches_every_grounding_relation_not_grounded_in_alone():
    """Four of the six role words produce a relation other than GROUNDED_IN, and
    a discussion-kind fact cited by bare pg_id defaults to INFORMED_BY. Matching
    one relation makes a decision that cites its evidence read as though it
    rests on nothing — the same trap `GROUNDING_RELATIONS` exists to close."""
    q = _q()
    rels = "|".join(GROUNDING_RELATIONS)
    assert f"-[:{rels}*1..4]->" in q
    for rel in GROUNDING_RELATIONS:
        assert rel in q


def test_the_walk_is_multi_hop_and_capped():
    """Multi-hop because a decision can ground on another judgement and the
    FACTS are what carry the axis; capped because this runs inside a search."""
    assert "*1..4]->" in _q()
    assert "*1..2]->" in _q(hops=2)
    assert "*1..7]->" in _q(hops=7)


def test_a_superseded_fact_carries_nothing_forward():
    """A retired fact's section is not where its decision belongs."""
    assert "coalesce(f.superseded, false) = false" in _q()


# ── The answer's shape ──────────────────────────────────────────────────────

def test_domains_are_a_set_and_never_a_ranking():
    """`decision:1736` (ii) says SET, explicitly. D's own `literature` and a
    grounding fact's `literature` are one section, listed once — and the answer
    carries no order the caller could mistake for importance."""
    q = _q()
    assert "own + [x IN grounded WHERE NOT x IN own] AS domains" in q
    assert "collect(DISTINCT od.name) AS own" in q
    assert "collect(DISTINCT gd.name) AS grounded" in q


def test_the_answer_names_the_record_it_is_about():
    """One query serves a whole search's judgement hits, so every row has to say
    which anchor it answers — a positional match would misfile them the moment a
    judgement resolves to nothing and its row is absent."""
    q = _q()
    assert "UNWIND $pg_ids AS wanted" in q
    assert "RETURN wanted AS anchor_pg_id, p.name AS project," in q


def test_an_unresolvable_project_yields_no_row_rather_than_a_guess():
    """No project, no answer. The alternative — falling back to a name — is the
    same class of defect item 6 closed on the write side: a wrong node minted
    because the right one could not be found."""
    q = _q()
    assert "WHERE a IS NOT NULL" in q
    # A required MATCH, not an OPTIONAL one: no project means no row at all.
    assert f"OPTIONAL MATCH (a)-[:{ONT.project_of}]" not in q


def test_the_derivation_writes_nothing():
    """The whole point. A read that MERGEs is the defect `decision:1736`
    removed, arriving from the other side."""
    q = _q()
    for word in ("MERGE", "CREATE", "SET ", "DELETE", "REMOVE"):
        assert word not in q, f"{word!r} in a derivation that must only read"


def test_the_retired_entity_fixpoint_walk_is_gone():
    """`canonical_fixpoint_entity_cypher` walked this same shape to read a
    judgement's ENTITIES and had no caller left — a judgement carries none
    (`decision:1664`). It is replaced, not kept beside its successor."""
    import ontology
    assert not hasattr(ontology, "canonical_fixpoint_entity_cypher")


# ── The payload key is a CLIENT-FACING contract, so the doc must name it ─────

def test_both_skill_copies_name_the_belonging_key_the_coordinator_emits():
    """A doc describing a contract IS the contract. The expansion payload gained
    a key; a reader who is told "expansion returns its derived belonging" but
    never told what the key is CALLED cannot find it. Pinned against the code's
    own key rather than a literal, so a rename cannot pass by renaming the
    string in one place."""
    import coordinator
    key, = coordinator.MemoryCoordinator._belonging_entry({"project": "p",
                                                           "domains": []})
    assert key == "belonging"
    root = os.path.join(os.path.dirname(__file__), "..")
    copies = [
        os.path.join(root, "shared-memory", "SKILL.md"),
        os.path.join(root, "shared-memory-skill", "shared-memory", "SKILL.md"),
    ]
    for path in copies:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        assert f"`{key}`" in text, f"{path} never names the {key!r} key"
    assert open(copies[0], encoding="utf-8").read() == \
        open(copies[1], encoding="utf-8").read(), "the two SKILL.md copies diverged"
