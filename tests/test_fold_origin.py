"""fold_record_line — the per-record line the NREM thematic fold concatenates
into the §3.1 Zettelkasten index (C4: zero/low inference — no LLM call),
differentiating each record by TYPE / evidential KIND / ORIGIN locus (decision
916) / capture date. Pure; no infra. The origin locus threads through as a
PROPERTY (decision 916), never a graph edge — so the fold can cite where a fact
came from ('measured from coordinator.py') without any traversal."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
from consolidation_loop import fold_record_line as line  # noqa: E402


def test_bare_fact_line_when_no_capture_metadata():
    # A fact predating capture metadata (record is None / non-dict) → bare [FACT].
    assert line(None, "some content") == "[FACT] some content"
    assert line("not-a-dict", "x") == "[FACT] x"


def test_origin_marker_present_for_a_citable_locus():
    rec = {"rtype": "fact", "kind": "measured",
           "origin": "coordinator.py", "recorded": "2026-07-27", "pg_id": 42}
    out = line(rec, "the coordinator merges facts")
    assert 'from="coordinator.py"' in out
    assert "kind=measured" in out
    assert out.startswith("[FACT kind=measured")
    # kind precedes origin precedes recorded — a stable, parseable order.
    assert out.index("kind=") < out.index("from=") < out.index("recorded=")


def test_no_origin_marker_for_observation_or_discussion():
    # origin_location returns "" for these, so the marker must be absent entirely —
    # the line must never invent a locus a fact does not have.
    for kind, origin in (("observation", ""), ("discussion", "")):
        out = line({"rtype": "fact", "kind": kind, "origin": origin,
                    "recorded": "2026-07-27"}, "c")
        assert "from=" not in out
        assert f"kind={kind}" in out


def test_record_type_is_uppercased():
    out = line({"rtype": "decision", "kind": "observation", "recorded": "x"}, "c")
    assert out.startswith("[DECISION ")


def test_missing_keys_fall_back_without_raising():
    # A dict missing everything still renders with safe defaults.
    out = line({}, "c")
    assert out == "[FACT kind=observation recorded=unknown pg_id=?] c"
