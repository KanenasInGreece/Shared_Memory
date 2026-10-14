"""Ingress hardening: POST /memory/save is not a retrospective route, and a
fact cannot carry a decision object.

The dedicated POST /memory/retrospective handler is unchanged. A decision,
including a differently cased type, still saves through handle_save.
"""
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO = Path(__file__).parent.parent
_SCRIPTS = _REPO / "shared-memory" / "scripts"

_USE_RETRO = (
    "A retrospective cannot be saved via POST /memory/save; "
    "use POST /memory/retrospective (or the save_retrospective tool) "
    "against the target decision."
)
_FACT_DECISION = (
    "A fact cannot carry a 'decision' object in its metadata. "
    "Facts and decisions are distinct record types. To save a "
    "decision, set metadata.type = 'decision' and include required "
    "decision fields."
)

_STATE: dict = {}


def _load(name: str, path: Path):
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module", autouse=True)
def _gateway():
    saved_path = list(sys.path)
    had_apply = "apply" in sys.modules
    saved_apply = sys.modules.get("apply")
    _STATE["coordinator"] = _load("coordinator_ingress_hardening", _SCRIPTS / "coordinator.py")
    yield
    _STATE.clear()
    sys.path[:] = saved_path
    if had_apply:
        sys.modules["apply"] = saved_apply
    else:
        sys.modules.pop("apply", None)


class _ctx:
    def __init__(self, val):
        self._val = val

    async def __aenter__(self):
        return self._val

    async def __aexit__(self, *_):
        pass


def _request(body: dict) -> MagicMock:
    state = {"authenticated_agent": None, "principal": None}
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.rel_url.query.get = MagicMock(return_value=None)
    req.get = MagicMock(side_effect=lambda k, d=None: state.get(k, d))
    req.__getitem__ = MagicMock(side_effect=lambda k: state.get(k))
    return req


def _coordinator():
    c = _STATE["coordinator"].MemoryCoordinator()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": 99})
    conn.fetchval = AsyncMock(return_value=1)
    conn.execute = AsyncMock(return_value="DELETE 0")
    conn.fetch = AsyncMock(return_value=[])
    conn.transaction = MagicMock(return_value=_ctx(None))
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_ctx(conn))
    c._pool = pool
    session = AsyncMock()
    neo = MagicMock()
    neo.session = MagicMock(return_value=_ctx(session))
    c._neo4j = neo
    c._entity_vocab_resolve_many = AsyncMock(
        side_effect=lambda names: {n: n for n in names})
    c._resolve_typed_grounding = AsyncMock(return_value=[])
    c._promote_grounded_parked_facts = AsyncMock()
    return c, conn


def _save(metadata: dict, content: str = "a plain observation about widgets"):
    meta = {"source": "claude-code", "project": "widget_works", **metadata}
    return _request({"content": content, "metadata": meta})


async def _run(metadata: dict):
    c, conn = _coordinator()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        resp = await c.handle_save(_save(metadata))
    return resp, conn


def _decision(spelling: str) -> dict:
    return {
        "type": spelling,
        "decision": {
            "decided_by": "Operator",
            "project": "widget_works",
            "rationale": "a reason",
        },
    }


_POPULATED = {"decided_by": "Operator", "project": "widget_works", "rationale": "because"}


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["retrospective", "Retrospective", " RETROSPECTIVE "])
async def test_save_rejects_a_retrospective_in_any_casing(spelling):
    resp, conn = await _run({"type": spelling})
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["status"] == "error"
    assert body["error"] == "use_retrospective_endpoint"
    assert body["message"] == _USE_RETRO
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("type_value", [None, "fact", "Fact"])
@pytest.mark.parametrize("blob", [_POPULATED, {}])
async def test_save_rejects_a_fact_that_carries_a_decision_object(type_value, blob):
    metadata = {"decision": blob}
    if type_value is not None:
        metadata["type"] = type_value
    resp, conn = await _run(metadata)
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["status"] == "error"
    assert body["error"] == "fact_cannot_carry_decision"
    assert body["message"] == _FACT_DECISION
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [
    {},
    {"type": "fact"},
    {"type": "Fact"},
    {"decision": None},
])
async def test_save_accepts_a_legitimate_fact(metadata):
    resp, conn = await _run(metadata)
    assert resp.status == 200, resp.text
    assert json.loads(resp.text)["status"] == "success"
    conn.fetchrow.assert_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["decision", "Decision", "DECISION"])
async def test_save_accepts_a_legitimate_decision(spelling):
    resp, conn = await _run(_decision(spelling))
    assert resp.status == 200, resp.text
    assert json.loads(resp.text)["status"] == "success"
    stored = conn.fetchrow.await_args.args[2]
    assert stored["type"] == "decision"


@pytest.mark.asyncio
async def test_the_retrospective_endpoint_still_accepts_a_verdict():
    c, conn = _coordinator()
    conn.fetchrow = AsyncMock(side_effect=[
        {"id": 240, "type": "decision", "project": "widget_works"},
        {"id": 911},
    ])
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        resp = await c.handle_retrospective(_request({
            "pg_id": 240,
            "rating": "validated",
            "notes": "held up",
            "grounded_in": [700],
        }))
    assert resp.status == 200, resp.text
    body = json.loads(resp.text)
    assert body["status"] == "success"
    assert body["target_pg_id"] == 240
    assert body["pg_id"] == 911
