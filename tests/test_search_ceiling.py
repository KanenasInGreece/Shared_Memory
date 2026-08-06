"""
Tests for the CLIENT-side search timeout — the contract between a front door and
the gateway it is waiting on.

Why this file exists: v0.8.51 established that a timeout must be DERIVED from the
work being asked for, and applied it to the server's rerank call. Neither client
was brought under that rule. Both kept a constant — the CLI 30 s, the MCP 60 s —
while the gateway sized its own rerank from measured throughput and published the
result on /health. Measured 2026-08-06 at v0.8.56: real searches cost 19-35 s
against the CLI's 30 s, so searches failed INTERMITTENTLY, and the client reported
a gateway that had answered /health 3 ms earlier as down (fact:1112).

Nothing in the suite could catch it: every search test stubs the HTTP client, so
the timeout argument was never a value any assertion looked at.

The invariants:

  S1  The ceiling is never below what the GATEWAY ITSELF projects the call will
      cost. This is the defect, stated positively — a client that hangs up before
      the server it asked can answer.

  S2  An unknown cost falls back ABOVE the constant being replaced, never to it.
      The failure being fixed is a ceiling below the real cost, so ignorance must
      not resolve to the number already known to be too small.

  S3  SEARCH_TIMEOUT_S overrides the derivation outright — the operator's escape
      hatch, and the only way to get a constant back.

  S4  The derived value is clamped at both ends: a floor so the ceiling never
      regresses below what shipped, and a maximum so a pathological probe cannot
      make an agent block indefinitely.

  S5  BOTH FRONT DOORS AGREE. The two clients never import each other, so the
      rule is stated twice and only a test can hold the copies in step.

  S6  A read timeout is not reported as an unreachable gateway. httpx's
      ReadTimeout stringifies to the empty string, so folding the two together
      told the reader to check a daemon that was running.
"""

import importlib.util
import os
import sys

import httpx
import pytest


# ── Dynamic import of both front doors ───────────────────────────────────────
# memory_bridge is loaded from the TRACKED SKILL COPY, mirroring
# test_memory_bridge.py: that copy is what sync_skills.sh delivers, so it is the
# one whose behaviour reaches an agent.

def _load(name, *parts):
    path = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", *parts))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


memory_bridge = _load(
    "memory_bridge",
    "shared-memory-skill", "shared-memory", "scripts", "memory_bridge.py",
)
vector_skill = _load("vector_skill", "vector-skill.py")

CLIENTS = [
    pytest.param(memory_bridge, id="cli"),
    pytest.param(vector_skill, id="mcp"),
]

# The live /health block measured on 2026-08-06 at v0.8.56, verbatim in shape.
# The reranker's projection is what the old constants sat below.
LIVE_CAPABILITY = {
    "status": "ok",
    "reranker": {
        "probe_chars": 4000,
        "latency_s": 1.03,
        "throughput_chars_s": 3870,
        "projected_full_payload_s": 127.0,
        "ceiling_s": 921.6,
        "serves_full_payload": True,
        "status": "ok",
    },
    "embedder": {
        "probe_chars": 1000,
        "latency_s": 0.26,
        "throughput_chars_s": 3906,
        "projected_full_payload_s": 6.3,
        "ceiling_s": 122.9,
        "serves_full_payload": True,
        "status": "ok",
    },
}

# The constants that shipped, and that the derivation replaces.
OLD_CLI_CONSTANT = 30.0
OLD_MCP_CONSTANT = 60.0


@pytest.mark.parametrize("client", CLIENTS)
def test_s1_ceiling_covers_the_gateways_own_projection(client):
    """S1. The gateway published 127.0 s for a full payload while the CLI waited
    30 s and the MCP door 60 s. A client must never hang up before the server it
    asked can answer."""
    projected = LIVE_CAPABILITY["reranker"]["projected_full_payload_s"]
    ceiling = client.search_ceiling(LIVE_CAPABILITY)

    assert ceiling > projected, (
        f"ceiling {ceiling}s is below the gateway's own projection of {projected}s"
    )
    # And specifically above both constants that shipped — the regression itself.
    assert ceiling > OLD_CLI_CONSTANT
    assert ceiling > OLD_MCP_CONSTANT


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("capability", [
    pytest.param(None, id="absent"),
    pytest.param({}, id="empty"),
    pytest.param({"reranker": "not-a-dict"}, id="malformed"),
    pytest.param({"reranker": {"status": "error"}}, id="unprobed"),
    pytest.param({"reranker": {"projected_full_payload_s": None}}, id="null-projection"),
    pytest.param({"reranker": {"projected_full_payload_s": "abc"}}, id="unparseable"),
])
def test_s2_unknown_cost_falls_back_above_the_old_constant(client, capability):
    """S2. Ignorance must not resolve to the number already known to be too
    small. Every unusable shape lands on the fallback, and the fallback is
    higher than the constant it replaces."""
    ceiling = client.search_ceiling(capability)

    assert ceiling == client.SEARCH_TIMEOUT_FALLBACK_S
    assert ceiling > OLD_CLI_CONSTANT
    assert ceiling > OLD_MCP_CONSTANT


@pytest.mark.parametrize("client", CLIENTS)
def test_s3_explicit_override_wins_outright(client, monkeypatch):
    """S3. SEARCH_TIMEOUT_S is the escape hatch: it must beat the derivation and
    both clamps, or an operator cannot pin a value at all."""
    monkeypatch.setattr(client, "SEARCH_TIMEOUT_S", 42.0)

    assert client.search_ceiling(LIVE_CAPABILITY) == 42.0
    assert client.search_ceiling(None) == 42.0

    # Beats the clamps too — an override below the floor is still honoured.
    monkeypatch.setattr(client, "SEARCH_TIMEOUT_S", 5.0)
    assert client.search_ceiling(LIVE_CAPABILITY) == 5.0


@pytest.mark.parametrize("client", CLIENTS)
def test_s4_derived_value_is_clamped_at_both_ends(client):
    """S4. A floor so a fast backend cannot push the ceiling below what shipped,
    and a maximum so a pathological probe cannot block an agent indefinitely."""
    tiny = {"reranker": {"projected_full_payload_s": 0.01},
            "embedder": {"projected_full_payload_s": 0.01}}
    assert client.search_ceiling(tiny) == client.SEARCH_TIMEOUT_FLOOR_S

    absurd = {"reranker": {"projected_full_payload_s": 100000.0}}
    assert client.search_ceiling(absurd) == client.SEARCH_TIMEOUT_MAX_S


@pytest.mark.parametrize("capability", [
    pytest.param(LIVE_CAPABILITY, id="live"),
    pytest.param(None, id="absent"),
    pytest.param({"reranker": {"projected_full_payload_s": 0.01}}, id="floor"),
    pytest.param({"reranker": {"projected_full_payload_s": 100000.0}}, id="max"),
    pytest.param({"embedder": {"projected_full_payload_s": 40.0}}, id="embedder-only"),
])
def test_s5_both_front_doors_derive_the_same_ceiling(capability):
    """S5. Two doors to one gateway. They never import each other, so the rule is
    written twice — and a capability added to one and not the other is exactly
    the Group 1 failure this suite exists to catch."""
    assert memory_bridge.search_ceiling(capability) == vector_skill.search_ceiling(capability)


def test_s5b_both_front_doors_ship_the_same_tunables():
    """S5, second half. Equal outputs today mean nothing if the knobs drift."""
    for name in ("SEARCH_TIMEOUT_FLOOR_S", "SEARCH_TIMEOUT_MAX_S",
                 "SEARCH_TIMEOUT_FALLBACK_S", "SEARCH_SAFETY_FACTOR",
                 "SEARCH_OVERHEAD_S"):
        assert getattr(memory_bridge, name) == getattr(vector_skill, name), name


def test_s6_cli_read_timeout_is_not_reported_as_a_dead_gateway():
    """S6. The message that cost this session the misdiagnosis. A ReadTimeout
    stringifies to nothing, so the old text read
    'unreachable ... is hive_mind_proxy.py running? ()' about a live gateway."""
    timed_out = memory_bridge._coordinator_unavailable(httpx.ReadTimeout(""), 215.0)
    message = timed_out["message"]

    assert "hive_mind_proxy.py running" not in message
    assert "unreachable" not in message.lower()
    assert "215s" in message                       # names the ceiling that was hit
    assert "SEARCH_TIMEOUT_S" in message           # and how to raise it

    # The genuine case must still say what it always said.
    dead = memory_bridge._coordinator_unavailable(httpx.ConnectError("refused"))
    assert "unreachable" in dead["message"].lower()
    assert "hive_mind_proxy.py running" in dead["message"]


def test_s6b_mcp_read_timeout_is_not_reported_as_a_dead_gateway():
    """S6 on the other door — which told the reader to start a running service."""
    timed_out = vector_skill._unavailable(httpx.ReadTimeout(""), 215.0)

    assert "unreachable" not in timed_out.lower()
    assert "systemctl" not in timed_out
    assert "215s" in timed_out
    assert "SEARCH_TIMEOUT_S" in timed_out

    dead = vector_skill._unavailable(httpx.ConnectError("refused"))
    assert "unreachable" in dead.lower()
    assert "systemctl" in dead


@pytest.mark.asyncio
@pytest.mark.parametrize("client", CLIENTS)
async def test_sizing_never_fails_the_search(client, monkeypatch):
    """The probe is a convenience, not a dependency: a gateway that cannot answer
    /health must degrade to the fallback, never raise out of the search path."""
    monkeypatch.setattr(client, "_CAPABILITY_CACHE", None)

    class _Boom:
        async def __aenter__(self):
            raise httpx.ConnectError("no gateway")

        async def __aexit__(self, *_):
            return False

    if client is memory_bridge:
        monkeypatch.setattr(client, "_async_client", lambda _t: _Boom())
    else:
        monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_k: _Boom())

    assert await client._gateway_capability() is None
    assert client.search_ceiling(None) == client.SEARCH_TIMEOUT_FALLBACK_S
