"""Decision 582 — typed decision→fact grounding: role vocab, advisory fact_kind
gate, and per-fact role parsing in the CLI. Pure logic only (no infra); the
cross-type apoc writer + resolver are verified live on deploy."""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

from ontology import (  # noqa: E402
    ONT, GROUNDING_ROLES, default_grounding_role, SPINE_RELATIONSHIPS,
)


def _load_bridge():
    path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts", "memory_bridge.py")
    )
    spec = importlib.util.spec_from_file_location("memory_bridge_grounding", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["memory_bridge_grounding"] = mod
    spec.loader.exec_module(mod)
    return mod


mb = _load_bridge()


# ── ontology: role vocab + advisory gate ──────────────────────────────────────

def test_grounding_roles_map_to_spine_relations():
    assert GROUNDING_ROLES["based_on"] == ONT.grounded_in
    assert GROUNDING_ROLES["grounded_in"] == ONT.grounded_in
    assert GROUNDING_ROLES["considered"] == ONT.considered
    assert GROUNDING_ROLES["rejected"] == ONT.rejected
    assert GROUNDING_ROLES["under_conditions"] == ONT.under_conditions
    assert GROUNDING_ROLES["informed_by"] == ONT.informed_by
    # every grounding role is a spine relationship the dream cycle depends on
    assert set(GROUNDING_ROLES.values()) <= SPINE_RELATIONSHIPS


def test_default_role_only_discussion_is_soft():
    # discussion is the single soft kind → INFORMED_BY (not hard basis)
    assert default_grounding_role("discussion") == ONT.informed_by
    # everything else defaults to hard basis
    for kind in ("observation", "tested", "measured", "researched"):
        assert default_grounding_role(kind) == ONT.grounded_in
    # unknown / None → safe hard-basis default
    assert default_grounding_role(None) == ONT.grounded_in
    assert default_grounding_role("nonsense") == ONT.grounded_in


# ── CLI: per-fact role parsing ─────────────────────────────────────────────────

def test_grounded_in_parses_per_fact_roles():
    _, meta = mb.build_decision_metadata(
        title="t", decided_by="X", project="p", rationale="r",
        grounded_in="534:considered,573,575:rejected",
    )
    assert meta["grounded_in"] == [534, 573, 575]
    # only the tagged ids carry a role; bare 573 defers to the fact_kind default
    assert meta["grounded_roles"] == {"534": "considered", "575": "rejected"}


def test_grounded_in_bare_ids_emit_no_roles():
    _, meta = mb.build_decision_metadata(
        title="t", decided_by="X", project="p", rationale="r",
        grounded_in="534, 575",
    )
    assert meta["grounded_in"] == [534, 575]
    assert "grounded_roles" not in meta


def test_grounded_in_roles_lowercased_and_trimmed():
    _, meta = mb.build_decision_metadata(
        title="t", decided_by="X", project="p", rationale="r",
        grounded_in="534: Based_On ",
    )
    assert meta["grounded_in"] == [534]
    assert meta["grounded_roles"] == {"534": "based_on"}
