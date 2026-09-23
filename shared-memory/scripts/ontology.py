import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse

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
    # Section of one project; spine, not loaded from ontology.yaml (decision:550).
    domain: str = "Domain"
    activity: str = "Activity"
    milestone: str = "Milestone"
    # First-class spine record keyed by pg_id; HAD_OUTCOME points at it.
    retrospective: str = "Retrospective"
    # Entity sub-labels: allowlist only — REM writes no labels (decision:1664).
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
    # Record→section belonging, reusing PROJECT_OF's established direction: the
    # chain reads (:Fact)-[:DOMAIN_OF]->(:Domain)-[:PROJECT_OF]->(:Project).
    # SPINE, for the same reason `domain` above is.
    domain_of: str = "DOMAIN_OF"
    acted_on_behalf_of: str = "ACTED_ON_BEHALF_OF"
    supersedes: str = "SUPERSEDES"
    informed_by: str = "INFORMED_BY"
    had_outcome: str = "HAD_OUTCOME"
    references: str = "REFERENCES"   # record→record cross-reference resolved from content (Stage 1.2b)
    grounded_in: str = "GROUNDED_IN"  # judgement → fact (decision:550)
    # Spine names kept for compliance; REM writes none of these edges (decision:1664).
    produces_insight: str = "PRODUCES_INSIGHT"
    under_conditions: str = "UNDER_CONDITIONS"
    considered: str = "CONSIDERED"
    rejected: str = "REJECTED"
    # Typed Entity→Entity names: compliance only; saves write MENTIONS (decision:472).
    depends_on: str = "DEPENDS_ON"   # needs / requires (build/config dependency)
    part_of: str = "PART_OF"         # composition / belongs-to
    implements: str = "IMPLEMENTS"   # realises a concept / pattern
    produces: str = "PRODUCES"       # creates output / data / artifact
    consumes: str = "CONSUMES"       # uses another's output / data (runtime I/O)
    runs_on: str = "RUNS_ON"         # executes on / deployed on
    configures: str = "CONFIGURES"   # controls / parametrises / governs
    describes: str = "DESCRIBES"     # documents / specifies (Document→X)
    validates: str = "VALIDATES"     # quality-gate / test / telemetry validates X
    # Lowered from 5 to 3 because the gate now counts grounded facts per (project, domain), which is sparser than the old entity hubs.
    density_threshold: int = 3
    insight_threshold: int = 2


def _load() -> OntologyConfig:
    """Build the ontology config. The SPINE (framework identity — Fact / Decision /
    CommunitySummary / Insight, provenance, alias, grounding, and every relation the
    consolidation dream cycle depends on) is HARDCODED via the dataclass defaults and
    is NEVER read from the config file (decision 550). Only the DOMAIN layer — entity
    sub-labels — plus framework consolidation tuning is read from ontology.yaml's
    `labels:` and `consolidation:` sections; there is no `relationships:` section —
    typed Entity→Entity relationship names are code-pinned like every other
    relationship type. Spine keys present in the file are ignored: the file cannot
    rename or redefine the framework, only extend the domain vocabulary."""
    cfg = OntologyConfig()  # all spine + domain defaults; spine is fixed from here on
    # shared-memory/ontology.yaml, then a repo-root fallback for old checkouts. SMEM_ONTOLOGY_PATH overrides both.
    _here = os.path.dirname(__file__)
    _override = os.environ.get("SMEM_ONTOLOGY_PATH")
    candidates = [_override] if _override else [
        os.path.normpath(os.path.join(_here, "..", "ontology.yaml")),
        os.path.normpath(os.path.join(_here, "..", "..", "ontology.yaml")),
    ]
    if not _yaml_available:
        return cfg
    data = None
    for path in candidates:
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            break
        except FileNotFoundError:
            continue
    if data is None:
        return cfg
    labels = data.get("labels", {})
    cons = data.get("consolidation", {})
    # DOMAIN entity sub-labels (configurable)
    cfg.component = labels.get("component", cfg.component)
    cfg.system = labels.get("system", cfg.system)
    cfg.model = labels.get("model", cfg.model)
    cfg.concept = labels.get("concept", cfg.concept)
    cfg.document = labels.get("document", cfg.document)
    # Framework consolidation tuning (operator-tunable mechanism params, NOT domain vocab)
    cfg.density_threshold = int(cons.get("density_threshold", cfg.density_threshold))
    cfg.insight_threshold = int(cons.get("insight_threshold", cfg.insight_threshold))
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


# Inbound name gate. Leaked ids, booleans, and schema words must not become Entity nodes. This is not a casing pass; aliases unify case variants.

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
    # Bare schema word. The `Domain:` form is refused separately by the axis-declaration pattern.
    "domain", "domain_of",
    # entity type sub-labels + typed relationships (decision 472) — schema vocabulary
    "component", "system", "model", "concept", "document",
    "depends_on", "part_of", "implements", "produces", "consumes",
    "runs_on", "configures", "describes", "validates",
})

_NUMERIC_NAME_RE = re.compile(r"^[0-9]+$")
_WHITESPACE_RE = re.compile(r"\s+")

# `Project:` / `Domain:` is belonging, not a topic, and it already has its own edge. A registry lookup of the bare name would delete real topic hubs, because projects are often named after what they discuss.
_AXIS_DECLARATION_RE = re.compile(r"^\s*(?:project|domain)\s*:", re.IGNORECASE)


def sanitize_entity_name(raw: object) -> str | None:
    """Normalise and validate one entity name. Returns the cleaned name, or None
    if it must be rejected. Pure and deterministic — no I/O.

    Rejection rules: non-string / empty after strip; numeric-only (leaked pg-ids,
    counts); shorter than MIN_ENTITY_NAME_LEN; lowercased form in the noise set;
    an axis declaration (`Project:` / `Domain:` prefix — see
    _AXIS_DECLARATION_RE). Internal whitespace is collapsed to a single space;
    casing is preserved.

    ⚠ This gate governs what reaches the GRAPH, never what is stored. Its callers
    are the outbox→graph projection and REM's proposal gate; the Postgres write
    path does not run it, so a rejected name stays verbatim in the record's
    metadata (Tier 1 pristine) and remains searchable there.
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
    if _AXIS_DECLARATION_RE.match(name):
        return None
    return name


def reserved_entity_name_reason(raw: object) -> str | None:
    """Why this name is RESERVED and can never be an entity — or None. Pure.

    The subset of `sanitize_entity_name`'s rejections that are about what the
    name MEANS rather than about its shape:

      * a schema word — a relationship or label name, or a content-free
        placeholder (`_ENTITY_NOISE_NAMES`)
      * an axis DECLARATION (`Project: …`, `Domain: …`, `_AXIS_DECLARATION_RE`)

    ⚠ IT IS DELIBERATELY NOT "everything sanitize rejects". The SHAPE
    rejections — a leaked pg_id (`254`), a single character, an empty string —
    stay exempt from refusal at ingress: they are noise the record may honestly
    carry, Tier 1 stores them verbatim, and the graph gate drops them. Refusing
    a whole save over one is the regression `_entity_ingress_validate`'s I3
    invariant exists to prevent (`tests/test_entity_vocabulary_ingress.py`,
    "noise sanitize_entity_name rejects is gate-exempt").

    A reserved name is different in kind: an `:Entity` called `Decision` or
    `Project: X` would be a HUB colliding with the ontology's own vocabulary,
    so there is no honest reading under which the caller meant it — which is
    what makes it a question to put back to the operator (400) rather than
    something to drop quietly at the graph boundary.

    ⛔ It says NOTHING about project NAMES. `shared-memory-GitHub` passes every
    rule here, and refusing it is a REGISTRY question (`fact:1215`: a project
    name is an axis, never an entity), answered by the coordinator against the
    live `projects` table — never by a form test in a pure module.
    """
    if not isinstance(raw, str):
        return None
    name = _WHITESPACE_RE.sub(" ", raw.strip())
    if not name:
        return None
    if name.lower() in _ENTITY_NOISE_NAMES:
        return "a schema word (an ontology label, relationship or placeholder)"
    if _AXIS_DECLARATION_RE.match(name):
        return "an axis declaration"
    return None


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


# An :Entity counts as referenced only if it has at least one incoming live MENTIONS edge (decision:890).
GENUINELY_REFERENCED_ENTITY_RULE = (
    "requires >=1 incoming, non-superseded MENTIONS edge — see ontology.py's "
    "GENUINELY_REFERENCED_ENTITY_RULE docstring (decision 890) before changing "
    "how any :Entity consumer decides candidacy for alias/duplicate resolution"
)


# Soft evidential weight derived from source_ref, not a spine label (decision 552). The floor is discussion; observation is a deliberate qualifier, so an unmarked fact is not weighted as evidence.
DISCUSSION_CONTEXT: str = "discussion_context"    # explicit form of the default
OBSERVATION_CONTEXT: str = "observation_context"  # a conclusion reasoned out in the discussion

# A live reading has no file to cite. Without this prefix it would fall through to discussion and weigh the same as a remark.
LIVE_PREFIX: str = "live:"
_LIVE_SCHEMES: tuple[str, ...] = ("neo4j://", "bolt://", "postgres://", "postgresql://")

_CODE_SUFFIXES: tuple[str, ...] = (
    ".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java", ".c", ".cc", ".cpp",
    ".h", ".sh", ".sql", ".yaml", ".yml", ".toml",
)

# Match a path component, not the substring "test". "latest" and "greatest" contain it, and a false tested kind outranks discussion in the insight prompt.
_TEST_TOKEN_RE = re.compile(r"(?:^|[/\\._-])tests?(?:[/\\._-]|$)")


def fact_kind_from_source_ref(source_ref: object) -> str:
    """Derive a fact's soft epistemic kind from its source_ref. Pure, deterministic.

    The FLOOR is 'discussion' — every fact comes out of a conversation, and a
    source_ref names the external context that upgraded it (see the block
    comment above). 'observation' is a deliberate qualifier, never a default.

      none / empty            → 'discussion'   (the floor: unmarked = conversational)
      'discussion_context'    → 'discussion'   (the explicit form of the floor)
      'observation_context'   → 'observation'  (a conclusion reasoned out in the discussion)
      'live:...' / db URI     → 'tested'       (empirical reading off the RUNNING system)
      http(s):// URL          → 'researched'   (external source)
      points into a test path → 'tested'       (empirically verified)
      a source-code file      → 'measured'     (measured from code)
      any other cited doc     → 'researched'
    """
    if not isinstance(source_ref, str) or not source_ref.strip():
        return "discussion"
    low = source_ref.strip().lower()
    if low == DISCUSSION_CONTEXT:
        return "discussion"
    if low == OBSERVATION_CONTEXT:
        return "observation"
    if low.startswith(LIVE_PREFIX) or low.startswith(_LIVE_SCHEMES):
        return "tested"
    if low.startswith(("http://", "https://")):
        return "researched"
    # strip a sub-document locator (file#L10, video@00:04) before keyword/suffix checks
    base = low.split("#", 1)[0].split("@", 1)[0].strip()
    if _TEST_TOKEN_RE.search(base):
        return "tested"
    if base.endswith(_CODE_SUFFIXES):
        return "measured"
    return "researched"


def origin_location(source_ref: object) -> str:
    """The human-citable ORIGIN locus of a fact, derived from its source_ref
    (decision 916). Pure, deterministic — the SAME classification as
    fact_kind_from_source_ref, but returning WHERE the knowledge came from so a
    fold can cite it ("measured from coordinator.py"). Empty string when there is
    no citable EXTERNAL locus — the two conversational kinds are the conversation
    itself, which the kind already conveys:

      none / empty          → ''            (the floor — kind='discussion' says it)
      'discussion_context'  → ''            (same, stated explicitly)
      'observation_context' → ''            (reasoned in-discussion; nothing external to cite)
      'live:neo4j/census'   → 'neo4j/census' (the live locus, prefix stripped)
      http(s):// URL        → the domain    ('arxiv.org')
      code / test / doc path→ the path, sub-document locator (#L10, @00:04) stripped
    """
    if not isinstance(source_ref, str) or not source_ref.strip():
        return ""
    s = source_ref.strip()
    low = s.lower()
    if low in (DISCUSSION_CONTEXT, OBSERVATION_CONTEXT):
        return ""
    if low.startswith(LIVE_PREFIX):
        # The locus is what was read, not the marker: "live:neo4j/entity-census"
        # cites as "neo4j/entity-census".
        return s[len(LIVE_PREFIX):].strip() or s
    if low.startswith(("http://", "https://")):
        netloc = urlparse(s).netloc
        return netloc or s
    return s.split("#", 1)[0].split("@", 1)[0].strip()


# A missing type must not mint a stub under the wrong label. That left the real Decision or Retrospective node unlinked.
RECORD_TYPE_LABELS: dict[str, str] = {
    "decision":      ONT.decision,
    "retrospective": ONT.retrospective,
    "fact":          ONT.fact,
}


def record_label_for_type(record_type: object) -> str:
    """Graph label for a technical_docs record type. Plain facts carry no
    explicit `type`, so None/unknown resolves to Fact — the historical default,
    kept deliberately so an untyped legacy row still lands on a real label."""
    if not isinstance(record_type, str):
        return ONT.fact
    return RECORD_TYPE_LABELS.get(record_type.strip().lower(), ONT.fact)


# Role word to spine relation (decision 582). A flat GROUNDED_IN would erase considered, rejected, and the soft informed_by case.
GROUNDING_ROLES: dict[str, str] = {
    "based_on":         ONT.grounded_in,   # positive evidence / basis
    "grounded_in":      ONT.grounded_in,
    "considered":       ONT.considered,
    "rejected":         ONT.rejected,
    "under_conditions": ONT.under_conditions,
    "informed_by":      ONT.informed_by,   # soft input (not hard basis)
}

# Default only when the operator names no role (decision 582). An explicit role always wins; discussion is the only soft kind.
_FACT_KIND_DEFAULT_ROLE: dict[str, str] = {
    "discussion": ONT.informed_by,
    # observation / tested / measured / researched → GROUNDED_IN (below)
}


def default_grounding_role(fact_kind: object) -> str:
    """Default grounding relation for a fact of the given kind when the operator
    named no explicit role (asserted_by=system_default). Pure, deterministic."""
    return _FACT_KIND_DEFAULT_ROLE.get(fact_kind, ONT.grounded_in)


# Derived from both role maps so a new role cannot be invisible to a traversal. Matching GROUNDED_IN alone hides a discussion that defaulted to INFORMED_BY.
GROUNDING_RELATIONS: tuple[str, ...] = tuple(sorted(
    set(GROUNDING_ROLES.values()) | set(_FACT_KIND_DEFAULT_ROLE.values()) | {ONT.grounded_in}
))


# Outcome states, not valence. `reversed` still drives the supersession cascade. Code-pinned; ontology.yaml cannot rename them.
RETRO_RATINGS: frozenset[str] = frozenset({
    "validated", "mixed", "refined", "pending", "reversed",
})


# Spine is code-pinned and is what NREM walks (decision 550). DOMAIN_LABELS is only the compliance allowlist: no writer stamps those sub-labels (decision:1664).
SPINE_LABELS: frozenset[str] = frozenset({
    ONT.fact, ONT.entity, ONT.community_summary, ONT.reasoning_trace,
    ONT.reasoning_step, ONT.decision, ONT.human, ONT.ai_agent,
    ONT.project, ONT.domain, ONT.activity, ONT.milestone, ONT.retrospective,
})
# DOMAIN_LABELS = configurable subject types; ONT.domain (`:Domain`) is the spine section axis and is in SPINE_LABELS.
DOMAIN_LABELS: frozenset[str] = frozenset({
    ONT.component, ONT.system, ONT.model, ONT.concept, ONT.document,
})
SPINE_RELATIONSHIPS: frozenset[str] = frozenset({
    ONT.entity_link, ONT.entity_link_alias, ONT.aliases, ONT.summarized_by,
    ONT.reasoning_next, ONT.was_attributed_to, ONT.was_assisted_by,
    ONT.was_generated_by, ONT.project_of, ONT.domain_of, ONT.acted_on_behalf_of,
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


def derived_belonging_cypher(hops: int = 4) -> str:
    """Read-only Cypher: a judgement's project is its decision's PROJECT_OF; domains are the same-project union of own, grounded, and judged sections (decision:1736).

    A retrospective anchors through HAD_OUTCOME. Walks do not cross project nodes. Binds `$pg_ids`; hops cap the grounding walk.
    """
    rels = "|".join(GROUNDING_RELATIONS)
    return (
        f"UNWIND $pg_ids AS wanted"
        f" MATCH (j {{pg_id: wanted}})"
        f" WHERE j:{ONT.decision} OR j:{ONT.retrospective}"
        # A retrospective reaches its decision; a decision is its own anchor.
        f" OPTIONAL MATCH (j)<-[:{ONT.had_outcome}]-(dec:{ONT.decision})"
        f" WITH wanted, j, CASE WHEN j:{ONT.decision} THEN j ELSE dec END AS a"
        f" WHERE a IS NOT NULL"
        # THE project node. Every section below is checked against this node.
        f" MATCH (a)-[:{ONT.project_of}]->(p:{ONT.project})"
        f" WITH wanted, p,"
        f"      CASE WHEN j = a THEN [a] ELSE [j, a] END AS anchors"
        # Own sections: asserted on the record or on its anchor.
        f" UNWIND anchors AS n"
        f" OPTIONAL MATCH (n)-[:{ONT.domain_of}]->(od:{ONT.domain})"
        f"                  -[:{ONT.project_of}]->(p)"
        f" WITH wanted, p, anchors, collect(DISTINCT od.name) AS own"
        # Judged sections: what the JUDGEMENTS on the grounding walk assert.
        # An intermediate decision's sections are operator-asserted, exactly
        # like the anchor's own, so the walk stops passing through them.
        f" UNWIND anchors AS n1"
        f" OPTIONAL MATCH (n1)-[:{rels}*1..{hops}]->(m)"
        f"                   -[:{ONT.domain_of}]->(jd:{ONT.domain})"
        f"                   -[:{ONT.project_of}]->(p)"
        f"   WHERE (m:{ONT.decision} OR m:{ONT.retrospective})"
        f"     AND coalesce(m.superseded, false) = false"
        f" WITH wanted, p, anchors, own, collect(DISTINCT jd.name) AS judged"
        # Grounded sections: the live facts either anchor rests on.
        f" UNWIND anchors AS n2"
        f" OPTIONAL MATCH (n2)-[:{rels}*1..{hops}]->(f:{ONT.fact})"
        f"                   -[:{ONT.domain_of}]->(gd:{ONT.domain})"
        f"                   -[:{ONT.project_of}]->(p)"
        f"   WHERE coalesce(f.superseded, false) = false"
        f" WITH wanted, p, own, judged, collect(DISTINCT gd.name) AS grounded"
        f" RETURN wanted AS anchor_pg_id, p.name AS project,"
        f"        own + [x IN judged WHERE NOT x IN own]"
        f"            + [x IN grounded WHERE NOT x IN own AND NOT x IN judged]"
        f"        AS domains"
    )
