"""
alias_graph.py — alias-component helpers (ADR-017 "A").

Soft `ALIASES` edges (Entity↔Entity, REM-written, LLM-gated, never merge nodes)
form alias *components*. Grouping uses Neo4j GDS `gds.wcc` to stamp a stable
`Entity.alias_component`; NREM/search read that property to treat all surface
forms of one concept as a single unit.

No-op-safe: with zero `ALIASES` edges there are no components — `alias_component`
stays null and every consumer falls back to the entity itself, so behaviour is
identical to the exact-name graph until REM starts writing edges.

Grouping key (used by consumers): `coalesce(e.alias_component, elementId(e))`.
"""
import logging
from ontology import ONT

log = logging.getLogger("alias_graph")

_GDS_GRAPH = "aliasComponents"          # transient GDS projection name


def alias_edges_exist(session) -> bool:
    """True iff at least one ALIASES edge exists (so the rel type is in schema
    and a GDS projection over it is valid). Checks the schema's relationship-type
    registry first to avoid the benign 'type does not exist' warning a bare MATCH
    emits before the first alias edge is ever written."""
    types = session.run(
        "CALL db.relationshipTypes() YIELD relationshipType "
        "RETURN collect(relationshipType) AS types"
    ).single()["types"]
    if ONT.aliases not in types:
        return False
    rec = session.run(
        f"MATCH ()-[r:{ONT.aliases}]-() RETURN count(r) > 0 AS any"
    ).single()
    return bool(rec and rec["any"])


def ensure_index(session) -> None:
    """Range index on Entity.alias_component so component lookups stay cheap."""
    session.run(
        f"CREATE INDEX entity_alias_component IF NOT EXISTS "
        f"FOR (e:{ONT.entity}) ON (e.alias_component)"
    ).consume()


def refresh_components(session) -> int:
    """Recompute alias components and stamp `Entity.alias_component` via gds.wcc.

    Call after REM writes/changes ALIASES edges. No-op (returns 0) when no alias
    edges exist — GDS can't project a relationship type that isn't in the schema,
    and consumers already fall back to elementId. Returns #entities stamped.
    """
    if not alias_edges_exist(session):
        return 0
    ensure_index(session)
    # Drop any stale projection from a crashed prior run.
    session.run(
        "CALL gds.graph.exists($g) YIELD exists "
        "WITH exists WHERE exists "
        "CALL gds.graph.drop($g) YIELD graphName RETURN graphName",
        g=_GDS_GRAPH,
    ).consume()
    # Project all entities + ALIASES as UNDIRECTED (synonymy is symmetric).
    session.run(
        "CALL gds.graph.project($g, $label, "
        "{rel: {type: $rel, orientation: 'UNDIRECTED'}})",
        g=_GDS_GRAPH, label=ONT.entity, rel=ONT.aliases,
    ).consume()
    try:
        rec = session.run(
            "CALL gds.wcc.write($g, {writeProperty: 'alias_component'}) "
            "YIELD nodePropertiesWritten RETURN nodePropertiesWritten AS n",
            g=_GDS_GRAPH,
        ).single()
        stamped = int(rec["n"]) if rec else 0
    finally:
        session.run(
            "CALL gds.graph.drop($g) YIELD graphName RETURN graphName", g=_GDS_GRAPH
        ).consume()
    log.info("alias components refreshed — %d entities stamped", stamped)
    return stamped
