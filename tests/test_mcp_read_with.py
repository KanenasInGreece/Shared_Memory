"""F22: MCP read_with is the tool call, rebuilt from full_record.ref."""
import importlib.util
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("fastmcp")


def _load():
    path = os.path.join(os.path.dirname(__file__), "..", "mcp", "vector-skill.py")
    spec = importlib.util.spec_from_file_location("vector_skill_read_with", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["vector_skill_read_with"] = module
    spec.loader.exec_module(module)
    return module


vs = _load()

_ENTRY = {
    "ref": "decision:7",
    "full_record": {"ref": "decision:7", "chars": 12, "read_with": "search --ref decision:7"},
}


def _ok(body):
    return MagicMock(status_code=200, json=lambda: json.loads(json.dumps(body)))


async def _quiet():
    return None


@pytest.mark.asyncio
async def test_f22_mcp_read_with_is_the_tool_call_on_every_read():
    """F22: read_with on the MCP door is not the tool call, including a warning wrapper."""
    ranked = {"results": [{**_ENTRY, "ranked": False}], "fallback": "keyword"}
    by_ref = {"results": [dict(_ENTRY)]}
    lineage = {"pg_id": 7, "related": [{"record": dict(_ENTRY)}]}
    graph = {"records": [{"n": dict(_ENTRY)}]}

    async def _post(url, **kwargs):
        if url.endswith("/memory/search"):
            body = kwargs.get("json") or {}
            return _ok(by_ref if body.get("refs") else ranked)
        if url.endswith("/memory/graph"):
            return _ok(graph)
        return _ok({})

    async def _get(url, **kwargs):
        return _ok(lineage)

    with patch.object(vs, "_gateway_capability", _quiet), \
         patch.object(vs, "_gateway_capacity", _quiet), \
         patch("httpx.AsyncClient.post", side_effect=_post), \
         patch("httpx.AsyncClient.get", side_effect=_get):
        ranked_body = json.loads(await vs.hybrid_search_and_rerank("query"))
        by_ref_body = json.loads(await vs.hybrid_search_and_rerank(refs=["decision:7"]))
        lineage_body = json.loads(await vs.record_lineage("decision:7"))
        graph_body = json.loads(await vs.graph_query("MATCH (n) RETURN n"))

    expect = 'hybrid_search_and_rerank(refs=["decision:7"])'
    assert ranked_body["warnings"]
    assert ranked_body["results"][0]["full_record"]["read_with"] == expect
    assert by_ref_body[0]["full_record"]["read_with"] == expect
    assert lineage_body["related"][0]["record"]["full_record"]["read_with"] == expect
    assert graph_body[0]["n"]["full_record"]["read_with"] == expect
    assert "search --ref" not in json.dumps(ranked_body)
