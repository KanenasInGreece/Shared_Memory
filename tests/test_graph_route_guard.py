"""Item 7 of the v0.9.69 post-first-write hardening plan — the `/memory/graph`
read-only keyword guard.

Invariant **G1**: every write keyword is refused regardless of the whitespace
that surrounds it.

The guard used to spell the SET clause `SET\\s` — a keyword plus ONE
whitespace character — inside an alternation closed by `\\b`. For
`SET  n:Label` (two spaces) `SET\\s` consumed the first space and the closing
`\\b` then had to hold between two spaces, which it never does: the query
passed the guard and was refused only by the Neo4j session's
`default_access_mode="READ"`. Live-reproduced, `fact:1734` (C, guard).

Everything here is table-driven over the module-level `_WRITE_CYPHER` regex,
plus one end-to-end pass through `handle_graph` proving the guard is the thing
that produces the 400 and that the Neo4j driver is never reached.

Mutation check (RUN, recorded in HANDOFF.md): restoring `SET\\s` in
`coordinator.py` makes exactly six cases fail —
`test_write_keyword_is_refused[set-double-space]`,
`[set-trailing]`, the three `test_known_over_block[*set*]` cases and
`test_handle_graph_refuses_double_space_set_before_neo4j` (which then reaches
the mocked driver, proving the guard was the thing that stopped it). The
`set-newline` and `set-tab` cases survive the mutation — `\\s` matches those
two characters, which is exactly why the bypass needed a DOUBLE space to be
found; they are kept as boundary coverage, not as the killing cases.
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

from coordinator import _WRITE_CYPHER, MemoryCoordinator  # noqa: E402


# ── Queries that MUST be refused ──────────────────────────────────────────────
# id → cypher. The `set-*` whitespace family is the regression this file exists
# for; the rest pin the other keywords so a future rewrite cannot drop one.
REFUSED = {
    "set-single-space":   "MATCH (n) SET n.x = 1 RETURN n",
    "set-double-space":   "MATCH (n) SET  n:Label RETURN n",
    "set-newline":        "MATCH (n)\nSET n.x = 1\nRETURN n",
    "set-tab":            "MATCH (n)\tSET\tn.x = 1 RETURN n",
    "set-lowercase":      "match (n) set n.x = 1 return n",
    "set-mixed-case":     "MATCH (n) SeT n.x = 1 RETURN n",
    "set-trailing":       "MATCH (n) RETURN n // SET",
    "create":             "CREATE (n:Thing {name: 'x'})",
    "create-lowercase":   "create (n:Thing)",
    "delete":             "MATCH (n) DELETE n",
    "detach-delete":      "MATCH (n) DETACH DELETE n",
    "detach-delete-2sp":  "MATCH (n) DETACH  DELETE n",
    "remove":             "MATCH (n) REMOVE n:Label",
    "remove-double-sp":   "MATCH (n) REMOVE  n:Label",
    "merge":              "MERGE (n:Thing {name: 'x'})",
    "call":               "CALL db.labels()",
    "call-double-space":  "CALL  db.labels()",
    "load-csv":           "LOAD CSV FROM 'file:///x.csv' AS row RETURN row",
    "load-csv-2sp":       "LOAD  CSV FROM 'file:///x.csv' AS row RETURN row",
    "drop":               "DROP INDEX idx_thing",
}

# Known, ACCEPTED over-blocks: a read-only guard errs towards refusal. These are
# legitimate read queries the keyword guard nonetheless rejects. Pinned so the
# cost is visible and a later relaxation is a deliberate change, not a drift.
KNOWN_OVER_BLOCKS = {
    "bare-property-set":  "MATCH (n) RETURN n.set",
    "alias-set":          "MATCH (n) RETURN n.x AS set",
    "string-literal-set": "MATCH (n) WHERE n.name = 'SET' RETURN n",
    "string-literal-del": "MATCH (n) WHERE n.note = 'DELETE me' RETURN n",
}

# Queries that MUST pass — the property names the old `SET\s` spelling was
# written to protect. `\bSET\b` cannot match inside a longer word, so they are
# safe under the boundary form too.
ALLOWED = {
    "settings-property":  "MATCH (n) RETURN n.settings",
    "asset-property":     "MATCH (n) RETURN n.asset",
    "assets-label":       "MATCH (n:Asset) RETURN n.assets",
    "onset-property":     "MATCH (n) WHERE n.onset > 0 RETURN n",
    "created-at":         "MATCH (n) RETURN n.created_at ORDER BY n.created_at",
    "plain-match":        "MATCH (n:Entity) RETURN n LIMIT 10",
    "optional-match":     "MATCH (a) OPTIONAL MATCH (a)-[r]->(b) RETURN a, b",
    "with-where":         "MATCH (n) WITH n WHERE n.pg_id IS NOT NULL RETURN n",
    "dropped-word":       "MATCH (n) WHERE n.note = 'dropped' RETURN n",
    "merged-word":        "MATCH (n) WHERE n.state = 'merged' RETURN n",
    "recall-word":        "MATCH (n) WHERE n.note = 'recall' RETURN n",
    "created-word":       "MATCH (n) WHERE n.note = 'created' RETURN n",
    "deleted-word":       "MATCH (n) WHERE n.note = 'deleted' RETURN n",
}


@pytest.mark.parametrize("cypher", list(REFUSED.values()), ids=list(REFUSED))
def test_write_keyword_is_refused(cypher):
    """G1: a mutating keyword is caught whatever whitespace follows it."""
    assert _WRITE_CYPHER.search(cypher) is not None


@pytest.mark.parametrize("cypher", list(KNOWN_OVER_BLOCKS.values()),
                         ids=list(KNOWN_OVER_BLOCKS))
def test_known_over_block(cypher):
    """Accepted cost of a keyword guard: these reads are refused too."""
    assert _WRITE_CYPHER.search(cypher) is not None


@pytest.mark.parametrize("cypher", list(ALLOWED.values()), ids=list(ALLOWED))
def test_read_query_is_allowed(cypher):
    """A word that merely CONTAINS a keyword is not a keyword."""
    assert _WRITE_CYPHER.search(cypher) is None


def _coordinator():
    coord = MemoryCoordinator.__new__(MemoryCoordinator)
    coord._neo4j = MagicMock()
    return coord


def _request(body):
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    return req


@pytest.mark.asyncio
async def test_handle_graph_refuses_double_space_set_before_neo4j():
    """End-to-end: the double-space bypass is now a 400 from the GUARD, and the
    Neo4j session is never opened (previously it opened and the write was
    refused one layer down, by READ access mode)."""
    coord = _coordinator()
    resp = await coord.handle_graph(_request({"cypher": "MATCH (n) SET  n:Label RETURN n"}))
    assert resp.status == 400
    coord._neo4j.session.assert_not_called()


@pytest.mark.asyncio
async def test_handle_graph_still_opens_a_read_session_for_a_read_query():
    """The guard does not block ordinary reads, and the session is still
    opened READ-only (the second layer stays in place)."""
    coord = _coordinator()
    session = MagicMock()
    session.run = AsyncMock(return_value=MagicMock(data=AsyncMock(return_value=[{"n": 1}])))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    coord._neo4j.session = MagicMock(return_value=ctx)

    resp = await coord.handle_graph(_request({"cypher": "MATCH (n) RETURN n.settings"}))
    assert resp.status == 200
    coord._neo4j.session.assert_called_once_with(default_access_mode="READ")
