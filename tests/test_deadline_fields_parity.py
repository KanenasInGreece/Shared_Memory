"""Both client doors send the same caller deadline and use it for transport."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys

import httpx
import pytest


@pytest.fixture
def clients(monkeypatch):
    monkeypatch.setenv("SECURE_ENV_FILE", "")
    monkeypatch.setenv("VECTOR_SKILL_ENV", "/dev/null")
    monkeypatch.setenv("COORDINATOR_UDS", "")
    root = Path(__file__).resolve().parents[1]
    modules = []
    for name, path in (
        ("bridge_deadline_parity", "shared-memory/scripts/memory_bridge.py"),
        ("mcp_deadline_parity", "mcp/vector-skill.py"),
    ):
        spec = importlib.util.spec_from_file_location(name, root / path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "_append_log", lambda *args, **kwargs: None)
        for key, value in {
            "COORDINATOR_BASE": "http://gateway.invalid:1",
            "AGENT_ID": "shared_agent",
            "SEARCH_TIMEOUT_S": 0,
            "SEARCH_TIMEOUT_FLOOR_S": 30,
            "SEARCH_TIMEOUT_MAX_S": 300,
            "SEARCH_TIMEOUT_FALLBACK_S": 120,
            "SEARCH_SAFETY_FACTOR": 1.5,
            "SEARCH_OVERHEAD_S": 15,
        }.items():
            monkeypatch.setattr(module, key, value)
        modules.append(module)
    return modules


@pytest.fixture(params=[200, 503])
def gateway(request, monkeypatch, clients):
    calls = []
    health = {
        "status": "ok" if request.param == 200 else "degraded",
        "api_version": clients[0].API_VERSION,
        "backend_capability": {
            "embedder": {"projected_full_payload_s": 80},
            "reranker": {"projected_full_payload_s": 40},
        },
    }

    async def get(self, url, **kwargs):
        assert url.endswith("/health")
        return httpx.Response(request.param, json=health)

    async def post(self, url, **kwargs):
        calls.append((url, kwargs["json"], self.timeout.read))
        return httpx.Response(200, json={"status": "success", "results": [], "pg_id": 1})

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    return calls


def test_search_and_save_deadlines_match(clients, gateway, monkeypatch, capsys):
    bridge, mcp = clients

    async def run():
        monkeypatch.setattr(sys, "argv", ["memory_bridge.py", "search", "deadline query"])
        await bridge.main()
        await mcp.hybrid_search_and_rerank("deadline query")
        metadata = json.dumps({"project": "test"})
        monkeypatch.setattr(sys, "argv", ["memory_bridge.py", "save", "fact", metadata])
        await bridge.main()
        assert json.loads(await mcp.save_artifact("fact", metadata))["status"] == "success"

    asyncio.run(run())
    capsys.readouterr()
    assert len(gateway) == 4
    cli_search, mcp_search, cli_save, mcp_save = gateway
    assert cli_search == mcp_search
    assert cli_search[0].endswith("/memory/search")
    assert cli_search[1]["wait_s"] == cli_search[2] == 195
    assert cli_search[1]["encoder_wait_s"] == 180
    assert cli_save == mcp_save
    assert cli_save[0].endswith("/memory/save")
    assert cli_save[1]["wait_s"] == 135
    assert cli_save[2] == 150  # wait_s plus the 15 s response margin, so the gateway's 503 can arrive
    assert "encoder_wait_s" not in cli_save[1]


@pytest.mark.parametrize("wait", [10, 15])
def test_nonpositive_encoder_budget_is_omitted(clients, gateway, monkeypatch, wait):
    async def run():
        for client in clients:
            monkeypatch.setattr(client, "SEARCH_TIMEOUT_S", wait)
            await client._search_payload("small budget")

    asyncio.run(run())
    assert gateway[0] == gateway[1]
    assert gateway[0][1]["wait_s"] == gateway[0][2] == wait
    assert "encoder_wait_s" not in gateway[0][1]


@pytest.mark.parametrize("projection, expected", [(1, 30), (400, 300), (None, 60), ("bad", 60)])
def test_save_projection_bounds_and_fallback_match(clients, gateway, monkeypatch, projection, expected):
    async def run():
        for client in clients:
            monkeypatch.setattr(client, "_CAPABILITY_CACHE", {
                "embedder": {"projected_full_payload_s": projection},
                "reranker": {"projected_full_payload_s": 900},
            })
            await client.save_artifact("fact", json.dumps({"project": "test"}))

    asyncio.run(run())
    assert gateway[0] == gateway[1]
    _, body, timeout = gateway[0]
    if projection in (None, "bad"):
        assert "wait_s" not in body
        assert timeout == expected
    else:
        assert body["wait_s"] == expected
        assert timeout == expected + 15
    assert "encoder_wait_s" not in body


def test_compat_readers_accept_degraded_health(clients, gateway):
    async def run():
        for client in clients:
            result = await client.check_gateway_compat()
            assert result["reachable"] is True
            assert result["compat"] == "ok"
            assert result["server_api_version"] == client.API_VERSION
            assert result["gateway_status"] in ("ok", "degraded")

    asyncio.run(run())


@pytest.mark.parametrize("projection", [1, 80, 400])
def test_save_wire_body_and_timeout_are_pinned_exactly(clients, gateway, monkeypatch, projection):
    """Pin the whole save body, not only wait_s, to match the behaviour before the
    wait helper was extracted.
    """
    async def run():
        for client in clients:
            monkeypatch.setattr(client, "_CAPABILITY_CACHE", {
                "embedder": {"projected_full_payload_s": projection}})
            await client.save_artifact("fact", json.dumps({"project": "test"}))

    asyncio.run(run())
    wait = min(300, max(30, projection * 1.5 + 15))
    for _, body, timeout in gateway[:2]:
        assert body == {"content": "fact", "metadata": {"project": "test"},
                        "agent_id": "shared_agent", "wait_s": wait}
        assert timeout == wait + 15


async def _both_retros(clients):
    bridge, mcp = clients
    await bridge.save_retrospective_artifact(7, "validated", "notes", date="2026-01-01",
                                             source="shared_agent", grounded_in="3:measured,4")
    await mcp.save_retrospective(7, "validated", "notes", "shared_agent", date="2026-01-01",
                                 grounded_in="3:measured,4")


@pytest.mark.parametrize("projection", [1, 80, 400])
def test_retrospective_wire_bodies_match_and_carry_the_save_deadline(clients, gateway, monkeypatch, projection):
    for client in clients:
        monkeypatch.setattr(client, "_CAPABILITY_CACHE", {
            "embedder": {"projected_full_payload_s": projection}})
    asyncio.run(_both_retros(clients))
    cli, mcp = gateway[:2]
    assert cli == mcp
    url, body, timeout = cli
    assert url.endswith("/memory/retrospective")
    wait = min(300, max(30, projection * 1.5 + 15))
    assert body == {"pg_id": 7, "rating": "validated", "notes": "notes", "date": "2026-01-01",
                    "agent_id": "shared_agent", "grounded_in": [3, 4],
                    "grounded_roles": {"3": "measured"}, "wait_s": wait}
    assert timeout == wait + 15 > wait


@pytest.mark.parametrize("projection", [None, "bad", 0])
def test_retrospective_without_projection_invents_no_deadline(clients, gateway, monkeypatch, projection):
    for client in clients:
        monkeypatch.setattr(client, "_CAPABILITY_CACHE", {
            "embedder": {"projected_full_payload_s": projection}})
    asyncio.run(_both_retros(clients))
    assert gateway[0] == gateway[1]
    assert "wait_s" not in gateway[0][1]
    assert gateway[0][2] == 60.0


def test_invalid_rating_is_refused_before_any_network_call(clients, gateway, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("network or capability call before rating check")

    async def run():
        for client in clients:
            monkeypatch.setattr(client, "_gateway_capability", boom)
        bridge, mcp = clients
        assert (await bridge.save_retrospective_artifact(1, "great", "n"))["status"] == "error"
        assert json.loads(await mcp.save_retrospective(1, "great", "n", "a"))["status"] == "error"

    asyncio.run(run())
    assert gateway == []


def test_the_two_memory_bridge_copies_are_byte_identical():
    root = Path(__file__).resolve().parents[1]
    assert ((root / "shared-memory/scripts/memory_bridge.py").read_bytes()
            == (root / "shared-memory-skill/shared-memory/scripts/memory_bridge.py").read_bytes())
