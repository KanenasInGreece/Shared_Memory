"""Domain names are project-local sections; facts and decisions assert them, retrospectives do not, and NREM folds on (project, domain).

Lookups take a project id. A record may list several sections. A missing domain identity writes no graph edge (no name-keyed fallback). Server-side only.
"""

import os

from ontology import ONT

# Both spellings are the same field. Accepting only one would refuse the careless caller and silently accept the careful one.
DOMAIN_KEYS: tuple[str, ...] = ("domain", "domains")


def _domains_from(blob) -> list[str]:
    """Domain names carried by one dict, under either key. Pure."""
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(blob, dict):
        return out
    for key in DOMAIN_KEYS:
        raw = blob.get(key)
        values = raw if isinstance(raw, (list, tuple)) else [raw]
        for value in values:
            if not isinstance(value, str):
                continue
            name = value.strip()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out


def resolve_domains(metadata) -> list[str]:
    """The domains a record names, normalised to a list. Pure, total.

    Accepts a bare string or a list of strings under either key, strips each
    value, drops blanks, and de-duplicates while preserving order. Returns ``[]``
    when the record names none, which is the ordinary case: a record with no
    domain is a record filed under its project and nothing narrower, and that is
    never an error.

    ⚠ SAME PRECEDENCE AS ``PROJECT_SQL``: a judgement carries its axis values
    inside the ``decision`` blob, a fact at the top level, and the blob wins.
    The two axes must resolve out of the same place or a decision's project and
    its domain can come from different halves of one record — which is how a
    record ends up filed in a section of a project it does not belong to.

    Non-dict input and non-string values resolve to ``[]``/are skipped rather
    than raising — this sits on the ingress path, where the metadata blob is
    client-supplied and untrusted.
    """
    if not isinstance(metadata, dict):
        return []
    from_blob = _domains_from(metadata.get("decision"))
    return from_blob if from_blob else _domains_from(metadata)


def names_a_domain(metadata) -> bool:
    """Did the caller SUPPLY a domain field at all? (P17's refusal test.)

    Deliberately distinct from ``resolve_domains(...) != []``. A judgement
    carrying ``"domain": ""`` or ``"domain": []`` has still reached for the
    field, and answering "no domain here" would let the one shape that needs
    telling apart — an agent that believes judgements carry domains — pass
    unremarked. Presence of the KEY is the signal; what is in it is not.

    ⚠ IT LOOKS INSIDE THE `decision` BLOB TOO, because that is where a decision's
    own axis value lives: ``resolve_project`` reads the project from there, so an
    agent mirroring the shape it already knows would naturally put a domain
    beside it. Checking only the top level would refuse the careless caller and
    silently accept the one who followed the existing pattern — precisely
    backwards.
    """
    if not isinstance(metadata, dict):
        return False
    if any(k in metadata for k in DOMAIN_KEYS):
        return True
    blob = metadata.get("decision")
    return isinstance(blob, dict) and any(k in blob for k in DOMAIN_KEYS)


# Every lookup takes a project id (migration 028). A name alone would repeat the project axis's original defect.

DOMAIN_EXISTS_SQL = (
    "SELECT id FROM project_domains WHERE project_id = $1 AND name = $2"
)

# Unfiltered, same reason as PROJECT_NAMES_SQL: a spelling below the trigram floor would otherwise register as a new section.
DOMAIN_NAMES_SQL = "SELECT name FROM project_domains WHERE project_id = $1"

# Stored key, same reason as PROJECT_NAME_OR_KEY_SQL. Arrays because one record names several sections; a single-value call left the rest unresolved.
DOMAIN_NAME_OR_KEY_SQL = (
    "SELECT name FROM project_domains"
    " WHERE project_id = $1"
    "   AND (name = ANY($2::text[]) OR normalized_key = ANY($3::text[]))"
)

# Description match is the difference from projects. `crypto` will never trigram-match a section named `security`.
DOMAIN_PROPOSALS_SQL = (
    "SELECT name FROM ("
    "  SELECT name, similarity(name, $2) AS score"
    "    FROM project_domains WHERE project_id = $1 AND similarity(name, $2) >= $3"
    "  UNION"
    "  SELECT name, similarity(coalesce(description, ''), $2) AS score"
    "    FROM project_domains"
    "   WHERE project_id = $1 AND similarity(coalesce(description, ''), $2) >= $3"
    ") m ORDER BY score DESC, name LIMIT $4"
)

# Same floor as projects: a rejected save with no usable proposals is a dead end.
DOMAIN_PROPOSAL_SIMILARITY = 0.25
DOMAIN_PROPOSAL_LIMIT = 5

# Own name so a deployment can move it without moving the project floor. Sections are named more loosely, so this is the one more likely to need moving.
DOMAIN_CONFUSABLE_SIMILARITY = float(
    os.environ.get("DOMAIN_CONFUSABLE_SIMILARITY", "0.6")
)

DOMAIN_CONFUSABLE_SQL = (
    "SELECT name FROM project_domains"
    " WHERE project_id = $1 AND similarity(name, $2) >= $3 AND name <> $2"
    " ORDER BY similarity(name, $2) DESC, name LIMIT $4"
)

# Joins on project_id. An alias string is not unique across projects.
DOMAIN_ALIAS_RESOLVE_SQL = (
    "SELECT d.name FROM domain_aliases da"
    "  JOIN aliases a ON a.id = da.alias_id"
    "  JOIN project_domains d ON d.id = da.domain_id"
    " WHERE da.active AND da.project_id = $1 AND a.name = $2"
)

# Set form for ingress and the search filter, which cannot ask one name at a time. Both columns are labelled `alias` and `canonical` because asyncpg keys rows by column name and both source columns are `name`.
DOMAIN_ALIASES_SQL = (
    "SELECT a.name AS alias, d.name AS canonical"
    "  FROM domain_aliases da"
    "  JOIN aliases a ON a.id = da.alias_id"
    "  JOIN project_domains d ON d.id = da.domain_id"
    " WHERE da.active AND da.project_id = $1"
)

DOMAIN_REGISTER_SQL = (
    "INSERT INTO project_domains (project_id, name, created_by)"
    " VALUES ($1, $2, $3)"
    " ON CONFLICT (project_id, name) DO NOTHING"
    " RETURNING id"
)


def domain_merge_cypher(var: str = "d", id_param: str = "$domain_id") -> str:
    """MERGE :Domain on registry id only; no name-keyed fallback, because the same section name in two projects must stay two nodes (migration 028)."""
    return f"MERGE ({var}:{ONT.domain} {{domain_id: {id_param}}})"
