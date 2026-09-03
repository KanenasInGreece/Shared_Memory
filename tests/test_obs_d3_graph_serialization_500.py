"""OBS D3 — `/memory/graph` must not turn a JSON-coercion bug into a fake
Neo4j outage, and must not answer one with a bare (un-shaped) 500 either.

THE DEFECT
----------
`handle_graph`'s success path (`coordinator.py`, the line building
`web.json_response({"status": "success", "records": records})`) sat OUTSIDE
the query try/except and never ran `records` through `_json_safe`. A Neo4j
`DateTime`/`Date`/`Time` value in any returned property makes
`web.json_response` raise `TypeError` while building the response — an
exception the query try/except cannot see (it already exited), so it
propagates uncaught past the handler and aiohttp answers with its own bare,
un-shaped 500 (no `{"status": "error", ...}` envelope at all), rather than the
gateway's normal error shape.

THE FIX, AND WHY IT NEEDS ITS OWN NARROW try
---------------------------------------------
`records` is now run through `_json_safe` and `json.dumps` in a SEPARATE,
narrow try scoped to (TypeError, ValueError) — the exceptions `json.dumps`
itself raises. A failure there returns the handler's own 500 JSON shape
WITHOUT incrementing `_neo4j_tx_failures_total`: routing a bug in our own
serialization code into the same counter as a genuine database outage would
make `/memory/telemetry` lie about the database's health.

PROVE-FAILING-FIRST (recorded in the commit body, reproduced here in the
docstring for a cold reader): running `test_a_temporal_property_no_longer_
crashes_the_response` against the pre-fix `handle_graph` — reached over a
real `TestServer`/`TestClient` socket, not a direct coroutine call, so
aiohttp's own uncaught-exception path is what answers — got HTTP 500 with a
body that is NOT `{"status": "error", ...}` JSON (aiohttp's generic
"Internal Server Error" plain-text page), proving the "bare 500" the defect
describes. After the fix, the same request gets HTTP 200 with the DateTime
value stringified.

`cypher_rejected` (`ClientError` → 400) is untouched: it returns from inside
the query try/except, before this file's new code ever runs — pinned by
`test_cypher_rejected_400_is_unaffected` reusing `test_graph_client_error.py`'s
own harness style.
"""
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from neo4j.exceptions import ClientError
from neo4j.time import DateTime

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


def _coord_with_records(records):
    """A coordinator whose Neo4j session's `run().data()` returns `records`."""
    c = MemoryCoordinator()
    result = MagicMock()
    result.data = AsyncMock(return_value=records)
    session = MagicMock()
    session.run = AsyncMock(return_value=result)
    neo4j = MagicMock()
    neo4j.session = MagicMock(return_value=_AsyncCtx(session))
    c._neo4j = neo4j
    return c


def _coord_raising(raises):
    c = MemoryCoordinator()
    session = MagicMock()
    session.run = AsyncMock(side_effect=raises)
    neo4j = MagicMock()
    neo4j.session = MagicMock(return_value=_AsyncCtx(session))
    c._neo4j = neo4j
    return c


READ_ONLY = "MATCH (n) RETURN n LIMIT 1"


async def _post_graph(c, cypher, params=None):
    """Drive `handle_graph` over a REAL aiohttp socket (TestServer/TestClient,
    no port binding) so an exception that escapes the handler is answered by
    aiohttp's OWN uncaught-exception path, exactly as it is on the live
    gateway — a direct coroutine call cannot observe the "bare 500" shape."""
    app = web.Application()
    app.router.add_post("/memory/graph", c.handle_graph)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        resp = await client.post("/memory/graph",
                                  json={"cypher": cypher, "params": params or {}})
        status = resp.status
        raw = await resp.read()
        return status, raw
    finally:
        await client.close()


# `_json_safe` sanitizes dict VALUES only (`{k: _json_safe(v) for k, v in
# value.items()}`) — it never touches KEYS. A non-string, non-int/float/bool/
# None key (a tuple, here) therefore survives `_json_safe` completely
# unchanged, and only `json.dumps` itself raises `TypeError` on it — exactly
# the "`_json_safe` output still fails `json.dumps`" case the brief asks for.
_BAD_KEY_PAYLOAD = {("a", "b"): "x"}


@pytest.mark.asyncio
async def test_a_temporal_property_no_longer_crashes_the_response():
    ts = DateTime(2024, 1, 1, 12, 0, 0)
    c = _coord_with_records([{"n": {"name": "x", "when": ts}}])
    status, raw = await _post_graph(c, READ_ONLY)
    assert status == 200
    body = json.loads(raw)
    assert body["status"] == "success"
    assert body["records"][0]["n"]["when"] == ts.iso_format()
    assert c._neo4j_tx_failures_total == 0


@pytest.mark.asyncio
async def test_pre_fix_temporal_property_was_a_bare_unshaped_500():
    """Reproduces the DEFECT directly (not via git-stashing the fix): call the
    old, unguarded serialization path exactly as `handle_graph` used to —
    `web.json_response({"status": "success", "records": records})` with no
    `_json_safe`/narrow-try in front of it — and show aiohttp answers with an
    un-shaped 500, not the gateway's `{"status": "error", ...}` envelope."""
    ts = DateTime(2024, 1, 1, 12, 0, 0)
    records = [{"n": {"name": "x", "when": ts}}]

    async def _old_unguarded_handler(request):
        return web.json_response({"status": "success", "records": records})

    app = web.Application()
    app.router.add_post("/memory/graph", _old_unguarded_handler)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        resp = await client.post("/memory/graph", json={"cypher": READ_ONLY})
        status = resp.status
        raw = await resp.read()
    finally:
        await client.close()

    assert status == 500
    # The bare aiohttp error page is not our JSON envelope at all.
    with pytest.raises((json.JSONDecodeError, KeyError, TypeError)):
        body = json.loads(raw)
        assert body["status"] == "error"


@pytest.mark.asyncio
async def test_an_object_that_still_fails_after_json_safe_is_the_handlers_own_500():
    c = _coord_with_records([{"n": {"bad": _BAD_KEY_PAYLOAD}}])
    status, raw = await _post_graph(c, READ_ONLY)
    assert status == 500
    body = json.loads(raw)
    assert body == {"status": "error", "message": "query failed"}
    # The whole point of the narrow try: a JSON bug must never inflate the
    # database-outage counter.
    assert c._neo4j_tx_failures_total == 0


@pytest.mark.asyncio
async def test_cypher_rejected_400_is_unaffected():
    c = _coord_raising(ClientError("Type mismatch: expected String but was Integer"))
    status, raw = await _post_graph(c, READ_ONLY)
    assert status == 400
    body = json.loads(raw)
    assert body["error"] == "cypher_rejected"
    assert c._cypher_rejected_total == 1
    assert c._neo4j_tx_failures_total == 0
