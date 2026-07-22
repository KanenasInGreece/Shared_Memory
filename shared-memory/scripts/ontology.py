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
    # Retrospective-as-record (retro-as-node session, 2026-07-14): a retrospective
    # is a first-class SPINE record — own pg_id/technical_docs row, own node,
    # keyed by pg_id like Fact/Decision (never by name). The Decision keeps a
    # HAD_OUTCOME edge to it as the trigger; NREM hops to the node for rating/
    # content/grounding. Never configurable from ontology.yaml.
    retrospective: str = "Retrospective"
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
    grounded_in: str = "GROUNDED_IN" # Decision/Retrospective→Fact: fact(s) grounding this record (decision 550) — SPINE
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
    """Build the ontology config. The SPINE (framework identity — Fact / Decision /
    CommunitySummary / Insight, provenance, alias, grounding, and every relation the
    consolidation dream cycle depends on) is HARDCODED via the dataclass defaults and
    is NEVER read from the config file (decision 550). Only the DOMAIN layer — entity
    sub-labels + typed Entity→Entity relationships — plus framework consolidation
    tuning is read from ontology.yaml. Spine keys present in the file are ignored: the
    file cannot rename or redefine the framework, only extend the domain vocabulary."""
    cfg = OntologyConfig()  # all spine + domain defaults; spine is fixed from here on
    path = os.environ.get(
        "SMEM_ONTOLOGY_PATH",
        os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "ontology.yaml"))
    )
    if not _yaml_available:
        return cfg
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return cfg
    labels = data.get("labels", {})
    rels = data.get("relationships", {})
    cons = data.get("consolidation", {})
    # DOMAIN entity sub-labels (configurable)
    cfg.component = labels.get("component", cfg.component)
    cfg.system = labels.get("system", cfg.system)
    cfg.model = labels.get("model", cfg.model)
    cfg.concept = labels.get("concept", cfg.concept)
    cfg.document = labels.get("document", cfg.document)
    # DOMAIN typed Entity→Entity relationships (configurable)
    cfg.depends_on = rels.get("depends_on", cfg.depends_on)
    cfg.part_of = rels.get("part_of", cfg.part_of)
    cfg.implements = rels.get("implements", cfg.implements)
    cfg.produces = rels.get("produces", cfg.produces)
    cfg.consumes = rels.get("consumes", cfg.consumes)
    cfg.runs_on = rels.get("runs_on", cfg.runs_on)
    cfg.configures = rels.get("configures", cfg.configures)
    cfg.describes = rels.get("describes", cfg.describes)
    cfg.validates = rels.get("validates", cfg.validates)
    # Framework consolidation tuning (operator-tunable mechanism params, NOT domain vocab)
    cfg.density_threshold = int(cons.get("density_threshold", cfg.density_threshold))
    cfg.insight_threshold = int(cons.get("insight_threshold", cfg.insight_threshold))
    cfg.alias_max_hops = int(cons.get("alias_max_hops", cfg.alias_max_hops))
    return cfg


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
    "under_conditions", "informed_by", "had_outcome", "grounded_in", "supersedes",
    "was_attributed_to", "was_assisted_by", "was_generated_by", "project_of",
    "reports_on", "acted_on_behalf_of", "summarized_by", "next_step",
    "fact", "entity", "decision", "human", "aiagent", "project",
    "activity", "milestone", "communitysummary", "reasoningtrace", "reasoningstep",
    "retrospective",
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


# ── Genuinely-referenced entity (decision 890, fact 889's follow-up finding) ──
# A shape/length-clean name is not the same question as "is this actually a
# named entity". A Decision's own CONSIDERED/REJECTED/UNDER_CONDITIONS/
# PRODUCES_INSIGHT targets are free-text provenance — deliberately allowed to
# be arbitrary-length prose (rem_loop.py's registry gate, decision 718, already
# stops NEW unregistered free phrases from minting a node via those relationship
# types) — but legacy nodes of that shape predate 718 and carry no MENTIONS
# edge at all. Anything that treats every :Entity node as an equally valid
# alias/duplicate-resolution candidate (alias-writer's candidate generation,
# entity-resolution evaluation, entity-graph telemetry, search-time ALIASES
# expansion) must apply this SAME criterion, or the same node is "real" in one
# read path and "provenance noise" in another — exactly the inconsistency this
# ontology module exists to prevent.
#
# THE RULE, to be applied identically everywhere a consumer decides whether an
# :Entity node is eligible for alias/duplicate consideration:
#
#   Eligible  IFF  it has >=1 incoming, non-superseded MENTIONS edge.
#
# Deliberately a POSITIVE check on MENTIONS (the one spine relationship whose
# whole purpose is "content genuinely referenced this as a named entity"),
# not an enumeration of provenance relationship types to exclude — a 5th
# provenance-style relationship type added later cannot silently bypass this
# check the way an exclusion list could. Synchronous scripts share the actual
# query (`entity_resolution_eval.fetch_entities`); the async gateway
# (`coordinator.py`, a different runtime/driver) cannot import that function,
# so it re-expresses the SAME rule in its own Cypher — any consumer doing so
# must match this criterion exactly, not approximate it.
GENUINELY_REFERENCED_ENTITY_RULE = (
    "requires >=1 incoming, non-superseded MENTIONS edge — see ontology.py's "
    "GENUINELY_REFERENCED_ENTITY_RULE docstring (decision 890) before changing "
    "how any :Entity consumer decides candidacy for alias/duplicate resolution"
)


# ── Fact epistemic kind (soft, DERIVED from source_ref) ───────────────────────
# fact_kind is a soft tag — NOT a spine sub-label — giving a stored fact its
# evidential weight for the high-signal grounding story (decision 552 + the
# fact-overload discussion). It is DERIVED from source_ref, never elicited
# separately. A plain stored fact is an observation; its source upgrades it.
DISCUSSION_CONTEXT: str = "discussion_context"  # reserved source_ref for conversation-derived facts

_CODE_SUFFIXES: tuple[str, ...] = (
    ".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java", ".c", ".cc", ".cpp",
    ".h", ".sh", ".sql", ".yaml", ".yml", ".toml",
)


def fact_kind_from_source_ref(source_ref: object) -> str:
    """Derive a fact's soft epistemic kind from its source_ref. Pure, deterministic.

      none / empty          → 'observation'  (a plain stored fact)
      'discussion_context'  → 'discussion'   (from a conversation)
      http(s):// URL        → 'researched'   (external source)
      points into a test    → 'tested'       (empirically verified)
      a source-code file    → 'measured'     (measured from code)
      any other cited doc   → 'researched'
    """
    if not isinstance(source_ref, str) or not source_ref.strip():
        return "observation"
    low = source_ref.strip().lower()
    if low == DISCUSSION_CONTEXT:
        return "discussion"
    if low.startswith(("http://", "https://")):
        return "researched"
    # strip a sub-document locator (file#L10, video@00:04) before keyword/suffix checks
    base = low.split("#", 1)[0].split("@", 1)[0].strip()
    if "test" in base:
        return "tested"
    if base.endswith(_CODE_SUFFIXES):
        return "measured"
    return "researched"


# ── Decision→fact grounding roles + advisory fact_kind gate (decision 582) ─────
# A decision links to each grounding fact by a ROLE relation, not a flat GROUNDED_IN.
# GROUNDING_ROLES maps the operator-facing role word (elicited via --grounded-in
# "pgid:role") to the spine relation. Every one is already a SPINE relationship.
GROUNDING_ROLES: dict[str, str] = {
    "based_on":         ONT.grounded_in,   # positive evidence / basis
    "grounded_in":      ONT.grounded_in,
    "considered":       ONT.considered,
    "rejected":         ONT.rejected,
    "under_conditions": ONT.under_conditions,
    "informed_by":      ONT.informed_by,   # soft input (not hard basis)
}

# Advisory gate (decision 582, OPTION A): fact_kind sets the DEFAULT grounding
# relation when the operator names none — a discussion is soft (INFORMED_BY),
# everything else defaults to hard basis (GROUNDED_IN). This is the minimal soft/
# hard cut; it is NOT enforced — an explicit operator role always wins and is
# recorded asserted_by=operator; nothing is silently rewritten. Hard enforcement
# (option B) is deferred until mis-typing evidence justifies it. Deliberately small
# (only discussion is soft) so it can be refined on the evidence option A gathers.
_FACT_KIND_DEFAULT_ROLE: dict[str, str] = {
    "discussion": ONT.informed_by,
    # observation / tested / measured / researched → GROUNDED_IN (below)
}


def default_grounding_role(fact_kind: object) -> str:
    """Default grounding relation for a fact of the given kind when the operator
    named no explicit role (asserted_by=system_default). Pure, deterministic."""
    return _FACT_KIND_DEFAULT_ROLE.get(fact_kind, ONT.grounded_in)


# ── Retrospective outcome-state ratings (spine) ───────────────────────────────
# The one machine-readable outcome field on a retrospective record (retro-as-node
# session). Outcome STATES, not valence: 'reversed' keeps its structural semantics
# (supersession cascade), 'pending' = not yet judged, 'refined' = the decision
# evolved. Free-text nuance lives in the notes; legacy free-text ratings are
# preserved in metadata.original_rating by the one-time migration. Code-pinned —
# never read from ontology.yaml.
RETRO_RATINGS: frozenset[str] = frozenset({
    "validated", "mixed", "refined", "pending", "reversed",
})


# ── Spine vs Domain split (decision 550) ──────────────────────────────────────
# SPINE = the framework identity / unique selling point — code-pinned, never read
# from ontology.yaml: the high-signal ADR capture (Fact/Decision/CommunitySummary/
# Insight + provenance), alias-not-merge, fact-grounding, and every relation the
# summarising dream cycle (NREM) depends on. DOMAIN = the configurable vocabulary
# (entity sub-labels + typed Entity→Entity relations) loaded from the file; it
# describes what records are ABOUT and is applied only at first-write and REM.
# The boundary contract test asserts consolidation touches only SPINE identifiers.
SPINE_LABELS: frozenset[str] = frozenset({
    ONT.fact, ONT.entity, ONT.community_summary, ONT.reasoning_trace,
    ONT.reasoning_step, ONT.decision, ONT.human, ONT.ai_agent,
    ONT.project, ONT.activity, ONT.milestone, ONT.retrospective,
})
DOMAIN_LABELS: frozenset[str] = frozenset({
    ONT.component, ONT.system, ONT.model, ONT.concept, ONT.document,
})
SPINE_RELATIONSHIPS: frozenset[str] = frozenset({
    ONT.entity_link, ONT.entity_link_alias, ONT.aliases, ONT.summarized_by,
    ONT.reasoning_next, ONT.was_attributed_to, ONT.was_assisted_by,
    ONT.was_generated_by, ONT.project_of, ONT.acted_on_behalf_of,
    ONT.supersedes, ONT.informed_by, ONT.had_outcome, ONT.references,
    ONT.produces_insight, ONT.under_conditions, ONT.considered, ONT.rejected,
    ONT.grounded_in,
})
DOMAIN_RELATIONSHIPS: frozenset[str] = frozenset({
    ONT.depends_on, ONT.part_of, ONT.implements, ONT.produces, ONT.consumes,
    ONT.runs_on, ONT.configures, ONT.describes, ONT.validates,
})

# ── Ontology vocabulary (compliance reference) ────────────────────────────────
# Every node label / relationship type the schema defines = spine ∪ domain.
# Anything in the live graph outside these sets is legacy or foreign drift —
# surfaced by compliance telemetry and reusable by cleanup tooling.
KNOWN_LABELS: frozenset[str] = SPINE_LABELS | DOMAIN_LABELS
KNOWN_RELATIONSHIPS: frozenset[str] = SPINE_RELATIONSHIPS | DOMAIN_RELATIONSHIPS


# ── Domain-range map for typed Entity→Entity relationships (Stage 1.2) ─────────
# Which typed relationship is legal between which entity sub-types — the gate REM
# enforces in Stage 1.3 (an unknown/over-broad typed edge falls back to MENTIONS).
# `rel -> {source_label: frozenset(allowed target labels)}`. MENTIONS is the
# unconstrained fallback and is intentionally absent here. Cross-checked with a
# companion advisor/researcher agent's domain-range map; key guardrail: artifacts
# reach the abstract Concept hub ONLY via IMPLEMENTS / DESCRIBES (never DEPENDS_ON),
# which prevented the modularity collapse that over-broad concept edges cause.
_C, _S, _M, _K, _D = ONT.component, ONT.system, ONT.model, ONT.concept, ONT.document
_A, _DEC = ONT.activity, ONT.decision  # Process reuses Activity

DOMAIN_RANGE: dict[str, dict[str, frozenset[str]]] = {
    ONT.depends_on: {_C: frozenset({_C, _S, _M}), _S: frozenset({_S, _C, _M}),
                     _A: frozenset({_C, _S, _M})},
    ONT.part_of:    {_C: frozenset({_C, _S}), _S: frozenset({_S})},
    ONT.implements: {_C: frozenset({_K}), _S: frozenset({_K})},
    ONT.produces:   {_C: frozenset({_D, _M}), _S: frozenset({_D, _M}),
                     _A: frozenset({_D, _M})},
    ONT.consumes:   {_C: frozenset({_C, _S, _M}), _S: frozenset({_C, _S, _M}),
                     _A: frozenset({_C, _S, _M})},
    ONT.runs_on:    {_C: frozenset({_S}), _S: frozenset({_S}), _M: frozenset({_S})},
    ONT.configures: {_C: frozenset({_C, _S}), _D: frozenset({_C, _S})},
    ONT.describes:  {_D: frozenset({_C, _S, _K, _DEC})},
    ONT.validates:  {_C: frozenset({_C, _S, _M}), _A: frozenset({_C, _S, _M})},
}


def is_allowed_relation(rel: str, src_label: str, tgt_label: str) -> bool:
    """True if a typed Entity→Entity `rel` is permitted from `src_label` to
    `tgt_label` per the domain-range map. Pure. MENTIONS (and any rel not in the
    map) returns False here — callers use MENTIONS as the explicit fallback."""
    return tgt_label in DOMAIN_RANGE.get(rel, {}).get(src_label, frozenset())
