"""Resolve a record's project from Postgres metadata; never fall back to domain (a section) or scope (access control).

SERVER-SIDE ONLY. Never added to ``sync_skills.sh`` or ``shared-memory-skill/``
— the skill is a thin HTTP client and resolution happens at ingress.
"""

import os

from ontology import ONT

# Judgements store the project inside the decision blob; facts store it at the top level. NULL means the caller folds nothing, not a shared bucket.
PROJECT_SQL = "COALESCE(metadata->'decision'->>'project', metadata->>'project')"

# Where a project may be written. Do not substitute PROJECT_SQL: COALESCE would skip a row that still holds the old spelling in the shadowed field.
PROJECT_MATCH_SQL = (
    "(metadata->>'project' = {p} OR metadata->'decision'->>'project' = {p})"
)

# Parked records still save and search, but they must not fold or mint a :Project. The name is reserved.
SENTINEL = "general_discussion"


# Registry (migration 022). Without it a typo and a new project are the same event and both enter silently.

PROJECT_EXISTS_SQL = "SELECT 1 FROM projects WHERE name = $1"

# Every name, unfiltered. A trigram neighbour list let `testing` vs `Test_Ing` (0.545, under the 0.6 floor) register as new. The key is compared in Python so SQL does not grow a second definition.
PROJECT_NAMES_SQL = "SELECT name FROM projects"

# Reads the stored normalized_key, not an expression over name. A WHERE-clause key would follow the server locale and drift from the value stored at registration. Both lookups are one statement so ingress does not pay a second round trip.
PROJECT_NAME_OR_KEY_SQL = (
    "SELECT name FROM projects WHERE name = $1 OR normalized_key = $2"
)

# The id does not move when the label is renamed, and the graph node is keyed on it (migration 027).
PROJECT_ID_SQL = "SELECT id FROM projects WHERE name = $1"

# Trigram first so registration still works when the embedder is down. Project names are short and typo-shaped; description matching belongs on the domain axis.
PROJECT_PROPOSALS_SQL = (
    "SELECT name FROM projects"
    " WHERE similarity(name, $1) >= $2"
    " ORDER BY similarity(name, $1) DESC, name"
    " LIMIT $3"
)

# Loose on purpose. A hyphenated near-miss scores lower than it looks, and a rejected save with no proposals is a dead end.
PROPOSAL_SIMILARITY = 0.25
PROPOSAL_LIMIT = 5


# A "this is new" flag is set by the same agent that mistypes. The spelling check is not overridable; the confusable check is.

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


# Same function as axis_key. The spelling guard (fact:1047) is documented under this name, and the two must not diverge.
spelling_key = axis_key


# Both the Python key and the SQL key are asserted against this list. Unicode numeric characters such as ½ are omitted because str.isalnum and POSIX [:alnum:] disagree, and changing that is fact:1047's guard.
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


# Distinct registered pairs topped out at 0.500 and typos started at 0.78, so 0.6 sits in the gap. Too low trains operators to override; too high never fires.
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


# One resolver for both axes; the caller passes the names in scope. `via` is exact, alias (a retired spelling), or normalised (the key matched). Registry and alias key hits share normalised because the caller only needs to know the literal string was not on file.
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
