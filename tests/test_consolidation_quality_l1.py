"""W3b L1 — the SLOT-distillate invariant (I2) of the insight scaffold.

I2: the per-judgement SLOT distillates rendered into the scaffold are keyed
only to rows in this fold — a slot whose pg_id has no row is unreachable, and
a row whose slot is absent renders empty rather than borrowing another
judgement's text. The `reversal_lines` channel and the `PRINCIPLE` paragraph
are separate, explicitly-passed inputs, outside I2.

The invariant holds by loop-source construction (`_assemble_insight_content`
iterates the sorted ROWS and looks slots up by row pg_id), so no line-flip
mutation exists; the two structural mutations that break it are named in each
test's docstring, measured against a scratchpad copy of the module.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

from consolidation_loop import _assemble_insight_content, parse_insight_slots

# row shape: (pg_id, content, project, rtype, meta) — `_fold_insight`'s fetch shape
ROWS = [
    (10, "Decision Ten\n\nbody", "p", "decision", {}),
    (20, "held", "p", "retrospective", {"target_pg_id": 10, "rating": "validated"}),
]


def _line_starting(out, prefix):
    lines = [l for l in out.splitlines() if l.startswith(prefix)]
    assert len(lines) == 1, lines
    return lines[0]


def test_extra_slot_is_never_rendered():
    """INVARIANT: a slot whose pg_id has no row is never rendered.

    The first three assertions are REGRESSION CONTEXT ONLY — already covered by
    tests/test_nrem_confidence.py:350 (title + pg_id + slot text + PRINCIPLE)
    and :375 (the retrospective line shape); measured, they survive both
    mutations. The invariant is carried by the two NEGATIVE assertions: make
    the loop slot-driven (iterate the slot keys instead of the rows, rendering
    a key with no row) and those two die. The orphan id 8888 is a substring of
    nothing else rendered — keep it that way if the fixture changes.
    """
    slots = {10: "ten-text", 20: "twenty-text", 8888: "FABRICATED", "PRINCIPLE": "p"}
    out = _assemble_insight_content(ROWS, [], slots)

    assert "[decision:10] «Decision Ten»\nten-text" in out
    assert "[retrospective:20 → decision:10] rating: validated — twenty-text" in out
    assert "PRINCIPLE: p" in out
    assert "FABRICATED" not in out
    assert "8888" not in out


def test_missing_slot_renders_empty_never_borrows():
    """INVARIANT: a RETROSPECTIVE row whose slot is absent renders an empty
    distillate and never borrows another judgement's text.

    Mutation: at the `text = (slots.get(pg_id) or "")` line fall back to another
    key's distillate and this test dies while test 1 stays green (measured).
    The distillate VALUE is pinned on its own line, without naming the
    PRINCIPLE paragraph, which is outside I2.
    """
    slots = {10: "ten-text", "PRINCIPLE": "p"}
    out = _assemble_insight_content(ROWS, [], slots)

    assert "[decision:10] «Decision Ten»\nten-text" in out
    retro = _line_starting(out, "[retrospective:20 → decision:10]")
    assert retro.split("—", 1)[1].strip() == ""
    assert out.count("ten-text") == 1


def test_missing_decision_slot_renders_empty_never_borrows():
    """The other direction of I2: a DECISION whose slot is absent must not
    borrow the retrospective's distillate.

    Mutation: allow the borrow fallback for rtype != "retrospective" only, and
    this test dies while the two above stay green (measured by the QA review).
    """
    out = _assemble_insight_content(ROWS, [], {20: "twenty-text", "PRINCIPLE": "p"})

    _line_starting(out, "[decision:10] «Decision Ten»")
    assert out.split("[decision:10] «Decision Ten»\n", 1)[1].splitlines()[0] == ""
    assert out.count("twenty-text") == 1


def test_parse_insight_slots_zero_padded_marker_is_normalised():
    """Language-level normalisation: int("010") == 10.

    Carries NO mutation of its own: measured, every mutation that kills this
    also kills tests/test_nrem_confidence.py:283 and :323, and the mutation
    that isolates the zero-padding path kills nothing. Kept as documentation
    of the normalisation, not as a guard.
    """
    slots, principle = parse_insight_slots("SLOT 010: x\nPRINCIPLE: p")
    assert slots == {10: "x"}
    assert principle == "p"
