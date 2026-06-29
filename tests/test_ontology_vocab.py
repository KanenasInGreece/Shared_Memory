"""Stage 1.1 ontology vocabulary additions (decision 472).

Locks the 5 new entity sub-labels + 9 typed Entity->Entity relationships into
ONT, the compliance vocabulary (KNOWN_LABELS / KNOWN_RELATIONSHIPS), and the
inbound noise filter. Pure — no infra. Schema defs only; population is Stage 1.3+.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
from ontology import (  # noqa: E402
    ONT, KNOWN_LABELS, KNOWN_RELATIONSHIPS, sanitize_entity_name,
)

NEW_SUBLABELS = {"Component", "System", "Model", "Concept", "Document"}
NEW_RELATIONSHIPS = {
    "DEPENDS_ON", "PART_OF", "IMPLEMENTS", "PRODUCES", "CONSUMES",
    "RUNS_ON", "CONFIGURES", "DESCRIBES", "VALIDATES",
}
_VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def test_ont_exposes_new_sublabels():
    assert {ONT.component, ONT.system, ONT.model, ONT.concept, ONT.document} == NEW_SUBLABELS


def test_ont_exposes_new_relationships():
    got = {ONT.depends_on, ONT.part_of, ONT.implements, ONT.produces,
           ONT.consumes, ONT.runs_on, ONT.configures, ONT.describes, ONT.validates}
    assert got == NEW_RELATIONSHIPS


def test_known_labels_include_sublabels():
    assert NEW_SUBLABELS <= KNOWN_LABELS


def test_known_relationships_include_typed():
    assert NEW_RELATIONSHIPS <= KNOWN_RELATIONSHIPS


def test_provenance_labels_still_present():
    # Reuse decision: Person/Agent/Process map to these, so they must remain known.
    assert {ONT.human, ONT.ai_agent, ONT.activity, ONT.project} <= KNOWN_LABELS


def test_new_vocab_are_valid_cypher_identifiers():
    # They get interpolated into MERGE patterns once REM writes them (1.3).
    for name in NEW_SUBLABELS | NEW_RELATIONSHIPS:
        assert _VALID_IDENTIFIER.match(name), name


def test_new_schema_vocab_rejected_as_entity_names():
    # Bare label/rel words must not become Entity hubs (schema leakage).
    for name in ["Component", "system", "Model", "concept", "Document",
                 "DEPENDS_ON", "depends_on", "Implements", "consumes", "VALIDATES"]:
        assert sanitize_entity_name(name) is None, name


def test_real_entities_still_accepted():
    # Guard against the noise list over-reaching: real names must survive.
    for name in ["BGE-M3", "Gemma4", "coordinator", "OutboxPattern", "Memory Model"]:
        assert sanitize_entity_name(name) == name
