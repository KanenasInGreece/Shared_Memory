"""Stage 1.2 domain-range map — which typed relationship is legal between which
entity sub-types. Pure gate logic; inert until REM wires it in Stage 1.3.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
from ontology import ONT, DOMAIN_RANGE, is_allowed_relation  # noqa: E402


def test_allowed_core_pairings():
    assert is_allowed_relation(ONT.depends_on, ONT.component, ONT.system)
    assert is_allowed_relation(ONT.runs_on, ONT.component, ONT.system)
    assert is_allowed_relation(ONT.implements, ONT.component, ONT.concept)
    assert is_allowed_relation(ONT.produces, ONT.activity, ONT.document)
    assert is_allowed_relation(ONT.consumes, ONT.component, ONT.model)
    assert is_allowed_relation(ONT.describes, ONT.document, ONT.decision)
    assert is_allowed_relation(ONT.validates, ONT.activity, ONT.model)


def test_concept_reachable_only_via_implements_or_describes():
    # The key guardrail: artifacts must NOT DEPENDS_ON a Concept (hub/modularity trap).
    assert not is_allowed_relation(ONT.depends_on, ONT.component, ONT.concept)
    assert not is_allowed_relation(ONT.depends_on, ONT.system, ONT.concept)
    # but IMPLEMENTS and DESCRIBES may reach it
    assert is_allowed_relation(ONT.implements, ONT.component, ONT.concept)
    assert is_allowed_relation(ONT.describes, ONT.document, ONT.concept)


def test_disallowed_pairings_fall_through():
    # Over-broad edges the gate must reject (REM coerces these to MENTIONS).
    assert not is_allowed_relation(ONT.runs_on, ONT.component, ONT.concept)
    assert not is_allowed_relation(ONT.configures, ONT.document, ONT.model)  # narrowed per review
    assert not is_allowed_relation(ONT.validates, ONT.component, ONT.concept)  # narrowed per review
    assert not is_allowed_relation(ONT.describes, ONT.document, ONT.model)     # narrowed (6→4)
    assert not is_allowed_relation(ONT.implements, ONT.component, ONT.component)


def test_runs_on_target_is_only_system():
    for src in (ONT.component, ONT.system, ONT.model):
        assert is_allowed_relation(ONT.runs_on, src, ONT.system)
        assert not is_allowed_relation(ONT.runs_on, src, ONT.component)


def test_mentions_is_not_in_the_map():
    # MENTIONS is the explicit fallback, intentionally unconstrained / absent.
    assert ONT.entity_link not in DOMAIN_RANGE
    assert not is_allowed_relation(ONT.entity_link, ONT.component, ONT.system)


def test_unknown_rel_or_label_is_false():
    assert not is_allowed_relation("NONSENSE", ONT.component, ONT.system)
    assert not is_allowed_relation(ONT.depends_on, ONT.human, ONT.system)  # Human not a source here


def test_map_only_references_known_labels():
    from ontology import KNOWN_LABELS
    for rel, srcs in DOMAIN_RANGE.items():
        for src, tgts in srcs.items():
            assert src in KNOWN_LABELS, src
            for t in tgts:
                assert t in KNOWN_LABELS, t
