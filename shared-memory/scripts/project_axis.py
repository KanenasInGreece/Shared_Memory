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
