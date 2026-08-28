"""Unit tests for migrate_retro_edges.py — the one-time legacy self-loop →
Retrospective-record conversion. Pure planning logic only (no live stores)."""

import importlib.util
import os
import sys


def load_migrator():
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    path = os.path.join(scripts_dir, "migrate_retro_edges.py")
    spec = importlib.util.spec_from_file_location("migrate_retro_edges", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["migrate_retro_edges"] = mod
    spec.loader.exec_module(mod)
    return mod


mig = load_migrator()


def test_rating_map_targets_only_the_enum():
    from ontology import RETRO_RATINGS
    assert set(mig.RATING_MAP.values()) <= set(RETRO_RATINGS)
    assert mig.FALLBACK_RATING in RETRO_RATINGS


def test_build_plan_maps_and_flags():
    loops = [
        {"decision_id": 1, "rating": "high", "date": "2026-06-01",
         "notes": "held", "edge_id": "e1"},
        {"decision_id": 2, "rating": "totally-new-wording", "date": "",
         "notes": "odd", "edge_id": "e2"},
    ]
    plan = mig.build_plan(loops, {})
    assert plan[0]["mapped_rating"] == "validated" and not plan[0]["unmapped"]
    assert plan[0]["created_at"] == "2026-06-01"        # backdated from the edge
    assert plan[1]["mapped_rating"] == mig.FALLBACK_RATING and plan[1]["unmapped"]
    assert plan[1]["created_at"] is None                 # no date → now(), flagged
    assert plan[0]["source"] == "unknown"                # no surviving outbox row


def test_build_plan_recovers_provenance_from_legacy_rows():
    loops = [{"decision_id": 42, "rating": "good", "date": "2026-05-01",
              "notes": "held up well", "edge_id": "e1"}]
    legacy = {(42, "held up well"): {"source": "claude",
                                     "principal": "operator",
                                     "connected_from": {"uid": 1000}}}
    plan = mig.build_plan(loops, legacy)
    assert plan[0]["source"] == "claude"
    assert plan[0]["principal"] == "operator"
    assert plan[0]["connected_from"] == {"uid": 1000}
