"""The evidence sweep is RETIRED (v0.9.67) — no process mints a typed
Entity→Entity edge, and the reference resolver has no LLM judge.

Two guards, both structural because the subject is an ABSENCE:

1. The sweep's module is gone — deleted, not fenced off.
2. `reference_resolver` exposes no judge surface and no relation choice at all:
   every citation is REFERENCES (decision:3135), so there is no
   classification left to regain a network branch.

Deliberately NOT guarded: whether the retired name appears anywhere in the
shipped Python. A string-absence check is a taboo-word guard — it breaks on
honest historical context and keeps the retired name alive in the suite
(merger ruling, v0.9.67).
"""
import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")

sys.path.insert(0, SCRIPTS_DIR)
import reference_resolver as rr  # noqa: E402


def test_relation_sweep_module_is_gone():
    assert not os.path.exists(os.path.join(SCRIPTS_DIR, "relation_sweep.py"))


# ── The reference resolver's judge ────────────────────────────────────────────

@pytest.mark.parametrize("attr", ["judge_enabled", "REFERENCE_JUDGE_MODE", "_MODE", "_URL", "_MODEL", "classify_relation", "deterministic_relation"])
def test_reference_resolver_has_no_judge_surface(attr):
    assert not hasattr(rr, attr), f"reference_resolver still exposes {attr}"
