import os
from dataclasses import dataclass

try:
    import yaml
    _yaml_available = True
except ImportError:
    _yaml_available = False


@dataclass
class OntologyConfig:
    # Node labels
    fact: str = "Fact"
    entity: str = "Entity"
    community_summary: str = "CommunitySummary"
    reasoning_trace: str = "ReasoningTrace"
    reasoning_step: str = "ReasoningStep"
    # Relationship types
    entity_link: str = "MENTIONS"
    entity_link_alias: str = "REPORTS_ON"
    summarized_by: str = "SUMMARIZED_BY"
    reasoning_next: str = "NEXT_STEP"
    # Consolidation tuning
    density_threshold: int = 5


def _load() -> OntologyConfig:
    path = os.environ.get(
        "SMEM_ONTOLOGY_PATH",
        os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "ontology.yaml"))
    )
    if not _yaml_available:
        return OntologyConfig()
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
        labels = cfg.get("labels", {})
        rels = cfg.get("relationships", {})
        cons = cfg.get("consolidation", {})
        return OntologyConfig(
            fact=labels.get("fact", "Fact"),
            entity=labels.get("entity", "Entity"),
            community_summary=labels.get("community_summary", "CommunitySummary"),
            reasoning_trace=labels.get("reasoning_trace", "ReasoningTrace"),
            reasoning_step=labels.get("reasoning_step", "ReasoningStep"),
            entity_link=rels.get("entity_link", "MENTIONS"),
            entity_link_alias=rels.get("entity_link_alias", "REPORTS_ON"),
            summarized_by=rels.get("summarized_by", "SUMMARIZED_BY"),
            reasoning_next=rels.get("reasoning_next", "NEXT_STEP"),
            density_threshold=int(cons.get("density_threshold", 5)),
        )
    except FileNotFoundError:
        return OntologyConfig()


ONT = _load()
