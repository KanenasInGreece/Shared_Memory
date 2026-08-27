"""One project resolution, shared by every reader (invariant P1).

The project a record belongs to is Postgres-metadata semantics, not graph
vocabulary, so this does not belong in ``ontology.py``. It is also not owned by
either daemon: ``coordinator.py`` and ``consolidation_loop.py`` import nothing
from each other, and before this module the same COALESCE existed in eight
places in five files — three of them already canonical, five of them not. Two
readers additionally fell back to ``domain`` and one to ``scope``, so the same
record answered "which project?" differently depending on who asked.

Two things are deliberately OUT of the chain:

* ``domain`` — a domain is a SECTION OF a project. Falling back from a project
  to a section of some project was never defensible: it makes a section answer
  a question about the whole, and on this corpus it is what let 219 of 261
  decisions read as untagged while carrying a project all along.
* ``scope`` — access control, never topical. Including it keys a record by who
  may SEE it rather than what it is ABOUT, which on a deployment that uses
  scopes silently partitions along permission lines.

SERVER-SIDE ONLY. Never added to ``sync_skills.sh`` or ``shared-memory-skill/``
— the skill is a thin HTTP client and resolution happens at ingress.
"""

import os

from ontology import ONT

# The canonical resolution, as SQL. Judgements carry their project inside the
# decision blob; facts carry it at the top level. NULL when neither is present
# — callers decide what an unresolvable project means, and from PR 2 that
# answer is "folds nothing", never a shared bucket.
PROJECT_SQL = "COALESCE(metadata->'decision'->>'project', metadata->>'project')"

# Where a project may be WRITTEN, as a predicate over one parameter.
#
# This is NOT the resolution and must never be substituted for it. A migration
# tool rewriting a legacy spelling has to touch every field the value can live
# in, including the shadowed one: a row carrying the old name in
# ``decision.project`` AND the new name at the top level is a row that still
# needs rewriting, but PROJECT_SQL resolves it to the new name and would skip
# it. Sharing the definition of *where projects live* is the point; sharing
# COALESCE precedence with a writer would silently under-reach.
PROJECT_MATCH_SQL = (
    "(metadata->>'project' = {p} OR metadata->'decision'->>'project' = {p})"
)

# The parked-project sentinel (used from PR 3, reserved from now). A record
# whose project cannot be established saves, searches and enriches normally but
# is excluded from folding — it is never a topic and never mints a :Project
# node. The name is reserved: no real project may be called this.
SENTINEL = "general_discussion"


# ── The registry (migration 022) ─────────────────────────────────────────────
# A project used to be whatever string a client sent, so there was nothing for a
# value to be unknown AGAINST: a typo and a new project were the same event, and
# both entered the corpus silently. These two statements are what make an
# unrecognised value loud instead of merely new.

PROJECT_EXISTS_SQL = "SELECT 1 FROM projects WHERE name = $1"

# EVERY registered name, for the spelling-key comparison below.
#
# ⚠ IT IS NOT FILTERED, and that is the whole point. The spelling check used to
# run only over the TRIGRAM NEIGHBOURS a confusable query returned, which meant a
# separator-and-case variant was refused only when it also happened to score
# above the similarity floor. It frequently does not: measured on a live
# registry, `testing` vs `Test_Ing` scores 0.545 against a floor of 0.6, so a
# pure spelling variant registered as a brand-new value — the exact event the
# guard exists to prevent, slipping past because a SEPARATE, softer heuristic
# did not fire. A spelling is an EXACT equality on a normalised key; it must
# never be gated behind a fuzzy score.
#
# The comparison is done in Python with `same_spelling` rather than as a
# normalising SQL expression, deliberately: a registry is tens of rows, and
# re-expressing `spelling_key` in SQL would create a second definition of the key
# that can drift from the one every other caller uses.
PROJECT_NAMES_SQL = "SELECT name FROM projects"

# The by-key lookup, and the by-name lookup, in ONE indexed statement.
#
# ⚠ IT READS THE STORED COLUMN, not a normalising expression over `name`.
# Migration 035 maintains `projects.normalized_key` with a BEFORE INSERT/UPDATE
# trigger and puts a UNIQUE constraint on it, so the key is a value the database
# owns, indexed, and re-derived only when a row is written. Computing the key in
# the WHERE clause instead would scan, and would make this query's answer depend
# on the SERVER's current locale rather than on what was stored when the name was
# registered — which is exactly the drift a stored column removes.
#
# The key parameter is computed by `axis_key` on this side. That keeps ONE Python
# definition of the key (the comparison stays where `same_spelling` lives) while
# the MATCH happens against the database's own materialised value.
#
# Both halves in one statement because they answer one question — "what does this
# spelling mean?" — and splitting them would cost a second round trip on the
# ingress path for no gain. At most two rows come back: the exact row and the key
# row, which are the same row whenever the caller sent the canonical name.
PROJECT_NAME_OR_KEY_SQL = (
    "SELECT name FROM projects WHERE name = $1 OR normalized_key = $2"
)

# The registry IDENTITY behind a name (migration 027). The name is a label a
# client asserts and an operator types; this is the thing that does not move
# when the label does, and it is what the graph node is keyed on.
PROJECT_ID_SQL = "SELECT id FROM projects WHERE name = $1"

# Proposals for a value that missed. TRIGRAM FIRST, and that ordering is a
# dependency decision, not a ranking preference: trigram needs no embedder, so
# registration cannot be taken down by an embedding outage. A vector signal over
# name + description is added for DOMAINS, where an operator typing `crypto`
# should reach a section named `security` whose description mentions key
# handling — names alone cannot carry that, and descriptions are what make it
# work. Project names are short and typo-shaped, so trigram carries them.
PROJECT_PROPOSALS_SQL = (
    "SELECT name FROM projects"
    " WHERE similarity(name, $1) >= $2"
    " ORDER BY similarity(name, $1) DESC, name"
    " LIMIT $3"
)

# Deliberately loose. A rejected save is a dead end unless the proposals are
# usable, and a near-miss on a hyphenated name scores lower than intuition
# suggests ("shared memory" vs "shared-memory-GitHub").
PROPOSAL_SIMILARITY = 0.25
PROPOSAL_LIMIT = 5


# ── Declaring a NEW project: the two ways it is really a typo ────────────────
#
# A registry only stops a misspelling from becoming a project if declaring a new
# project is harder than mistyping an old one. It is the agent that sets the
# "this is new" flag, and it is the agent that makes the spelling error, so a
# flag alone guards nothing: the operator says "go ahead with this idea" meaning
# THIS project, and a plausible variant silently becomes a second one. Every
# retired spelling this corpus carries arrived exactly that way.
#
# So the same claim faces two checks, and only the second is overridable.

def axis_key(name) -> str:
    """THE normalisation key for both axes: lowercase, letters and digits only.

    ``Orbit_Relay``, ``orbit-relay`` and ``orbit relay`` all reduce to one
    key, which is the point — those are SPELLINGS of one project, never separate
    projects, and no confirmation can make them separate. Separators and case
    are the whole of the difference in every rename this registry has recorded.

    ⚠ THERE IS A SECOND COPY OF THIS RULE, IN SQL — ``axis_normalize(text)``
    (migration 035), which is what the unique functional indexes are built on.
    Two definitions of one key can drift, and a drifted key is the worst
    possible failure here: the database would accept a pair of names Python
    calls one project. So they are held together by a FIXTURE LIST rather than
    by intention — :data:`AXIS_KEY_FIXTURES` below is asserted here by
    ``tests/test_axis_normalized_keys.py`` and asserted again, verbatim, by the
    migration's own ``DO`` block when the merger applies it. A change to either
    side that the other does not follow fails at one of those two points.
    """
    if not isinstance(name, str):
        return ""
    return "".join(ch for ch in name.lower() if ch.isalnum())


# The historical name, kept because it is what the spelling guard (fact:1047)
# is documented under throughout this module. ONE function, two names — the
# guard asks "are these two names the same spelling?", the axis resolver asks
# "what is the key of this name?", and they must never be able to answer
# differently.
spelling_key = axis_key


# The agreement fixture. Each pair is (input, expected key), and BOTH
# implementations are asserted against it — Python in the suite, SQL at
# migration-apply time.
#
# ⚠ WHAT IS DELIBERATELY NOT IN HERE. Python's ``str.isalnum()`` is true for
# Unicode NUMERIC characters that are not digits (``½``, ``²`` — categories No
# and Nl), while Postgres' POSIX ``[:alnum:]`` under a UTF-8 locale generally is
# not. Those characters therefore have no agreed answer and no fixture claims
# one; a name containing them would key differently in the two stores. It has
# never occurred in a registered name on any deployment we can see, and closing
# it means changing ``axis_key``'s behaviour — which is fact:1047's guard, so it
# is a ruling and not a builder's edit. Letters (Latin, accented, Greek) and
# ASCII digits DO agree and are fixtured below.
AXIS_KEY_FIXTURES: tuple[tuple[str, str], ...] = (
    ("orbit-relay", "orbitrelay"),
    ("Orbit_Relay", "orbitrelay"),
    ("orbit relay", "orbitrelay"),
    ("ORBIT-RELAY", "orbitrelay"),
    ("  orbit-relay  ", "orbitrelay"),
    ("orbit.relay", "orbitrelay"),
    ("orbit/relay", "orbitrelay"),
    ("alpha-service-2", "alphaservice2"),
    ("Ops2026", "ops2026"),
    ("Ãgua-Viva", "ãguaviva"),
    ("Ωmega_Project", "ωmegaproject"),
    ("Über-Tooling", "übertooling"),
    ("---", ""),
    ("", ""),
)


def same_spelling(a, b) -> bool:
    """Do two names differ only in separators and case?"""
    key = spelling_key(a)
    return bool(key) and key == spelling_key(b)


# Above this trigram similarity a proposed new name is CONFUSABLE with a
# registered one and must be confirmed as deliberately distinct.
#
# ⚠ Derived from a live registry, not guessed, and env-overridable because the
# right floor depends on how a deployment names things: measured over every pair
# of 37 registered projects, the closest legitimately DISTINCT pair scored 0.500
# and NO pair reached 0.6 — while realistic typos of a registered name scored
# 0.78 to 1.00. The gap between those two populations is where this sits. Too
# low and every new project needs an override, which trains the reflex to
# override; too high and the check never fires.
CONFUSABLE_SIMILARITY = float(os.environ.get("PROJECT_CONFUSABLE_SIMILARITY", "0.6"))

CONFUSABLE_SQL = (
    "SELECT name FROM projects"
    " WHERE similarity(name, $1) >= $2 AND name <> $1"
    " ORDER BY similarity(name, $1) DESC, name"
    " LIMIT $3"
)


def spelling_variant_of(candidate, registered):
    """The registered name `candidate` is merely a SPELLING of, or None. Pure.

    THE ONE IMPLEMENTATION FOR BOTH AXES, because they enforce the same rule and
    two loops would be two rules the day one of them is edited.

    ⚠ `registered` MUST be every registered name, never a similarity-filtered
    slice. This check used to be applied to the trigram neighbours a confusable
    query returned, which quietly made an EXACT rule conditional on a FUZZY one:
    a separator/case variant was refused only when it ALSO scored above the
    similarity floor. Measured on a live registry, `testing` vs `Test_Ing` scores
    0.545 against a floor of 0.6 — so a pure spelling variant registered as a
    brand-new value, which is the precise event the guard exists to prevent.

    ⛔ AND THE FIX IS NOT TO LOWER THE FLOOR. That would flatten two populations
    the floor deliberately separates — legitimately distinct names sit just under
    it — and would train the reflex to override a warning that fires on correct
    input. The two gates answer different questions and run in order: a SPELLING
    is exact equality on a normalised key and cannot be confirmed away; a
    CONFUSABLE is a fuzzy neighbour the operator may confirm as genuinely
    distinct.
    """
    return next((n for n in (registered or []) if same_spelling(n, candidate)),
                None)


# ── Resolving a supplied value to the canonical one ──────────────────────────
#
# THE ONE RESOLUTION FOR BOTH AXES. A project is registered globally and a
# domain is registered within one project, but *how* a supplied spelling becomes
# a canonical one is the same question on both, and two loops would be two rules
# the day one of them is edited — the same reasoning that made
# `spelling_variant_of` shared. Scoping is the CALLER's job: it hands in the
# registered names and the alias map that are in scope, and nothing here knows
# which axis it is serving.

# What `via` reports, and what each token means to a caller reading it back:
#
#   exact       the string they sent is the registered name — nothing changed
#   alias       the string is a registered RETIRED spelling, resolved through
#               the alias junction
#   normalised  the string matched nothing verbatim; it was the axis KEY that
#               matched, so their spelling differs from every registered and
#               aliased string on this axis
#
# ⚠ THE KEY STEPS COLLAPSE INTO ONE TOKEN ON PURPOSE. Steps 3 and 4 look up the
# registry and the alias table respectively, but what a caller needs to know is
# the same in both cases — *the literal string you sent is not on file* — and
# splitting it would report an implementation detail as if it were a difference
# that mattered to them.
VIA_EXACT = "exact"
VIA_ALIAS = "alias"
VIA_NORMALISED = "normalised"


def resolve_axis_value(supplied, registered, aliases) -> tuple:
    """`(canonical, via)` for one supplied axis value — or `(None, None)`. Pure.

    Four steps, in this order, and the order is the whole design:

      1. the registry, EXACTLY   → the ordinary case, and it must stay first so
                                   a save that is already correct costs nothing
      2. an active alias, EXACTLY → a retired spelling that was adjudicated once
      3. the registry, BY KEY     → a separator/case variant of a live name
      4. an active alias, BY KEY  → a separator/case variant of a retired name

    Exact before normalised, on both tables, because a name that IS on file must
    never be answered by something that merely keys the same as it — that would
    let a registered value be silently rewritten to a different registered value
    the day two names share a key. (Migration 035 makes that pair impossible in
    the registry; the ordering here does not depend on it.)

    `registered` is every canonical name in scope; `aliases` maps alias name →
    canonical name, already one-hop by construction (A3, project_alias.py). No
    walk, here or anywhere: a chain is collapsed when a rename is WRITTEN.
    """
    if not isinstance(supplied, str) or not supplied.strip():
        return None, None
    value = supplied.strip()
    names = [n for n in (registered or []) if isinstance(n, str)]
    alias_map = {k: v for k, v in (aliases or {}).items()
                 if isinstance(k, str) and isinstance(v, str)}

    if value in names:
        return value, VIA_EXACT
    if value in alias_map:
        return alias_map[value], VIA_ALIAS

    key = axis_key(value)
    if not key:
        return None, None
    for name in names:
        if axis_key(name) == key:
            return name, VIA_NORMALISED
    for alias, canonical in alias_map.items():
        if axis_key(alias) == key:
            return canonical, VIA_NORMALISED
    return None, None


def expand_axis_spellings(canonical, registered, aliases) -> list:
    """Every stored spelling that MEANS `canonical`, canonical first. Pure.

    This is the read-side twin of :func:`resolve_axis_value`, and it exists
    because ingress canonicalisation is not retroactive. Every value written
    from now on is canonical; the values already in the corpus were written
    under whatever rule was in force at the time, and a filter that matched only
    the canonical string would silently hide them — an empty result that reads
    as "there is nothing here" rather than "you asked with today's spelling".

    The set is: the canonical name, every active alias pointing at it, and every
    registered or aliased spelling that shares its key. Deduplicated, canonical
    first, the rest in a stable sorted order so a test can assert the SQL
    parameter rather than a set.

    ⛔ IT NEVER INCLUDES A SPELLING THAT MEANS SOMETHING ELSE, and an ALIAS is
    admitted on WHAT IT POINTS AT, never on its key. An alias keying the same as
    this canonical but resolving to a different one is an ambiguity, not a
    synonym — the one shape that must not be quietly swept into a filter, since
    a value pulled in wrongly here puts another project's records inside this
    project's answer. Migration 035 makes that pair fail loudly at apply time;
    this reader does not depend on it having run.
    """
    if not isinstance(canonical, str) or not canonical.strip():
        return []
    canonical = canonical.strip()
    key = axis_key(canonical)
    out = {canonical}
    for name in (registered or []):
        if isinstance(name, str) and key and axis_key(name) == key:
            out.add(name)
    for alias, target in (aliases or {}).items():
        if isinstance(alias, str) and isinstance(target, str) \
                and target.strip() == canonical:
            out.add(alias)
    return [canonical] + sorted(out - {canonical})


def unconfirmed_confusables(near, confirmed) -> list:
    """Which near matches the caller has NOT confirmed it means to differ from.

    Confirmation names the specific registered project being distinguished from,
    rather than setting a second boolean: a flag can be flipped without reading
    anything, while naming the neighbour cannot be produced without having seen
    it. Compared on the spelling key, so confirming ``Alpha-Service`` confirms
    ``alpha_service``.
    """
    if isinstance(confirmed, str):
        confirmed = [confirmed]
    keys = {spelling_key(c) for c in (confirmed or []) if isinstance(c, str)}
    return [n for n in (near or []) if spelling_key(n) not in keys]


def project_for_graph(metadata):
    """The project a `:Project` NODE may be minted from — P3 and P8 together.

    Resolution, minus the sentinel. A parked record still saves, still searches
    and still goes through enrichment; what it must not do is put a placeholder
    into the project set, where the insight gate's ">= 2 distinct projects" rule
    would count it as a project like any other and fold on it.
    """
    project = resolve_project(metadata)
    return None if project == SENTINEL else project


def project_merge_cypher(project_id, var: str = "p", name_param: str = "$project") -> str:
    """The MERGE that puts a record's project node in the graph (migration 027).

    A pure function returning Cypher, so the identity rule can be asserted
    directly rather than grepped out of three call sites that each embed it in a
    different surrounding clause.

    WITH an identity, the node is keyed on ``project_id`` and the name is SET as
    a display label. That is what makes a rename cost one property write on one
    node instead of a rewiring: the identity the edges hang off never moves.

    WITHOUT one, the node is keyed on the NAME. ⛔ THAT BRANCH IS NOW REACHED
    BY EXACTLY ONE INPUT: a caller that has no project name to give at all
    (``project_for_graph`` returns None for the parked-record SENTINEL, and the
    outbox FOREACH is guarded). Nothing else may pass None here.

    ⛔ SUPERSEDED RULE (v0.9.69, item 6, ruled R3). This docstring used to state
    the fallback as a DESIGN RULE — *"the WRITE must never be lost"*: an
    unidentified project still got its edge, keyed on its name, on the reasoning
    that a record with no project edge violates the axis outright while the READ
    side (the insight gate) fails closed, so losing the write would trade a
    synthesis risk for data loss.

    That rule is withdrawn, because its premise no longer holds. It was written
    when an UNREGISTERED project name could still reach a save. Under the
    ingress gate every project a save accepts is registered, so a missing
    identity is no longer "a name nobody registered" — it is a data-integrity
    defect or an unreadable registry, and in both cases keying on the name mints
    a SECOND node for a project that already has one, which is the divergence
    migration 027 exists to remove. ``coordinator._project_identity`` therefore
    RAISES rather than returning None: the outbox row retries and then goes
    `failed`, where the failure is VISIBLE, instead of being papered over with a
    duplicate node nobody will notice.

    See ``coordinator.ProjectIdentityUnavailable`` and the v0.9.69
    post-first-write hardening plan (item 6) for the ruling and its callers.
    """
    if project_id is None:
        return f"MERGE ({var}:{ONT.project} {{name: {name_param}}})"
    return (
        f"MERGE ({var}:{ONT.project} {{project_id: $project_id}})"
        f" SET {var}.name = {name_param}"
    )


def fold_eligible(project) -> bool:
    """Invariant P2 — a record with no resolvable project folds NOTHING.

    The one predicate both partitioners call, because the rule is easy to state
    and easy to half-implement. "Not eligible" means the record is **skipped**,
    never bucketed: grouping the unresolvable ones together under a shared key
    is the `general` bucket rebuilt by accident, and that bucket is precisely
    what fused unrelated facts into one narrative. Two records that each fail to
    name a project have nothing in common — their shared property is an ABSENCE,
    and an absence is not a topic.

    Empty and whitespace-only count as absent: a key that renders as nothing is
    the same defect wearing a different value.

    The SENTINEL is excluded too (P5). It is a real, searchable, enrichable
    value — it is simply not a SUBJECT, so folding on it would rebuild the
    `general` bucket under a new name, which is the one outcome this whole line
    of work exists to prevent.
    """
    return (
        isinstance(project, str)
        and bool(project.strip())
        and project.strip() != SENTINEL
    )


def resolve_project(metadata):
    """The Python twin of :data:`PROJECT_SQL` — same order, same exclusions.

    Returns ``None`` when no project is present, so a caller can tell "parked"
    from any particular bucket name. Non-dict input and non-string values
    resolve to ``None`` rather than raising: this sits on the ingress path from
    PR 3, where the metadata blob is client-supplied and untrusted.
    """
    if not isinstance(metadata, dict):
        return None
    decision = metadata.get("decision")
    if isinstance(decision, dict):
        value = decision.get("project")
        if isinstance(value, str) and value.strip():
            return value
    value = metadata.get("project")
    if isinstance(value, str) and value.strip():
        return value
    return None
