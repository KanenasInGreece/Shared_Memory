"""Fix batch C: the NREM `decision_cycles` gauge counts only foldable work, the
ontology thresholds are validated, and `insight_cypher_query`'s docstring is true.

No DB or Neo4j required.
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(_ROOT, "shared-memory", "scripts"))

import ontology  # noqa: E402

_LABELS = {1: "Decision", 2: "Retrospective", 3: "Retrospective", 4: "Decision",
           5: "Retrospective", 6: "Decision", 7: "Decision"}


async def _count(monkeypatch, group_components):
    """Run `_nrem_cycle_counts` over groups given as {first_fact_id: components}."""
    import coordinator
    import nrem_gate

    groups = [(("p", "s%d" % k), [""], [k]) for k in group_components]
    monkeypatch.setattr(nrem_gate, "eligible_domain_level_clusters",
                        lambda *a, **kw: groups)

    async def fake_walk(_neo4j, fact_ids):
        comps = group_components[fact_ids[0]]
        members = {i for c in comps for i in c}
        return ({i: _LABELS[i] for i in members}, {i: False for i in members}, comps)

    monkeypatch.setattr(coordinator, "walk_group_reached_set", fake_walk)

    result = MagicMock()
    result.data = AsyncMock(return_value=[
        {"pg_id": 101, "project": "p", "domain": "d"}])
    session = MagicMock()
    session.run = AsyncMock(return_value=result)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    coord = coordinator.MemoryCoordinator()
    coord._neo4j = MagicMock()
    coord._neo4j.session = MagicMock(return_value=session)
    return await coord._nrem_cycle_counts()


@pytest.mark.asyncio
async def test_decision_cycles_counts_only_groups_with_a_foldable_component(monkeypatch):
    counts = await _count(monkeypatch, {
        10: [[1, 2]],         # Decision + Retrospective: folds
        11: [[2, 3]],         # no Decision: dropped
        12: [[4], [5]],       # single-judgement components only: partitioned out
    })
    assert counts["fact_cycles"] == 3
    assert counts["decision_cycles"] == 1
    assert counts["total_cycles"] == counts["fact_cycles"] + 1


@pytest.mark.asyncio
async def test_a_group_with_one_foldable_and_one_single_component_counts_once(monkeypatch):
    counts = await _count(monkeypatch, {13: [[1, 2], [4]]})
    assert counts["decision_cycles"] == 1


def _write(tmp_path, monkeypatch, body):
    p = tmp_path / "ontology.yaml"
    p.write_text(body)
    monkeypatch.setenv("SMEM_ONTOLOGY_PATH", str(p))


@pytest.mark.parametrize("key", ["density_threshold", "insight_threshold"])
@pytest.mark.parametrize("value", ["3.5", "true", '"3"', "0", "-1", "abc", "null"])
def test_a_bad_threshold_is_refused_naming_the_key(tmp_path, monkeypatch, key, value):
    _write(tmp_path, monkeypatch, f"consolidation:\n  {key}: {value}\n")
    with pytest.raises(ValueError, match=f"consolidation.{key}"):
        ontology._load()


def test_whole_number_thresholds_load(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch,
           "consolidation:\n  density_threshold: 3\n  insight_threshold: 2\n")
    cfg = ontology._load()
    assert (cfg.density_threshold, cfg.insight_threshold) == (3, 2)


def test_shipped_ontology_yaml_is_read_and_keeps_its_thresholds(monkeypatch):
    assert ontology._yaml_available
    shipped = os.path.join(_ROOT, "shared-memory", "ontology.yaml")
    monkeypatch.setenv("SMEM_ONTOLOGY_PATH", shipped)
    import yaml
    with open(shipped) as f:
        data = yaml.safe_load(f)
    assert data["labels"]  # the file was really read, not only the defaults
    cfg = ontology._load()
    assert (cfg.density_threshold, cfg.insight_threshold) == (3, 2)


def test_insight_cypher_query_docstring_no_longer_claims_the_walk():
    from consolidation_loop import insight_cypher_query
    doc = insight_cypher_query.__doc__
    assert "re-derives" not in doc and "WALK" not in doc


def test_the_two_schema_copies_are_byte_identical():
    a = open(os.path.join(_ROOT, "shared-memory", "Documentation", "schema.md"), "rb").read()
    b = open(os.path.join(_ROOT, "shared-memory-skill", "shared-memory",
                          "Documentation", "schema.md"), "rb").read()
    assert a == b
