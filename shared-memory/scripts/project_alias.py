"""Alias resolution — a rename, remembered, and resolved in ONE lookup.

``normalize_projects.py`` moves records from an old spelling to a new one. That
fixes history and does nothing about the source: a folder on another machine
still carries the old name, so the next save recreates the variant. And with the
judgement recorded nowhere, the old name reads as an unregistered stranger to
every later review, so a decision made once returns to the operator forever.

An alias fixes both. The old name resolves to the current one AT INGRESS, so an
unreachable machine — or a folder nobody wants to rename — stops mattering, and
the decision is a row rather than a memory.

⚠ A3 — RESOLUTION IS ONE HOP, AND THAT IS A DESIGN CHOICE, NOT AN OPTIMISATION.
Chains are real here: one project has already been spelled three ways across two
machines. If resolution followed alias→alias links it would be a graph walk on
the ingress path, and a walk can cycle — two renames that eventually point at
each other would hang the save that triggered them, or loop until something
gives. So the chain is collapsed at WRITE time instead: renaming a project
re-points every alias already aimed at it and demotes the old canonical to an
alias, all in one transaction, so every alias always points DIRECTLY at a
canonical name. Reads stay a single indexed lookup, forever, and the chain is
still legible in the superseded rows.

SERVER-SIDE ONLY — never shipped in a skill. Clients send what their folder is
called; deciding what that means is the gateway's job.
"""

# Resolve one name. `{p}` is the caller's placeholder style — asyncpg uses $1,
# psycopg2 uses %s — matching PROJECT_MATCH_SQL's convention rather than picking
# a driver for every future caller.
#
# ⚠ THE MAPPING IS TO AN IDENTITY, NOT TO A NAME (migration 027). The junction
# stores ``project_id``; the CURRENT name is read back through the registry on
# every resolution. That is what makes an alias row stay true across a rename
# with no maintenance: the row records which project a retired spelling meant,
# and what that project is called today is a question only the registry answers.
ALIAS_RESOLVE_SQL = (
    "SELECT p.name"
    " FROM project_aliases pa"
    " JOIN aliases a ON a.id = pa.alias_id"
    " JOIN projects p ON p.id = pa.project_id"
    " WHERE a.name = {p} AND pa.active"
)

# Every active alias, for the tools that must not re-ask a settled question.
ACTIVE_ALIASES_SQL = (
    "SELECT a.name, p.name"
    " FROM project_aliases pa"
    " JOIN aliases a ON a.id = pa.alias_id"
    " JOIN projects p ON p.id = pa.project_id"
    " WHERE pa.active"
)

# Intern the string, returning its id whether or not it already existed. The
# no-op UPDATE is deliberate: a bare DO NOTHING returns no row, so the caller
# would need a second round trip for a name that already exists.
ALIAS_UPSERT_SQL = (
    "INSERT INTO aliases (name) VALUES ({p})"
    " ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name"
    " RETURNING id"
)


def alias_refusal(alias: str, canonical: str, registered: set) -> str | None:
    """Why this alias must not be created — or ``None`` when it may.

    A pure predicate over the three ways an alias can be wrong, so each one can
    be asserted directly rather than inferred from a database error message. The
    database enforces all three as well; this exists so the caller can refuse
    early with something a human can read.
    """
    if not isinstance(alias, str) or not alias.strip():
        return "alias is empty"
    if not isinstance(canonical, str) or not canonical.strip():
        return "canonical project is empty"
    alias, canonical = alias.strip(), canonical.strip()
    if alias == canonical:
        return f"{alias!r} cannot be an alias of itself"
    # A1 — the namespaces are disjoint. A string that is both a registered
    # project and an alias for a DIFFERENT one has two correct answers.
    if alias in registered:
        return (
            f"{alias!r} is a registered project; aliasing it would give one "
            f"string two meanings. Merge the records first, then alias the name "
            f"as part of retiring it"
        )
    if canonical not in registered:
        return f"{canonical!r} is not a registered project"
    return None


def canonical_or_self(name, aliases: dict):
    """The canonical name for ``name``, or ``name`` unchanged.

    ``aliases`` maps alias → canonical, already one-hop by construction (A3).
    Deliberately NOT recursive: if this ever needs to follow a second hop, the
    write path has failed to collapse a chain and the right fix is there, not a
    loop here that would paper over it and could cycle.
    """
    if not isinstance(name, str):
        return name
    return aliases.get(name.strip(), name)
