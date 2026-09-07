"""Unit tests for _assemble_insight_content — the SLOT invariant (v3, 2026-09-07).

Tests the per-judgement SLOT distillates rendered into the scaffold: a slot whose
pg_id has no row is unreachable, and a row whose slot is absent renders empty
rather than borrowing another judgement's text.

The `reversal_lines` channel and the `PRINCIPLE` paragraph are separate,
explicitly-passed inputs.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import consolidation_loop as cl
from consolidation_loop import _assemble_insight_content, parse_insight_slots

ROWS = [
    (10, "Decision Ten\n\nbody", "p", "decision", {}),
    (20, "held", "p", "retrospective", {"target_pg_id": 10, "rating": "validated"}),
]


def test_extra_slot_is_never_rendered():
    """INVARIANT: a slot whose pg_id has no row is never rendered."""
    slots = {10: "ten-text", 20: "twenty-text", 8888: "FABRICATED", "PRINCIPLE": "p"}
    out = _assemble_insight_content(ROWS, [], slots)
    
    assert "[decision:10] «Decision Ten»\nten-text" in out
    assert "[retrospective:20 → decision:10] rating: validated — twenty-text" in out
    assert "PRINCIPLE: p" in out
    assert "FABRICATED" not in out
    assert "8888" not in out


def test_missing_slot_renders_empty_never_borrows():
    """INVARIANT: a row whose slot is absent renders an empty distillate and never borrows."""
    slots = {10: "ten-text", "PRINCIPLE": "p"}
    out = _assemble_insight_content(ROWS, [], slots)
    
    assert "[decision:10] «Decision Ten»\nten-text" in out
    assert "[retrospective:20 → decision:10] rating: validated — \n\nPRINCIPLE: p" in out
    assert out.count("ten-text") == 1


def test_parse_insight_slots_zero_padded_marker_is_normalised():
    """Language-level normalisation: int("010") == 10."""
    slots, principle = parse_insight_slots("SLOT 010: x\nPRINCIPLE: p")
    assert slots == {10: "x"}
    assert principle == "p"
