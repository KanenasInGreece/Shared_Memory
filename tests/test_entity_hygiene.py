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
                 "shared-memory-GitHub", "Xenofon"]:
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
