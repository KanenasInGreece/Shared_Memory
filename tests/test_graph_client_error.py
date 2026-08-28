"""`/memory/graph` — a Cypher the DATABASE refuses is the CALLER's error (400).

`handle_graph` turned every exception into `500 {"message": "query failed"}`.
That put two unrelated events in one bucket: a gateway or Neo4j that is
genuinely down, and a query with a syntax error, an unknown function or a type
error in it. The second is not a server fault, and answering it with a 5xx tells
the caller the one thing that will never help — retry — while burying a real
outage in the same signal.

Live-reproduced (`decision:1756` (6)) with `coalesce(x.name, x.pg_id)` over
nodes whose `name` is a string and whose `pg_id` is an integer: Neo4j raises
`ClientError`, and the gateway reported an outage.

The discriminator is the driver's own exception class, never a string match on
the message: `neo4j.exceptions.ClientError` IS the driver's word for "you asked
for something invalid". `ServiceUnavailable` and everything else stay 500.

MUTATION CHECK (run, recorded in HANDOFF.md): delete the `except ClientError`
clause in `handle_graph` so every exception falls through to the generic
handler, and `test_a_cypher_the_database_refuses_is_a_400` +
`test_the_refusal_carries_the_drivers_own_message` die (both become 500) while
`test_an_unreachable_database_is_still_a_500` survives — which is what proves
the two paths are actually distinguished rather than both being answered by the
same clause.
"""
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from neo4j.exceptions import ClientError, ServiceUnavailable

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

from coordinator import MemoryCoordinator  # noqa: E402


class _AsyncCtx:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


def _coord(raises):
    """A coordinator whose Neo4j session raises `raises` from `run`."""
    c = MemoryCoordinator()
    session = MagicMock()
    session.run = AsyncMock(side_effect=raises)
    neo4j = MagicMock()
    neo4j.session = MagicMock(return_value=_AsyncCtx(session))
    c._neo4j = neo4j
    return c


def _request(cypher):
    req = MagicMock()
    req.json = AsyncMock(return_value={"cypher": cypher})
    return req


# A read-only query — it must reach the driver rather than being stopped by the
# write-keyword guard, or this file would prove nothing about the driver's own
# refusals.
READ_ONLY = "MATCH (n) RETURN coalesce(n.name, n.pg_id) LIMIT 1"


@pytest.mark.asyncio
async def test_a_cypher_the_database_refuses_is_a_400():
    c = _coord(ClientError("Type mismatch: expected String but was Integer"))
    resp = await c.handle_graph(_request(READ_ONLY))
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["status"] == "error"
    assert body["error"] == "cypher_rejected"


@pytest.mark.asyncio
async def test_the_refusal_carries_the_drivers_own_message():
    """The message names the offending clause, which is the entire value of the
    reply: without it the caller is told "rejected" and has to guess what. It is
    capped rather than dropped — and nothing in it comes from the database's
    CONTENTS, only from the query the caller just sent."""
    c = _coord(ClientError("Unknown function 'coalsece'"))
    resp = await c.handle_graph(_request(READ_ONLY))
    body = json.loads(resp.body)
    assert "coalsece" in body["message"]
    assert len(body["message"]) <= 300


@pytest.mark.asyncio
async def test_a_very_long_driver_message_is_capped():
    c = _coord(ClientError("x" * 5000))
    resp = await c.handle_graph(_request(READ_ONLY))
    assert len(json.loads(resp.body)["message"]) == 300


@pytest.mark.asyncio
async def test_an_unreachable_database_is_still_a_500():
    """The half that must NOT move. A database that cannot be reached is the
    server's problem, the caller's query is fine, and retrying is the right
    thing to do — so it keeps the 5xx and the opaque message."""
    c = _coord(ServiceUnavailable("connection refused"))
    resp = await c.handle_graph(_request(READ_ONLY))
    assert resp.status == 500
    assert json.loads(resp.body)["message"] == "query failed"


@pytest.mark.asyncio
async def test_an_unexpected_exception_is_still_a_500():
    c = _coord(RuntimeError("something else entirely"))
    resp = await c.handle_graph(_request(READ_ONLY))
    assert resp.status == 500
    assert json.loads(resp.body)["message"] == "query failed"


@pytest.mark.asyncio
async def test_the_write_guard_still_answers_before_the_driver():
    """The keyword guard is untouched and still fires FIRST: a write never
    reaches the session, so it can never be reported as `cypher_rejected` — a
    refusal that reads as "fix your syntax" would be actively misleading about
    a query that is refused on policy."""
    c = _coord(ClientError("never reached"))
    resp = await c.handle_graph(_request("MATCH (n) SET n.x = 1 RETURN n"))
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body.get("error") != "cypher_rejected"
    c._neo4j.session.assert_not_called()
