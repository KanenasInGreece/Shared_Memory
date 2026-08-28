"""Phase 1 inbound entity-name hygiene gate (ontology.sanitize_entity_name[s]).

These are pure-function tests — no infra, no LLM. They lock the behaviour of the
outbox->graph and REM gates: which LLM/agent-supplied names become Entity nodes
and which are rejected as noise. Live evidence motivating the gate: leaked pg-ids
("254".."259") and schema vocabulary had become Entity hubs in the graph.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
from ontology import sanitize_entity_name, sanitize_entity_names  # noqa: E402


# ── Accepted names (kept, casing preserved) ──────────────────────────────────

def test_proper_nouns_pass_unchanged():
    # Proper-noun casing is canonical — the gate must NOT lowercase it.
    for name in ["Neo4j", "LanceDB", "OpenClaw", "Coordinator", "BGE-M3",
                 "shared-memory-GitHub", "Operator"]:
        assert sanitize_entity_name(name) == name


def test_short_meaningful_abbreviations_kept():
    # Default MIN_ENTITY_NAME_LEN=2 keeps two-char abbreviations.
    for name in ["uv", "VM", "ER"]:
        assert sanitize_entity_name(name) == name


def test_multiword_phrases_kept():
    # Decision alternatives are phrases — they remain valid entities.
    assert sanitize_entity_name("synchronous writes") == "synchronous writes"


# ── Rejected names (return None) ─────────────────────────────────────────────

def test_numeric_only_rejected():
    # Leaked pg-ids / counts — the headline live bug.
    for name in ["254", "256", "259", "0", "42"]:
        assert sanitize_entity_name(name) is None


def test_empty_and_whitespace_rejected():
    for name in ["", "   ", "\t", "\n"]:
        assert sanitize_entity_name(name) is None


def test_single_char_rejected():
    for name in ["a", "x", "1", "."]:
        assert sanitize_entity_name(name) is None


def test_booleans_and_placeholders_rejected():
    for name in ["true", "False", "NULL", "none", "n/a", "N/A", "tbd", "TODO",
                 "yes", "no", "unknown"]:
        assert sanitize_entity_name(name) is None


def test_schema_vocabulary_rejected():
    # Relationship/label names leaking from extraction must not become entities.
    for name in ["MENTIONS", "mentions", "PRODUCES_INSIGHT", "Considered",
                 "Fact", "entity", "Decision", "AIAgent", "Project"]:
        assert sanitize_entity_name(name) is None


def test_non_string_rejected():
    for value in [None, 123, 4.5, ["x"], {"a": 1}, True]:
        assert sanitize_entity_name(value) is None


# ── Axis declarations are not topics ─────────────────────────────────────────
# `tests/test_rem_axis_gate.py` closes the TYPED door: the enrichment daemon can
# no longer mint a `:Project` node or point any relation at one. These tests
# close the UNTYPED one beside it, which is the door the data actually came
# through. A name of the form `Project: <something>` is an ordinary `:Entity` —
# it never touches the label allowlist, so nothing in the typed gate can see it,
# and it arrives on the same relation every genuine topic uses.
#
# Shape of the live population that motivated the rule: eleven such entities
# carrying 152 inbound edges, all but four of them the generic topic relation,
# the largest a hub of 91. Records were being clustered by which project they
# belong to rather than by what they are about — a cluster keyed on the axis.

def test_axis_declaration_names_are_rejected():
    # Spelling variants matter: the axis value is a folder name, so it arrives
    # hyphenated, underscored and mixed-case, and every rendering is the same
    # declaration.
    for name in ["Project: alpha-service", "Project: alpha_service",
                 "Project: Alpha-Service-Automation", "Project: alpha",
                 "Project: shared-memory", "Project: shared-memory-monitor"]:
        assert sanitize_entity_name(name) is None, name


def test_axis_declaration_rejected_regardless_of_case_or_spacing():
    # The form is what declares the axis, not a particular rendering of it.
    for name in ["project: alpha", "PROJECT: Alpha", "Project:alpha",
                 "Project : alpha", "  project:  alpha  ", "PrOjEcT:x"]:
        assert sanitize_entity_name(name) is None, name


def test_domain_declarations_rejected_before_the_domain_axis_exists():
    # Deliberately ahead of the code: the domain axis is specified but unbuilt,
    # and admitting `Domain: x` as a topic now would seed the same hub the
    # project sweep just had to remove.
    for name in ["Domain: ontology", "domain: consolidation", "DOMAIN:ops",
                 "Domain : retrieval"]:
        assert sanitize_entity_name(name) is None, name


def test_bare_project_names_are_still_topics():
    """The gate is a FORM test, never a lookup against the project registry —
    and this is the test that makes the difference bite.

    Registered project names are frequently real topics in their own right: a
    project is often named after the very thing its records discuss, and short
    registry names are ordinary English words. Measured before this shipped, one
    registry row was simultaneously a system entity carrying 91 inbound edges —
    the same size as the axis hub that was removed, and every one of those edges
    a true statement about that system. A gate that resolved bare names would
    have deleted it.

    A name spelling out `Project:` has declared which axis it is on. A bare name
    has declared nothing, so it stays a topic.
    """
    for name in ["memory", "skills", "monitor", "gateway",
                 "shared-memory-GitHub", "alpha-service"]:
        assert sanitize_entity_name(name) == name


def test_a_colon_elsewhere_does_not_make_a_name_an_axis_declaration():
    # Only a name that OPENS with the axis word declares an axis. Rejecting on a
    # colon anywhere would take out titles and qualified names wholesale.
    for name in ["Subproject: the alias layer", "Projects: the registry view",
                 "ADR-014: the thin client", "Neo4j: 5.x",
                 "Invariant: axes are not topics"]:
        assert sanitize_entity_name(name) == name


def test_axis_declarations_are_dropped_from_a_list_leaving_the_real_topics():
    raw = ["Neo4j", "Project: alpha", "Outbox", "Domain: ontology", "uv"]
    assert sanitize_entity_names(raw) == ["Neo4j", "Outbox", "uv"]


# ── Normalisation ────────────────────────────────────────────────────────────

def test_whitespace_is_trimmed_and_collapsed():
    assert sanitize_entity_name("  Hive   Mind  ") == "Hive Mind"
    assert sanitize_entity_name("Hive\tMind") == "Hive Mind"


# ── List helper ──────────────────────────────────────────────────────────────

def test_sanitize_list_drops_rejects_dedups_preserves_order():
    raw = ["Neo4j", "254", "  ", "Outbox", "Neo4j", "true", "VM"]
    assert sanitize_entity_names(raw) == ["Neo4j", "Outbox", "VM"]


def test_sanitize_list_handles_non_list():
    for value in [None, "Neo4j", 123, {"a": 1}]:
        assert sanitize_entity_names(value) == []


def test_sanitize_list_all_noise_returns_empty():
    # A fact whose only "entities" were noise legitimately ends up entity-less.
    assert sanitize_entity_names(["254", "true", "n/a", "x"]) == []
