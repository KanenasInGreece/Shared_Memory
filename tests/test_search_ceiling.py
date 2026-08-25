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

import asyncio
import importlib.util
import pytest
pytest.importorskip("fastmcp")
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
vector_skill = _load("vector_skill", "mcp", "vector-skill.py")

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


# ── B1 (fact:1560, grounded on decision:1114) ─────────────────────────────────
# "Ignorance must not resolve to the number already known to be too small" —
# extended from "nothing probed at all" (S2 above) to the MIXED case: one
# backend probes fine, the other reports its cost is UNKNOWN. The known
# backend's number is only a LOWER bound on the true cost; treating the
# failing backend as contributing zero let the derivation fall all the way to
# SEARCH_TIMEOUT_FLOOR_S (30s) — the exact number already known to be too
# small — while genuinely not knowing what the failing backend costs.

@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("bad_block", [
    pytest.param({"status": "failing"}, id="status-failing"),
    pytest.param({"projection_stale": True}, id="projection-stale"),
    pytest.param({"status": "failing", "projection_stale": True}, id="both"),
])
def test_s7_mixed_probed_and_failing_backend_floors_at_fallback_not_floor(client, bad_block):
    """One backend probes to a tiny number, the other's cost is UNKNOWN. The
    floor under the derivation must be SEARCH_TIMEOUT_FALLBACK_S (120), never
    SEARCH_TIMEOUT_FLOOR_S (30) — this is the defect this unit fixes."""
    capability = {
        "reranker": {"projected_full_payload_s": 1.0, "status": "ok"},
        "embedder": bad_block,
    }
    ceiling = client.search_ceiling(capability)
    assert ceiling == client.SEARCH_TIMEOUT_FALLBACK_S
    assert ceiling > client.SEARCH_TIMEOUT_FLOOR_S


@pytest.mark.parametrize("client", CLIENTS)
def test_s7b_known_cost_above_fallback_still_wins_over_the_fallback_floor(client):
    """The fallback is a FLOOR, not a cap — when the known backend alone
    already projects above it, that larger number must still win."""
    capability = {
        "reranker": {"projected_full_payload_s": 200.0, "status": "ok"},
        "embedder": {"status": "failing"},
    }
    # 200.0 * 1.5 + 15 = 315, clamped to SEARCH_TIMEOUT_MAX_S (300).
    assert client.search_ceiling(capability) == client.SEARCH_TIMEOUT_MAX_S


@pytest.mark.parametrize("client", CLIENTS)
def test_s7c_two_explicitly_healthy_backends_land_on_the_plain_floor(client):
    """T-06 (PR #310 review): the previous name/docstring here claimed
    coverage of "error"/absent status that the body never exercised (both
    blocks were `status: "ok"` with positive projections). Renamed to
    describe what it actually tests: the ordinary two-healthy-backends case
    sits on SEARCH_TIMEOUT_FLOOR_S, unaffected by the unknown-cost guard.
    The "error"/absent/no-projection shapes get their own tests below
    (test_s7d-h, T-05/R2-N3 — absent/empty/malformed now floor at the
    fallback, not the plain floor; only a well-formed non-empty dict with a
    non-triggering status still lands here)."""
    capability = {
        "reranker": {"projected_full_payload_s": 0.01, "status": "ok"},
        "embedder": {"projected_full_payload_s": 0.01, "status": "ok"},
    }
    assert client.search_ceiling(capability) == client.SEARCH_TIMEOUT_FLOOR_S


# ── R2-N3 (PR-A delta review): absent/empty/non-dict IS unknown-cost too ────
# An earlier round of this file documented ABSENT/malformed backend blocks as
# NOT tripping the fallback floor (T-05) — PR-A's delta review overturned that
# for three of the four shapes: a block that is ABSENT entirely, an empty
# `{}`, or not a dict at all (malformed) is now the SAME ignorance as an
# explicit `status: "failing"`. Only a WELL-FORMED, NON-EMPTY dict carrying a
# plain `status: "error"` or `"ok"` with no projection remains outside the
# guard (test_s7e/f below) — those are exercised and pinned as documented
# behaviour for the same reason T-05 gave: not reachable from our own
# gateway's probe today, but the function has to make sense of an older,
# third-party or future gateway's /health too.

@pytest.mark.parametrize("client", CLIENTS)
def test_s7d_reranker_key_entirely_absent_now_floors_at_fallback(client):
    """R2-N3: an absent key is ignorance, not zero cost — same bucket as
    `status: "failing"`. (Previously documented as landing on the plain
    floor; that was the shape R2-N3 overturned.)"""
    capability = {"embedder": {"projected_full_payload_s": 0.01, "status": "ok"}}
    ceiling = client.search_ceiling(capability)
    assert ceiling == client.SEARCH_TIMEOUT_FALLBACK_S
    assert ceiling > client.SEARCH_TIMEOUT_FLOOR_S


@pytest.mark.parametrize("client", CLIENTS)
def test_s7g_empty_dict_block_now_floors_at_fallback(client):
    """R2-N3: `{}` is a well-formed dict by `isinstance`, but it carries no
    information at all — the same ignorance as an absent key, not the same
    as a populated-but-non-triggering block (test_s7e/f)."""
    capability = {
        "reranker": {},
        "embedder": {"projected_full_payload_s": 0.01, "status": "ok"},
    }
    ceiling = client.search_ceiling(capability)
    assert ceiling == client.SEARCH_TIMEOUT_FALLBACK_S
    assert ceiling > client.SEARCH_TIMEOUT_FLOOR_S


@pytest.mark.parametrize("client", CLIENTS)
def test_s7h_non_dict_block_mixed_with_a_probed_sibling_floors_at_fallback(client):
    """R2-N3: a malformed (non-dict) block alongside a genuinely probed
    sibling — S2 already covered "everything is malformed" (falls through the
    `not probed` branch regardless of `unknown`); this is the MIXED case that
    branch does not exercise."""
    capability = {
        "reranker": "not-a-dict",
        "embedder": {"projected_full_payload_s": 1.0, "status": "ok"},
    }
    ceiling = client.search_ceiling(capability)
    assert ceiling == client.SEARCH_TIMEOUT_FALLBACK_S
    assert ceiling > client.SEARCH_TIMEOUT_FLOOR_S


@pytest.mark.parametrize("client", CLIENTS)
def test_s7e_documented_ok_status_with_no_projection_lands_on_plain_floor(client):
    """NOT covered by R2-N3: a well-formed, NON-EMPTY dict with a plain "ok"
    status and no projection still lands on the plain floor — only an
    absent/empty/non-dict block, or an explicit failing/stale signal, floors
    at the fallback."""
    capability = {
        "reranker": {"status": "ok", "serves_full_payload": False},  # no projected_full_payload_s
        "embedder": {"projected_full_payload_s": 0.01, "status": "ok"},
    }
    assert client.search_ceiling(capability) == client.SEARCH_TIMEOUT_FLOOR_S


@pytest.mark.parametrize("client", CLIENTS)
def test_s7f_documented_plain_error_status_lands_on_plain_floor(client):
    """`status: "error"` is not `status: "failing"`, and the block is
    well-formed and non-empty — NOT covered by R2-N3 either."""
    capability = {
        "reranker": {"status": "error"},
        "embedder": {"projected_full_payload_s": 0.01, "status": "ok"},
    }
    assert client.search_ceiling(capability) == client.SEARCH_TIMEOUT_FLOOR_S


# ── B1 capacity fold — the gateway's own MEASURED worst case ────────────────

CAPACITY_HIGH_CEILING = {"derived": {"client_ceiling_s": 90.0}}
CAPACITY_HIGH_S_MAX = {"derived": {"s_max_measured_s": 50.0}}
CAPACITY_HIGH_S_MEAN = {"derived": {"s_mean_s": 50.0}}


@pytest.mark.parametrize("client", CLIENTS)
def test_s8_capacity_client_ceiling_s_raises_the_ceiling_when_higher(client):
    """The theoretical projection here clamps to the 30s floor; the server's
    own already-derived client_ceiling_s (90.0) must win because it's larger."""
    capability = {"reranker": {"projected_full_payload_s": 1.0, "status": "ok"},
                  "embedder": {"projected_full_payload_s": 1.0, "status": "ok"}}
    assert client.search_ceiling(capability, CAPACITY_HIGH_CEILING) == 90.0


@pytest.mark.parametrize("client", CLIENTS)
def test_s8b_capacity_s_max_measured_s_gets_the_same_safety_scaling(client):
    """s_max_measured_s is a raw reranker-seconds figure — not yet scaled for
    THIS client — so it gets the same SAFETY_FACTOR/OVERHEAD_S treatment as
    the theoretical projection: 50.0 * 1.5 + 15 = 90.0. Both backends probed
    (R2-N3: an absent one would itself floor at the fallback, muddying what
    this test isolates)."""
    capability = {"reranker": {"projected_full_payload_s": 1.0, "status": "ok"},
                  "embedder": {"projected_full_payload_s": 1.0, "status": "ok"}}
    assert client.search_ceiling(capability, CAPACITY_HIGH_S_MAX) == 90.0


@pytest.mark.parametrize("client", CLIENTS)
def test_s8b2_capacity_s_mean_s_gets_the_same_safety_scaling(client):
    """B1/T-02 (PR #310 review): s_mean_s is the gateway's own full-payload
    projection — always present once the gateway has probed at all, unlike
    s_max_measured_s which needs real search traffic first. Same treatment:
    50.0 * 1.5 + 15 = 90.0. Both backends probed (R2-N3: see test_s8b)."""
    capability = {"reranker": {"projected_full_payload_s": 1.0, "status": "ok"},
                  "embedder": {"projected_full_payload_s": 1.0, "status": "ok"}}
    assert client.search_ceiling(capability, CAPACITY_HIGH_S_MEAN) == 90.0


@pytest.mark.parametrize("client", CLIENTS)
def test_s8b3_t02_fact_1560_measured_case_is_now_sized_correctly(client):
    """T-02 (PR #310 review), THE central finding: on the host fact:1560
    measured (reranker failing, embedder probed ~1.8s), the ORIGINAL fold
    (client_ceiling_s + s_max_measured_s only) contributed NOTHING — the
    broken server mirror (T-01, out of scope here — server file) still
    returns 30.0 for client_ceiling_s, and s_max_measured_s is None before
    real search traffic. s_mean_s (259.9 on d9400, fact:1560's own number)
    is the one field that sizes it correctly. Verified: 259.9 * 1.5 + 15 =
    404.85, clamped to SEARCH_TIMEOUT_MAX_S (300) — far closer to the
    measured 96-260s reality than the pre-fix 120s fallback, let alone the
    original defect's 30s floor."""
    capability = {
        "reranker": {"status": "failing", "error": "TimeoutError"},
        "embedder": {"projected_full_payload_s": 1.8, "status": "ok"},
    }
    capacity = {"derived": {
        "client_ceiling_s": 30.0,   # T-01: the unfixed server mirror's output
        "s_max_measured_s": None,   # no real search traffic yet
        "s_mean_s": 259.9,          # fact:1560's own measured d9400 number
    }}
    assert client.search_ceiling(capability, capacity) == client.SEARCH_TIMEOUT_MAX_S
    # And specifically: it is the s_mean_s fold doing the work, not the
    # unknown-cost fallback alone (120.0) — pin the value, not just an
    # inequality (fact:1309).
    assert client.search_ceiling(capability, capacity) > client.SEARCH_TIMEOUT_FALLBACK_S


@pytest.mark.parametrize("client", CLIENTS)
def test_s8c_capacity_never_lowers_the_ceiling(client):
    """The server measurement is a floor a client can be RAISED to, never a
    cap it gets lowered to — a bigger theoretical projection still wins."""
    capability = {"reranker": {"projected_full_payload_s": 200.0, "status": "ok"}}
    low_capacity = {"derived": {"client_ceiling_s": 40.0}}
    assert (client.search_ceiling(capability, low_capacity)
            == client.search_ceiling(capability, None))


@pytest.mark.parametrize("client", CLIENTS)
def test_s8d_capacity_alone_is_still_clamped_to_max(client):
    huge_capacity = {"derived": {"client_ceiling_s": 100000.0}}
    assert client.search_ceiling(None, huge_capacity) == client.SEARCH_TIMEOUT_MAX_S


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("capacity", [
    pytest.param(None, id="none"),
    pytest.param({}, id="empty"),
    pytest.param({"derived": "not-a-dict"}, id="malformed-derived"),
    pytest.param({"derived": {"client_ceiling_s": "abc"}}, id="unparseable"),
    pytest.param({"derived": {"client_ceiling_s": -5.0}}, id="negative"),
    pytest.param({"derived": {"client_ceiling_s": 0}}, id="zero"),
    pytest.param({"derived": {"s_mean_s": "abc"}}, id="s_mean_s-unparseable"),
    pytest.param({"derived": {"s_mean_s": -5.0}}, id="s_mean_s-negative"),
    pytest.param({"derived": {"s_mean_s": 0}}, id="s_mean_s-zero"),
    pytest.param({"derived": {"s_mean_s": None}}, id="s_mean_s-none"),
])
def test_s8e_malformed_or_absent_capacity_is_ignored(client, capacity):
    capability = {"reranker": {"projected_full_payload_s": 1.0, "status": "ok"},
                  "embedder": {"projected_full_payload_s": 1.0, "status": "ok"}}
    assert (client.search_ceiling(capability, capacity)
            == client.search_ceiling(capability, None))


@pytest.mark.parametrize("client", CLIENTS)
def test_s9_explicit_override_wins_over_capacity_too(client, monkeypatch):
    """SEARCH_TIMEOUT_S is the operator's escape hatch — it must beat the
    capacity fold exactly as it beats the derivation and both clamps (S3)."""
    monkeypatch.setattr(client, "SEARCH_TIMEOUT_S", 42.0)
    huge_capacity = {"derived": {"client_ceiling_s": 999.0}}
    assert client.search_ceiling(None, huge_capacity) == 42.0


@pytest.mark.parametrize("capacity", [
    pytest.param(None, id="none"),
    pytest.param(CAPACITY_HIGH_CEILING, id="client_ceiling_s"),
    pytest.param(CAPACITY_HIGH_S_MAX, id="s_max_measured_s"),
    pytest.param(CAPACITY_HIGH_S_MEAN, id="s_mean_s"),
])
def test_s5c_both_front_doors_derive_the_same_ceiling_with_capacity(capacity):
    """S5, extended to the capacity parameter added in B1."""
    assert (memory_bridge.search_ceiling(LIVE_CAPABILITY, capacity)
            == vector_skill.search_ceiling(LIVE_CAPABILITY, capacity))


@pytest.mark.asyncio
@pytest.mark.parametrize("client", CLIENTS)
async def test_capability_and_capacity_share_one_health_request(client, monkeypatch):
    """B1c: _gateway_capacity() must not cost a second /health round trip —
    both blocks are cached from the SAME fetch, exactly like the coordinator's
    own capability probe is a single call reused for both."""
    monkeypatch.setattr(client, "_CAPABILITY_CACHE", None)
    monkeypatch.setattr(client, "_CAPACITY_CACHE", None, raising=False)
    payload = {"backend_capability": LIVE_CAPABILITY,
              "capacity": {"derived": {"client_ceiling_s": 55.0}}}
    calls = {"n": 0}

    class _Health:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, *_a, **_kw):
            calls["n"] += 1
            return type("R", (), {"status_code": 200,
                                  "json": staticmethod(lambda: payload)})()

    if client is memory_bridge:
        monkeypatch.setattr(client, "_async_client", lambda _t: _Health())
    else:
        monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_k: _Health())

    capability = await client._gateway_capability()
    capacity = await client._gateway_capacity()

    assert calls["n"] == 1
    assert capability == LIVE_CAPABILITY
    assert capacity == {"derived": {"client_ceiling_s": 55.0}}


@pytest.mark.asyncio
@pytest.mark.parametrize("client", CLIENTS)
async def test_concurrent_first_calls_still_fire_exactly_one_health_request(client, monkeypatch):
    """CQ-03 (PR #310 review): two searches starting in the same instant must
    not both see an empty cache and both fire a /health request — the
    asyncio.Lock around the fetch-and-fill serializes them, and the second
    waiter finds the cache already filled after it acquires the lock."""
    monkeypatch.setattr(client, "_CAPABILITY_CACHE", None)
    monkeypatch.setattr(client, "_CAPACITY_CACHE", None, raising=False)
    monkeypatch.setattr(client, "_HEALTH_FETCH_LOCK", asyncio.Lock())
    payload = {"backend_capability": LIVE_CAPABILITY, "capacity": None}
    calls = {"n": 0}

    class _SlowHealth:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, *_a, **_kw):
            calls["n"] += 1
            await asyncio.sleep(0.05)   # widens the race window on purpose
            return type("R", (), {"status_code": 200,
                                  "json": staticmethod(lambda: payload)})()

    if client is memory_bridge:
        monkeypatch.setattr(client, "_async_client", lambda _t: _SlowHealth())
    else:
        monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_k: _SlowHealth())

    results = await asyncio.gather(
        client._gateway_capability(), client._gateway_capability(),
        client._gateway_capability(), client._gateway_capability(),
    )

    assert calls["n"] == 1
    assert all(r == LIVE_CAPABILITY for r in results)



# R2-02 (PR #310 review round 2 delta): the two REAL shapes PR-A's merged
# server actually produces via `_merge_capability_projection()` /
# `_projection_age_s()` -- not hand-written minimal dicts. Both pinned by
# VALUE, not just parity (fact:1309).

FACT_1560_STALE_WITH_CARRIED = {
    "status": "ok",
    "probed_at": "2026-08-25T12:00:00+00:00",
    "reranker": {
        "probe_chars": 4000,
        "status": "failing",
        "error": "TimeoutError",
        "projected_full_payload_s": 127.0,   # carried from the last OK cycle
        "ceiling_s": 921.6,
        "throughput_chars_s": 3870,
        "latency_s": 1.03,
        "serves_full_payload": None,          # the verdict does NOT travel
        "projection_stale": True,
        "last_ok_at": "2026-08-25T10:42:29.900000+00:00",
        "projection_age_s": 4650.1,
    },
    "embedder": {
        "probe_chars": 1000,
        "status": "ok",
        "projected_full_payload_s": 6.3,
        "ceiling_s": 122.9,
        "throughput_chars_s": 3906,
        "latency_s": 0.26,
        "serves_full_payload": True,
        "projection_stale": False,
        "last_ok_at": "2026-08-25T12:00:00+00:00",
        "projection_age_s": 0.0,
    },
}

FACT_1560_NEVER_MEASURED = {
    "status": "unknown",
    "probed_at": None,
    "reranker": {
        "probe_chars": 4000,
        "status": "failing",
        "error": "ConnectError",
        "serves_full_payload": None,
        "projection_stale": None,   # PR-A's third state: never measured
    },
    "embedder": {
        "probe_chars": 1000,
        "status": "failing",
        "error": "ConnectError",
        "serves_full_payload": None,
        "projection_stale": None,
    },
}


@pytest.mark.parametrize("client", CLIENTS)
def test_r2_02_stale_with_carried_number_ceiling_is_214_95(client):
    """The carried 127.0 (reranker) is USED, not treated as unknown -- a
    stale-but-real number still costs money; only a TRULY unknown cost floors
    at the fallback. (127.0 + 6.3) * 1.5 + 15 = 214.95."""
    assert client.search_ceiling(FACT_1560_STALE_WITH_CARRIED) == pytest.approx(214.95)


@pytest.mark.parametrize("client", CLIENTS)
def test_r2_02_never_measured_ceiling_is_120_via_the_failing_signal(client):
    """projection_stale: None (never measured) carries no number to use, so
    the explicit status: "failing" signal on both backends floors at the
    fallback -- same value as the mixed case, reached via "nothing has ever
    been measured" rather than "a measurement stopped being fresh"."""
    assert (client.search_ceiling(FACT_1560_NEVER_MEASURED)
            == client.SEARCH_TIMEOUT_FALLBACK_S == 120.0)


def test_r2_02_both_real_shapes_parity_between_doors():
    assert (memory_bridge.search_ceiling(FACT_1560_STALE_WITH_CARRIED)
            == vector_skill.search_ceiling(FACT_1560_STALE_WITH_CARRIED))
    assert (memory_bridge.search_ceiling(FACT_1560_NEVER_MEASURED)
            == vector_skill.search_ceiling(FACT_1560_NEVER_MEASURED))
