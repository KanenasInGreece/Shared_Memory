import os
import re
from dataclasses import dataclass

_VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

try:
    import yaml
    _yaml_available = True
except ImportError:
    _yaml_available = False


@dataclass
class OntologyConfig:
    # Node labels — core
    fact: str = "Fact"
    entity: str = "Entity"
    community_summary: str = "CommunitySummary"
    reasoning_trace: str = "ReasoningTrace"
    reasoning_step: str = "ReasoningStep"
    # Node labels — provenance (Phase A)
    decision: str = "Decision"
    human: str = "Human"
    ai_agent: str = "AIAgent"
    project: str = "Project"
    activity: str = "Activity"
    milestone: str = "Milestone"
    # Relationship types — core
    entity_link: str = "MENTIONS"
    entity_link_alias: str = "REPORTS_ON"
    summarized_by: str = "SUMMARIZED_BY"
    reasoning_next: str = "NEXT_STEP"
    # Relationship types — provenance (Phase A)
    was_attributed_to: str = "WAS_ATTRIBUTED_TO"
    was_assisted_by: str = "WAS_ASSISTED_BY"
    was_generated_by: str = "WAS_GENERATED_BY"
    project_of: str = "PROJECT_OF"
    acted_on_behalf_of: str = "ACTED_ON_BEHALF_OF"
    supersedes: str = "SUPERSEDES"
    informed_by: str = "INFORMED_BY"
    had_outcome: str = "HAD_OUTCOME"
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
            decision=labels.get("decision", "Decision"),
            human=labels.get("human", "Human"),
            ai_agent=labels.get("ai_agent", "AIAgent"),
            project=labels.get("project", "Project"),
            activity=labels.get("activity", "Activity"),
            milestone=labels.get("milestone", "Milestone"),
            entity_link=rels.get("entity_link", "MENTIONS"),
            entity_link_alias=rels.get("entity_link_alias", "REPORTS_ON"),
            summarized_by=rels.get("summarized_by", "SUMMARIZED_BY"),
            reasoning_next=rels.get("reasoning_next", "NEXT_STEP"),
            was_attributed_to=rels.get("was_attributed_to", "WAS_ATTRIBUTED_TO"),
            was_assisted_by=rels.get("was_assisted_by", "WAS_ASSISTED_BY"),
            was_generated_by=rels.get("was_generated_by", "WAS_GENERATED_BY"),
            project_of=rels.get("project_of", "PROJECT_OF"),
            acted_on_behalf_of=rels.get("acted_on_behalf_of", "ACTED_ON_BEHALF_OF"),
            supersedes=rels.get("supersedes", "SUPERSEDES"),
            informed_by=rels.get("informed_by", "INFORMED_BY"),
            had_outcome=rels.get("had_outcome", "HAD_OUTCOME"),
            density_threshold=int(cons.get("density_threshold", 5)),
        )
    except FileNotFoundError:
        return OntologyConfig()


def _validate(cfg: OntologyConfig) -> OntologyConfig:
    """Reject label/relationship names that could inject Cypher when interpolated."""
    for field, val in vars(cfg).items():
        if isinstance(val, str) and not _VALID_IDENTIFIER.match(val):
            raise ValueError(
                f"ontology.yaml: {field}={val!r} is not a valid Cypher identifier "
                "(must match [A-Za-z_][A-Za-z0-9_]*)"
            )
    return cfg


ONT = _validate(_load())
