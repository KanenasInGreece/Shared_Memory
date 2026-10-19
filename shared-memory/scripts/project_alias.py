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
canonical name. Reads stay a single indexed lookup, forever. The re-point
overwrites the active row's target in place: nothing supersedes an alias row,
though decision:1015 (A4: mappings superseded, never deleted) asks for it.

SERVER-SIDE ONLY — never shipped in a skill. Clients send what their folder is
called; deciding what that means is the gateway's job.
"""

# `{p}` is the caller's placeholder, same as PROJECT_MATCH_SQL. Exact match on the stored
# spelling, at most one row (decision:1015 A2: one active mapping per alias). The name is
# read through project_id (migration 027), so the row never holds a stale project name.
ALIAS_RESOLVE_SQL = (
    "SELECT p.name"
    " FROM project_aliases pa"
    " JOIN aliases a ON a.id = pa.alias_id"
    " JOIN projects p ON p.id = pa.project_id"
    " WHERE a.name = {p} AND pa.active"
)

# Both columns are labelled. They are both `name` in their tables, and asyncpg keeps one of two identical keys, so the map would alias a name to itself.
ACTIVE_ALIASES_SQL = (
    "SELECT a.name AS alias, p.name AS canonical"
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
