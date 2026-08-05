"""One domain resolution, shared by every reader — the project axis' sibling.

A DOMAIN IS A SECTION OF ONE PROJECT. That single sentence decides everything
here and is the reason this is not a copy of ``project_axis`` with the words
changed:

* **It is project-local.** ``operations`` under one project and ``operations``
  under another are different sections that share a word. So every lookup takes
  a project id, and a domain is never resolvable from its name alone.
* **It is multi-valued.** A record belongs to exactly one project and may sit in
  several of its sections. First write links to all of them.
* **It is INHERITED by judgements, never self-named** (P17). A decision or a
  retrospective takes the union of its grounding facts' domains; a client that
  supplies one is refused, because a field that is silently dropped is a field
  the caller will send forever.
* **It gates nothing yet.** The project axis decides what folds; the domain axis
  is capture and representation until the fold behaviour moves onto it. That is
  why a missing identity here costs an edge rather than being rescued by a
  name-keyed fallback — see ``domain_merge_cypher``.

SERVER-SIDE ONLY. Never added to ``sync_skills.sh`` or ``shared-memory-skill/``
— the skill is a thin HTTP client and resolution happens at ingress.
"""

import os

from ontology import ONT

# Where a domain may be WRITTEN, and under which spellings. `domain` is the
# field this corpus has always used; `domains` is accepted because the value is
# a LIST now, and a caller reaching for the plural is describing the same thing
# rather than making a mistake. Both are read on facts and both are refused on
# judgements — a rule that recognised only one spelling would refuse the
# careless caller and silently accept the careful one.
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


# ── The registry (migration 028) ─────────────────────────────────────────────
# Every statement takes a project id. There is no by-name-alone lookup in this
# module and that absence is load-bearing: the one way this axis reproduces the
# project axis' original defect is by letting a name answer on its own.

DOMAIN_EXISTS_SQL = (
    "SELECT id FROM project_domains WHERE project_id = $1 AND name = $2"
)

# Proposals for a value that missed. TRIGRAM over the name, UNION'd with a
# description match, both scoped to the project.
#
# ⚠ THE DESCRIPTION HALF IS NOT DECORATION, and it is the one real difference
# from the project version. Project names are short and typo-shaped, so a name
# similarity carries them. Section names are ordinary words chosen by different
# people at different times: an operator typing `crypto` should reach a section
# called `security` whose description mentions key handling, and no name
# similarity will ever connect those two words. Descriptions are what make a
# registry legible to someone who did not write it.
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

# Same floor as the project axis, and the same reasoning: a rejected save is a
# dead end unless the proposals are usable.
DOMAIN_PROPOSAL_SIMILARITY = 0.25
DOMAIN_PROPOSAL_LIMIT = 5

# Above this trigram similarity a proposed NEW section is confusable with one
# the project already has, and must be confirmed as deliberately distinct. Same
# default and the same override reasoning as the project floor, kept as its own
# name so a deployment can tune the two independently: sections are named more
# loosely than projects, so this is the floor more likely to need moving.
DOMAIN_CONFUSABLE_SIMILARITY = float(
    os.environ.get("DOMAIN_CONFUSABLE_SIMILARITY", "0.6")
)

DOMAIN_CONFUSABLE_SQL = (
    "SELECT name FROM project_domains"
    " WHERE project_id = $1 AND similarity(name, $2) >= $3 AND name <> $2"
    " ORDER BY similarity(name, $2) DESC, name LIMIT $4"
)

# A retired spelling resolves to the section that replaced it — WITHIN its
# project, which is why this join goes through `domain_aliases.project_id`
# rather than trusting the alias string to be unique on its own.
DOMAIN_ALIAS_RESOLVE_SQL = (
    "SELECT d.name FROM domain_aliases da"
    "  JOIN aliases a ON a.id = da.alias_id"
    "  JOIN project_domains d ON d.id = da.domain_id"
    " WHERE da.active AND da.project_id = $1 AND a.name = $2"
)

DOMAIN_REGISTER_SQL = (
    "INSERT INTO project_domains (project_id, name, created_by)"
    " VALUES ($1, $2, $3)"
    " ON CONFLICT (project_id, name) DO NOTHING"
    " RETURNING id"
)


def domain_merge_cypher(var: str = "d", id_param: str = "$domain_id") -> str:
    """The MERGE that puts a domain node in the graph (migration 028).

    ⛔ THERE IS NO NAME-KEYED FALLBACK, and the asymmetry with
    ``project_merge_cypher`` is deliberate rather than an omission.

    The project write path falls back to keying on the name when no identity is
    available, because losing a ``PROJECT_OF`` edge violates the axis outright
    and that axis already gates folding — a lost write there is worse than a
    node keyed on something mutable.

    Nothing gates on the domain axis yet, the value stays verbatim in the
    record's Postgres metadata either way, and ``backfill_domain_of.py`` can
    enqueue the edge later. So the honest answer to "no identity" is NO EDGE and
    a log line. Minting a name-keyed ``:Domain`` would re-ship, on a brand-new
    axis, the exact identity defect migration 027 was written to remove — and it
    would do it silently, because a name-keyed node looks correct until two
    projects use the same section name.

    The caller is responsible for having an id: ingress refuses an unregistered
    domain, so by the time a write happens the registry has answered.
    """
    return f"MERGE ({var}:{ONT.domain} {{domain_id: {id_param}}})"
