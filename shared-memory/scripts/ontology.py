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
    # A SECTION of one project (migration 028). SPINE, pinned here and never
    # read from ontology.yaml — an AMENDMENT to decision 550, recorded as one.
    # The yaml's own header promises that the consolidation cycle reads only
    # spine identifiers and that the configurable vocabulary "never triggers or
    # decides the consolidation mechanism"; the fold gate moves onto this axis,
    # so a renameable `:Domain` would make that sentence false.
    domain: str = "Domain"
    activity: str = "Activity"
    milestone: str = "Milestone"
    # Retrospective-as-record (retro-as-node session, 2026-07-14): a retrospective
    # is a first-class SPINE record — own pg_id/technical_docs row, own node,
    # keyed by pg_id like Fact/Decision (never by name). The Decision keeps a
    # HAD_OUTCOME edge to it as the trigger; NREM hops to the node for rating/
    # content/grounding. Never configurable from ontology.yaml.
    retrospective: str = "Retrospective"
    # Node labels — entity type sub-labels (Path A multi-label under :Entity; decision 472).
    # ⚠ No writer applies these any more — REM stopped assigning them (decision:1664,
    # v0.9.66); every :Entity is merged plain. They survive only as the schema-compliance
    # allowlist (KNOWN_LABELS below). Person/Agent/Process reuse the provenance labels
    # above (Human/AIAgent/Activity).
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
    grounded_in: str = "GROUNDED_IN" # Decision/Retrospective→Fact: fact(s) grounding this record (decision 550) — SPINE
    # REM-enrichment relationships (written by rem_loop.py)
    produces_insight: str = "PRODUCES_INSIGHT"
    under_conditions: str = "UNDER_CONDITIONS"
    considered: str = "CONSIDERED"
    rejected: str = "REJECTED"
    # Relationship types — typed Entity→Entity domain (decision 472). No writer mints
    # these any more: the evidence sweep that proposed them (`relation_sweep.py`) and
    # the confidence calibration that adjudicated them are both retired (v0.9.67/68).
    # `MENTIONS` remains the explicit edge a save writes; these names survive only as
    # the schema-compliance vocabulary for edges already in the graph.
    depends_on: str = "DEPENDS_ON"   # needs / requires (build/config dependency)
    part_of: str = "PART_OF"         # composition / belongs-to
    implements: str = "IMPLEMENTS"   # realises a concept / pattern
    produces: str = "PRODUCES"       # creates output / data / artifact
    consumes: str = "CONSUMES"       # uses another's output / data (runtime I/O)
    runs_on: str = "RUNS_ON"         # executes on / deployed on
    configures: str = "CONFIGURES"   # controls / parametrises / governs
    describes: str = "DESCRIBES"     # documents / specifies (Document→X)
    validates: str = "VALIDATES"     # quality-gate / test / telemetry validates X
    # Consolidation tuning. density_threshold recalibrated 5 -> 3 for the v2
    # FACT GATE (Dreaming Cycle Plan to v2, §2.1) — the population it measures
    # changed from "facts on an entity hub" to "facts GROUNDED_IN by a
    # judgement, grouped by (project, domain)", a structurally smaller and
    # sparser count on this corpus (measured live: two groups gate at 13 and 5
    # grounded facts). See consolidation_loop.py's DENSITY_THRESHOLD.
    density_threshold: int = 3
    insight_threshold: int = 2


def _load() -> OntologyConfig:
    """Build the ontology config. The SPINE (framework identity — Fact / Decision /
    CommunitySummary / Insight, provenance, alias, grounding, and every relation the
    consolidation dream cycle depends on) is HARDCODED via the dataclass defaults and
    is NEVER read from the config file (decision 550). Only the DOMAIN layer — entity
    sub-labels + typed Entity→Entity relationships — plus framework consolidation
    tuning is read from ontology.yaml. Spine keys present in the file are ignored: the
    file cannot rename or redefine the framework, only extend the domain vocabulary."""
    cfg = OntologyConfig()  # all spine + domain defaults; spine is fixed from here on
    # Candidate list (same form as the env loaders): the file lives with the
    # framework at shared-memory/ontology.yaml; the repo root is a FALLBACK
    # for checkouts predating the move. SMEM_ONTOLOGY_PATH overrides both.
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
    # Added with the domain axis (028). `Domain:` was already refused as an axis
    # DECLARATION by _AXIS_DECLARATION_RE; this catches the bare schema word, the
    # same way "project" has been caught since the label existed.
    "domain", "domain_of",
    # entity type sub-labels + typed relationships (decision 472) — schema vocabulary
    "component", "system", "model", "concept", "document",
    "depends_on", "part_of", "implements", "produces", "consumes",
    "runs_on", "configures", "describes", "validates",
})

_NUMERIC_NAME_RE = re.compile(r"^[0-9]+$")
_WHITESPACE_RE = re.compile(r"\s+")

# An AXIS DECLARATION is not a topic name (P14/P18). A name of the form
# `Project: <something>` states which project a record BELONGS TO — that is
# established at first write from the client's working directory, or later by the
# promotion writer, and it is carried by the PROJECT_OF edge. Writing it into the
# topic vocabulary as well makes the axis a hub that records cluster on, which is
# how a project name ends up anchoring a Tier-3 narrative that is about a project
# rather than about a theme.
#
# ⚠ DELIBERATELY A FORM TEST, NEVER A REGISTRY LOOKUP. Resolving a BARE name
# against the project registry would be the obvious implementation and it is
# wrong: registered project names are frequently real topics too — a project is
# often named after the very thing its records discuss, and short registry names
# are ordinary English words. Measured on a live corpus, one registry row was
# simultaneously a `:System` entity carrying 91 inbound edges; a gate that
# resolved bare names would have deleted a hub of true statements the same size
# as the axis hub it was meant to remove.
# A name that spells out `Project:` has declared
# which axis it is on; a bare name has declared nothing. Keeping it a form test
# also keeps this function PURE — no database, no I/O, same contract as every
# other rule here.
#
# `Domain:` is included before the domain axis exists, on purpose: the axis is
# specified and the same mistake is otherwise made twice.
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
# check the way an exclusion list could. `coordinator.py` re-expresses this
# SAME rule in its own Cypher (it cannot import a shared query — different
# runtime/driver) — any consumer doing so must match this criterion exactly,
# not approximate it. The offline `entity_resolution_eval.py` harness that
# once shared the query is retired; nothing else currently applies this rule.
GENUINELY_REFERENCED_ENTITY_RULE = (
    "requires >=1 incoming, non-superseded MENTIONS edge — see ontology.py's "
    "GENUINELY_REFERENCED_ENTITY_RULE docstring (decision 890) before changing "
    "how any :Entity consumer decides candidacy for alias/duplicate resolution"
)


# ── Fact epistemic kind (soft, DERIVED from source_ref) ───────────────────────
# fact_kind is a soft tag — NOT a spine sub-label — giving a stored fact its
# evidential weight for the high-signal grounding story (decision 552 + the
# fact-overload discussion). It is DERIVED from source_ref, never elicited
# separately.
#
# THE FLOOR IS `discussion`, NOT `observation`. Every fact is produced in a
# conversation; that is the base case, not a degenerate one. What a source_ref
# records is which EXTERNAL context entered that conversation and upgraded it:
#   code            -> measured
#   external source -> researched
#   empirical check -> tested   (a test run, OR a reading off the LIVE system)
#   nothing external, a conclusion reasoned out in the discussion -> observation
# So `observation` is a deliberate QUALIFIER ("we reasoned this out"), never a
# default — an unmarked fact is `discussion`, which the advisory gate then
# grounds softly as INFORMED_BY rather than as hard evidence. That is the point:
# an unqualified claim should not enter synthesis weighted as evidence.
DISCUSSION_CONTEXT: str = "discussion_context"    # explicit form of the default
OBSERVATION_CONTEXT: str = "observation_context"  # a conclusion reasoned out in the discussion

# Empirical readings off the RUNNING system (graph census, /health, journal) are
# `tested` — they are verified against reality, not derived from code. They have
# no file to cite, so they carry a `live:` locus (e.g. "live:neo4j/entity-census")
# or a datastore URI. Without this they would fall to the floor and a measurement
# of 4,318 live nodes would weigh the same as a passing remark.
LIVE_PREFIX: str = "live:"
_LIVE_SCHEMES: tuple[str, ...] = ("neo4j://", "bolt://", "postgres://", "postgresql://")

_CODE_SUFFIXES: tuple[str, ...] = (
    ".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java", ".c", ".cc", ".cpp",
    ".h", ".sh", ".sql", ".yaml", ".yml", ".toml",
)

# A path is a TEST path when a path COMPONENT is test-like — not when the string
# merely contains "test". A substring check promoted `scripts/latest_run.py` and
# `notes/greatest_hits.md` to `tested`, the highest evidential weight, because
# "latest" and "greatest" contain "test". Evidence weight must never inflate by
# accident: the insight prompt tells the model tested/measured outranks
# discussion, so a false `tested` silently strengthens a claim.
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


# ── Record type → graph label (the grounding-target resolver) ─────────────────
# A grounding target is any SPINE record, not only a Fact. Resolving its label
# from technical_docs `metadata->>'type'` MUST be exhaustive over the record
# types: a type that falls through to a default mints a stub node under the
# WRONG label, leaving the real node unlinked (the shadow-node class of defect,
# bug 578 — originally found for Decision targets, and repeated for
# Retrospective targets until this map replaced a binary conditional).
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


# Every relationship a grounding edge can carry — DERIVED from both role sources
# (the operator's word and the fact_kind default) so a role can never be added
# without every traversal that reads grounding seeing it.
#
# Anything walking "what grounds this record" must match ALL of these, never
# GROUNDED_IN alone: four of the six role words produce a different relation, and
# INFORMED_BY is what a discussion-kind fact defaults to when the operator names
# no role at all — the bare-pg_id path. Matching one relation makes a decision
# that cites its evidence read as though it rests on nothing.
GROUNDING_RELATIONS: tuple[str, ...] = tuple(sorted(
    set(GROUNDING_ROLES.values()) | set(_FACT_KIND_DEFAULT_ROLE.values()) | {ONT.grounded_in}
))


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
# summarising dream cycle (NREM) depends on. DOMAIN = the entity sub-labels loaded
# from the file; it describes what records are ABOUT. ⚠ Nothing currently applies
# a sub-label to a node — DOMAIN_LABELS below is a compliance ALLOWLIST, not a set
# of labels any writer stamps (REM writes none, `decision:1664`). The typed
# Entity→Entity relation NAMES are code-pinned too (no `relationships:` section
# in the file); only their compliance membership lives here.
# The boundary contract test asserts consolidation touches only SPINE identifiers.
SPINE_LABELS: frozenset[str] = frozenset({
    ONT.fact, ONT.entity, ONT.community_summary, ONT.reasoning_trace,
    ONT.reasoning_step, ONT.decision, ONT.human, ONT.ai_agent,
    ONT.project, ONT.domain, ONT.activity, ONT.milestone, ONT.retrospective,
})
# ⚠ NAMING TRAP, and it is worth the two lines: `DOMAIN_LABELS` below is the
# CONFIGURABLE vocabulary — "domain" in the ontology sense of a subject area —
# while `ONT.domain` is the belonging AXIS and is SPINE. The two senses of the
# word sit three lines apart, so `:Domain` belongs in the set above, never here.
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
    """Where a JUDGEMENT belongs, READ from the graph instead of written into it.

    Successor to `canonical_fixpoint_entity_cypher`, which walked the same
    shape to read a judgement's *entities* and had no caller left. This walks it
    to answer the question `decision:1736` moved to the read side: a decision
    and a retrospective carry only the sections their operator asserted on them,
    so anything they belong to BY VIRTUE OF WHAT THEY REST ON has to be derived
    at the moment it is asked for. Nothing here writes; nothing here is stored.

    Binds `$pg_ids` (a list — one query serves a whole search's judgement hits)
    and returns one row per resolvable judgement:

        anchor_pg_id   the id that was asked about
        project        the project NAME
        domains        the section names, a SET — never a ranking

    THE RULES IT IMPLEMENTS, each of which is a choice that could have gone the
    other way (`decision:1736` (ii)/(iii)):

    * **The anchor is the DECISION.** A retrospective follows `HAD_OUTCOME`
      backwards to the decision it judges; a decision is its own anchor. A
      verdict has no belonging of its own — it belongs where what it judges
      belongs, plus wherever its own measurements were taken.
    * **The project is the anchor's `PROJECT_OF`**, and a decision always
      asserts one at ingress, so this resolves whenever the graph is complete.
      No project, no rows: the answer is "not knowable from the graph", never a
      name-keyed guess.
    * **Domains = own ∪ judged ∪ grounded.** Three collections, all bound to
      the same project node:

        `own`       the sections asserted on the record or on its anchor
        `judged`    the sections asserted on any live DECISION or
                    RETROSPECTIVE reached on the grounding walk
        `grounded`  the sections of the non-superseded FACTS reachable the
                    same way

      Multi-hop because a decision can ground on another judgement, and the
      facts are what carry the axis.

      ⛔ `judged` IS NOT AN OPTIMISATION, IT IS A MISSING HALF (v0.9.72,
      `decision:1756` (4)). The walk always went THROUGH intermediate
      judgements to reach facts, and collected nothing from them — yet a
      decision's own sections are OPERATOR-ASSERTED, the strongest signal on
      the path. Measured live: retro 1694 derived `[]` while decision 1678, on
      its own grounding walk, asserts `architecture`. A judgement that rests on
      a judgement was reading only the leaves of its evidence.

      ⛔ AND `judged` SKIPS A SUPERSEDED JUDGEMENT, exactly as `grounded`
      skips a superseded fact. A decision a retrospective REVERSED is not
      where anything belongs: the reversal is what the supersession cascade
      stamps, and the same predicate already excludes those nodes on both
      sides of the insight gate. Collecting from one would let an overturned
      decision keep filing later work under its sections.

      ⚠ The three are UNIONED AS SETS. Each `collect` is DISTINCT, so no list
      repeats a name internally, and each list is anti-joined against the ones
      before it, so a section asserted on D2 and also carried by a fact appears
      exactly ONCE. A plain `+` would not: list concatenation in Cypher does
      not dedupe.
    * ⛔ **DERIVATION NEVER CROSSES A PROJECT BOUNDARY.** A domain is a SECTION
      OF A PROJECT, so a B-project decision grounded on A-project facts inherits
      none of A's sections. Both halves are bound to the SAME `:Project` NODE
      `p` — node identity, never a name comparison, because two projects can
      carry the same section name and a string match would silently merge them.
    * **"None" is a valid answer** for a decision that asserted nothing and
      rests on facts filed elsewhere. An empty list is the honest result, not a
      failure.

    Bounded by construction: `hops` caps the walk, the pattern is anchored on
    indexed `pg_id`, and the whole thing is one round trip for the batch.
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
