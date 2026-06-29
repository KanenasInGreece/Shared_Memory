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
    # Node labels — entity type sub-labels (Path A multi-label under :Entity; decision 472).
    # Stage 1 defines them; REM populates them (1.3) + one-time backfill (1.4). Person/
    # Agent/Process reuse the provenance labels above (Human/AIAgent/Activity).
    component: str = "Component"   # software unit we build (module/class/script/daemon)
    system: str = "System"        # service / datastore / framework / infra we run
    model: str = "Model"          # AI/ML model
    concept: str = "Concept"      # pattern / technique / principle / signal
    document: str = "Document"    # spec / ADR / doc / research artifact
    # Relationship types — core
    entity_link: str = "MENTIONS"
    entity_link_alias: str = "REPORTS_ON"
    aliases: str = "ALIASES"
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
    references: str = "REFERENCES"   # record→record cross-reference resolved from content (Stage 1.2b)
    # REM-enrichment relationships (written by rem_loop.py)
    produces_insight: str = "PRODUCES_INSIGHT"
    under_conditions: str = "UNDER_CONDITIONS"
    considered: str = "CONSIDERED"
    rejected: str = "REJECTED"
    # Relationship types — typed Entity→Entity domain (decision 472). REM picks one
    # (1.3) gated by the domain-range map; MENTIONS stays as fallback until per-edge
    # confidence retires it in v0.6.1.
    depends_on: str = "DEPENDS_ON"   # needs / requires (build/config dependency)
    part_of: str = "PART_OF"         # composition / belongs-to
    implements: str = "IMPLEMENTS"   # realises a concept / pattern
    produces: str = "PRODUCES"       # creates output / data / artifact
    consumes: str = "CONSUMES"       # uses another's output / data (runtime I/O)
    runs_on: str = "RUNS_ON"         # executes on / deployed on
    configures: str = "CONFIGURES"   # controls / parametrises / governs
    describes: str = "DESCRIBES"     # documents / specifies (Document→X)
    validates: str = "VALIDATES"     # quality-gate / test / telemetry validates X
    # Consolidation tuning
    density_threshold: int = 5
    insight_threshold: int = 2
    alias_max_hops: int = 2


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
            component=labels.get("component", "Component"),
            system=labels.get("system", "System"),
            model=labels.get("model", "Model"),
            concept=labels.get("concept", "Concept"),
            document=labels.get("document", "Document"),
            entity_link=rels.get("entity_link", "MENTIONS"),
            entity_link_alias=rels.get("entity_link_alias", "REPORTS_ON"),
            aliases=rels.get("aliases", "ALIASES"),
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
            references=rels.get("references", "REFERENCES"),
            produces_insight=rels.get("produces_insight", "PRODUCES_INSIGHT"),
            under_conditions=rels.get("under_conditions", "UNDER_CONDITIONS"),
            considered=rels.get("considered", "CONSIDERED"),
            rejected=rels.get("rejected", "REJECTED"),
            depends_on=rels.get("depends_on", "DEPENDS_ON"),
            part_of=rels.get("part_of", "PART_OF"),
            implements=rels.get("implements", "IMPLEMENTS"),
            produces=rels.get("produces", "PRODUCES"),
            consumes=rels.get("consumes", "CONSUMES"),
            runs_on=rels.get("runs_on", "RUNS_ON"),
            configures=rels.get("configures", "CONFIGURES"),
            describes=rels.get("describes", "DESCRIBES"),
            validates=rels.get("validates", "VALIDATES"),
            density_threshold=int(cons.get("density_threshold", 5)),
            insight_threshold=int(cons.get("insight_threshold", 2)),
            alias_max_hops=int(cons.get("alias_max_hops", 2)),
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


# ── Entity-name hygiene (inbound quality gate) ────────────────────────────────
# A deterministic "garbage-in" gate applied where names enter the graph
# (outbox→Neo4j projection and REM enrichment). It keeps graph hubs meaningful:
# leaked pg-ids ("254"), booleans, placeholders and schema vocabulary must never
# become Entity nodes. It is NOT a casing pass — proper-noun forms ("Neo4j",
# "LanceDB") are canonical and case-variant unification is the alias layer's job.

# Minimum entity-name length after stripping. Env-tunable. Default 2 keeps useful
# short abbreviations ("uv", "VM", "ER") while dropping single-character noise.
MIN_ENTITY_NAME_LEN: int = int(os.environ.get("MIN_ENTITY_NAME_LEN", "2"))

# Lowercased tokens that must never become Entity nodes.
_ENTITY_NOISE_NAMES: frozenset[str] = frozenset({
    # content-free placeholders / booleans
    "true", "false", "null", "none", "nil", "n/a", "na", "tbd", "todo",
    "yes", "no", "unknown", "undefined", "nan",
    # ontology vocabulary (relationship + label names) — schema leakage, not entities
    "mentions", "aliases", "considered", "rejected", "produces_insight",
    "under_conditions", "informed_by", "had_outcome", "supersedes",
    "was_attributed_to", "was_assisted_by", "was_generated_by", "project_of",
    "reports_on", "acted_on_behalf_of", "summarized_by", "next_step",
    "fact", "entity", "decision", "human", "aiagent", "project",
    "activity", "milestone", "communitysummary", "reasoningtrace", "reasoningstep",
    # entity type sub-labels + typed relationships (decision 472) — schema vocabulary
    "component", "system", "model", "concept", "document",
    "depends_on", "part_of", "implements", "produces", "consumes",
    "runs_on", "configures", "describes", "validates",
})

_NUMERIC_NAME_RE = re.compile(r"^[0-9]+$")
_WHITESPACE_RE = re.compile(r"\s+")


def sanitize_entity_name(raw: object) -> str | None:
    """Normalise and validate one entity name. Returns the cleaned name, or None
    if it must be rejected. Pure and deterministic — no I/O.

    Rejection rules: non-string / empty after strip; numeric-only (leaked pg-ids,
    counts); shorter than MIN_ENTITY_NAME_LEN; lowercased form in the noise set.
    Internal whitespace is collapsed to a single space; casing is preserved.
    """
    if not isinstance(raw, str):
        return None
    name = _WHITESPACE_RE.sub(" ", raw.strip())
    if not name:
        return None
    if _NUMERIC_NAME_RE.match(name):
        return None
    if len(name) < MIN_ENTITY_NAME_LEN:
        return None
    if name.lower() in _ENTITY_NOISE_NAMES:
        return None
    return name


def sanitize_entity_names(raw_names: object) -> list[str]:
    """Sanitise a list of names: drop rejects, de-duplicate, preserve order."""
    seen: set[str] = set()
    out: list[str] = []
    if not isinstance(raw_names, (list, tuple, set)):
        return out
    for r in raw_names:
        n = sanitize_entity_name(r)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


# ── Ontology vocabulary (compliance reference) ────────────────────────────────
# Every node label / relationship type the schema defines. Anything in the live
# graph outside these sets is legacy or foreign drift — surfaced by compliance
# telemetry and reusable by cleanup tooling. Derived from ONT so the vocabulary
# can never disagree with the identifiers the daemons actually write.
KNOWN_LABELS: frozenset[str] = frozenset({
    ONT.fact, ONT.entity, ONT.community_summary, ONT.reasoning_trace,
    ONT.reasoning_step, ONT.decision, ONT.human, ONT.ai_agent,
    ONT.project, ONT.activity, ONT.milestone,
    # entity type sub-labels (decision 472)
    ONT.component, ONT.system, ONT.model, ONT.concept, ONT.document,
})
KNOWN_RELATIONSHIPS: frozenset[str] = frozenset({
    ONT.entity_link, ONT.entity_link_alias, ONT.aliases, ONT.summarized_by,
    ONT.reasoning_next, ONT.was_attributed_to, ONT.was_assisted_by,
    ONT.was_generated_by, ONT.project_of, ONT.acted_on_behalf_of,
    ONT.supersedes, ONT.informed_by, ONT.had_outcome, ONT.references,
    ONT.produces_insight, ONT.under_conditions, ONT.considered, ONT.rejected,
    # typed Entity→Entity domain relationships (decision 472)
    ONT.depends_on, ONT.part_of, ONT.implements, ONT.produces, ONT.consumes,
    ONT.runs_on, ONT.configures, ONT.describes, ONT.validates,
})
