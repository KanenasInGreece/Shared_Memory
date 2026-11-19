"""Caller deadlines on the wire and degraded-health capability caching."""
import asyncio
import importlib.util
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setenv("SECURE_ENV_FILE", "")
    monkeypatch.setenv("COORDINATOR_UDS", "")
    path = Path(__file__).resolve().parents[1] / "shared-memory/scripts/memory_bridge.py"
    spec = importlib.util.spec_from_file_location("bridge_deadlines", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_append_log", lambda *args, **kwargs: None)
    for name, value in {
        "SEARCH_TIMEOUT_S": 0,
        "SEARCH_TIMEOUT_FLOOR_S": 30,
        "SEARCH_TIMEOUT_MAX_S": 300,
        "SEARCH_TIMEOUT_FALLBACK_S": 120,
        "SEARCH_SAFETY_FACTOR": 1.5,
        "SEARCH_OVERHEAD_S": 15,
    }.items():
        monkeypatch.setattr(module, name, value)
    return module


@pytest.fixture
def http_layer(monkeypatch):
    calls = {"get": [], "post": [], "health_status": 200, "health": {}}

    async def get(self, url, **kwargs):
        calls["get"].append(url)
        if calls.get("unreachable"):
            raise httpx.ConnectError("gateway unavailable")
        return httpx.Response(calls["health_status"], json=calls["health"])

    async def post(self, url, **kwargs):
        calls["post"].append((url, kwargs["json"], self.timeout.read))
        return httpx.Response(200, json={"status": "success", "results": [], "pg_id": 1})

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    return calls


def test_search_sends_derived_wait_and_encoder_budget(bridge, http_layer):
    http_layer["health"] = {"backend_capability": {
        "embedder": {"projected_full_payload_s": 20},
        "reranker": {"projected_full_payload_s": 40},
    }}
    result = asyncio.run(bridge._search_payload("deadline query"))
    assert result["status"] == "success"
    url, body, timeout = http_layer["post"][0]
    assert url.endswith("/memory/search")
    assert body["query"] == "deadline query"
    assert body["wait_s"] == timeout == 105
    assert body["encoder_wait_s"] == body["wait_s"] - 15 == 90


@pytest.mark.parametrize("wait", [15, 10])
def test_search_omits_nonpositive_encoder_budget(bridge, http_layer, monkeypatch, wait):
    monkeypatch.setattr(bridge, "SEARCH_TIMEOUT_S", wait)
    asyncio.run(bridge._search_payload("small budget"))
    _, body, timeout = http_layer["post"][0]
    assert body["wait_s"] == timeout == wait
    assert "encoder_wait_s" not in body


@pytest.mark.parametrize("projection, expected", [(40, 75), (80, 135), (1, 30), (400, 300)])
def test_save_sends_embedder_derived_wait_only(bridge, http_layer, projection, expected):
    http_layer["health"] = {"backend_capability": {
        "embedder": {"projected_full_payload_s": projection},
        "reranker": {"projected_full_payload_s": 900},
    }, "capacity": {"derived": {"client_ceiling_s": 290}}}
    result = asyncio.run(bridge.save_artifact("fact", {"project": "test"}))
    assert result["status"] == "success"
    url, body, timeout = http_layer["post"][0]
    assert url.endswith("/memory/save")
    assert body["content"] == "fact"
    assert body["wait_s"] == expected
    assert timeout == body["wait_s"] + bridge.SEARCH_OVERHEAD_S == expected + 15
    assert "encoder_wait_s" not in body


@pytest.mark.parametrize("embedder", [None, {}, "invalid", {"projected_full_payload_s": "bad"},
                                      {"projected_full_payload_s": 0}, {"projected_full_payload_s": -1}])
def test_save_omits_deadline_without_embedder_projection(bridge, http_layer, embedder):
    http_layer["health"] = {"backend_capability": {
        "embedder": embedder, "reranker": {"projected_full_payload_s": 90},
    }}
    asyncio.run(bridge.save_artifact("fact", {"project": "test"}))
    _, body, timeout = http_layer["post"][0]
    assert timeout == 60.0
    assert "wait_s" not in body
    assert "encoder_wait_s" not in body


def test_503_health_body_is_read_and_cached(bridge, http_layer):
    capability = {"embedder": {"projected_full_payload_s": 40}}
    capacity = {"derived": {"client_ceiling_s": 180}}
    http_layer.update(health_status=503, health={
        "status": "degraded", "backend_capability": capability, "capacity": capacity,
    })

    async def read_twice():
        assert await bridge._gateway_capability() == capability
        assert await bridge._gateway_capacity() == capacity
        assert await bridge._gateway_capability() == capability

    asyncio.run(read_twice())
    assert bridge._CAPABILITY_CACHE == capability
    assert len(http_layer["get"]) == 1


def test_compat_reads_503_health_body(bridge, http_layer):
    http_layer.update(health_status=503, health={
        "status": "degraded", "api_version": bridge.API_VERSION, "version": "1.2.0",
    })
    result = asyncio.run(bridge.check_gateway_compat())
    assert result["reachable"] is True
    assert result["compat"] == "ok"
    assert result["gateway_status"] == "degraded"
    assert result["server_api_version"] == bridge.API_VERSION


def test_unreachable_gateway_is_cached_empty(bridge, http_layer):
    http_layer["unreachable"] = True

    async def read_twice():
        assert await bridge._gateway_capability() is None
        assert await bridge._gateway_capacity() is None
        assert await bridge._gateway_capability() is None

    asyncio.run(read_twice())
    assert bridge._CAPABILITY_CACHE == {}
    assert bridge._CAPACITY_CACHE is None
    assert len(http_layer["get"]) == 1


def test_save_http_timeout_uses_caller_deadline(bridge, http_layer):
    http_layer["health"] = {"backend_capability": {
        "embedder": {"projected_full_payload_s": 80},
    }}
    asyncio.run(bridge.save_artifact("fact", {"project": "test"}))
    _, body, timeout = http_layer["post"][0]
    assert body["wait_s"] == 135
    assert timeout == body["wait_s"] + bridge.SEARCH_OVERHEAD_S == 150


@pytest.mark.parametrize("projection, expected", [(40, 75), (80, 135), (1, 30), (400, 300)])
def test_retrospective_sends_the_save_wait_and_a_timeout_above_it(bridge, http_layer, projection, expected):
    http_layer["health"] = {"backend_capability": {"embedder": {"projected_full_payload_s": projection}}}
    result = asyncio.run(bridge.save_retrospective_artifact(1, "validated", "notes", source="a"))
    assert result["status"] == "success"
    url, body, timeout = http_layer["post"][0]
    assert url.endswith("/memory/retrospective")
    assert body["wait_s"] == expected
    assert timeout == expected + bridge.SEARCH_OVERHEAD_S
    assert {"pg_id", "rating", "notes", "date", "agent_id"} <= set(body)


@pytest.mark.parametrize("embedder", [None, {}, "invalid", {"projected_full_payload_s": "bad"},
                                      {"projected_full_payload_s": 0}, {"projected_full_payload_s": -1}])
def test_retrospective_omits_deadline_without_embedder_projection(bridge, http_layer, embedder):
    http_layer["health"] = {"backend_capability": {"embedder": embedder}}
    asyncio.run(bridge.save_retrospective_artifact(1, "validated", "notes", source="a"))
    _, body, timeout = http_layer["post"][0]
    assert timeout == 60.0
    assert "wait_s" not in body


def test_retrospective_invalid_rating_makes_no_network_call(bridge, http_layer):
    result = asyncio.run(bridge.save_retrospective_artifact(1, "great", "notes"))
    assert result["status"] == "error"
    assert http_layer["post"] == [] and http_layer["get"] == []
