"""THE TELEMETRY CONTRACT, PINNED IN BOTH DIRECTIONS (v0.9.74, decision:1785).

A one-way check is worth very little here. Asserting only that every documented
key is emitted lets a new key arrive in a consumer's payload with no
documentation, no unit and no lifecycle — which is how /health reached 193 keys
without anyone being asked whether they belonged on a 30-second endpoint.
Asserting only that every emitted key is documented lets a key be silently
dropped while its row stays in the table. So:

  1. EVERY EMITTED KEY IS DOCUMENTED — undocumented key ⇒ failure.
  2. EVERY DOCUMENTED KEY IS EMITTED — dropped key ⇒ failure, except the
     conditional set, which is enumerated in one visible place
     (telemetry_contract.CONDITIONAL) precisely so it can be argued with.

And the DOCUMENT is generated from the same dict, so it cannot drift either.
"""

import ast
import asyncio
import importlib
import inspect
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared-memory" / "scripts"))

import telemetry_contract as tc  # noqa: E402
from telemetry_instruments import LatencyRing, Counter, percentile  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════════
# The pure primitives
# ══════════════════════════════════════════════════════════════════════════════

def test_percentile_matches_linear_interpolation():
    """The same definition Postgres' percentile_cont uses, so a ring percentile
    and a SQL percentile in one payload mean the same thing."""
    xs = [1, 2, 3, 4]
    assert percentile(xs, 0.5) == 2.5
    assert percentile(xs, 0.0) == 1
    assert percentile(xs, 1.0) == 4
    assert percentile([10], 0.95) == 10


def test_percentile_of_nothing_is_none_not_zero():
    """⛔ ABSENCE IS NOT ZERO. A ring nobody has written to has not measured a
    zero-millisecond call, and reporting 0.0 would make "never measured" read as
    "instantaneous"."""
    assert percentile([], 0.5) is None
    assert LatencyRing().snapshot()["p50_ms"] is None


def test_a_failed_call_is_counted_but_never_timed_into_the_window():
    """A failure is fast (connection refused) or pinned to a timeout ceiling —
    either way it describes the failure, not the service. Letting it into the
    window makes an outage read as a latency improvement."""
    r = LatencyRing(maxlen=10)
    r.record(100.0)
    r.record_error()
    r.record_error()
    snap = r.snapshot()
    assert snap["calls"] == 1
    assert snap["errors"] == 2
    assert snap["p95_ms"] == 100.0
    assert snap["window"] == 1


def test_the_window_reports_observations_not_capacity():
    """"p95 over 3 calls" and "p95 over 200 calls" must be distinguishable."""
    r = LatencyRing(maxlen=4)
    for v in (1.0, 2.0, 3.0):
        r.record(v)
    assert r.snapshot()["window"] == 3
    for v in (4.0, 5.0, 6.0):
        r.record(v)
    assert r.snapshot()["window"] == 4          # bounded by maxlen
    assert r.snapshot()["max_ms"] == 6.0        # lifetime max, not window max


def test_a_recorder_never_raises_into_its_caller():
    """⛔ THE INVARIANT THE WHOLE MODULE EXISTS FOR. A metric on a work path must
    not change that path's failure modes."""
    r = LatencyRing()
    r.record("not a number")        # noqa: type
    r.record(None)                  # noqa: type
    Counter(("a",)).bump("missing-key-is-fine")
    assert r.snapshot()["calls"] == 0


def test_a_counter_stamps_its_timestamp_at_the_increment():
    """A bare count resets with the process, so a poll-delta INVERTS across a
    restart. Stamped beside the counter so the pair cannot disagree."""
    c = Counter(("x", "y"))
    c.bump("x", ts="2026-08-28T00:00:00+00:00")
    assert c.snapshot() == {"x": 1, "y": 0}
    assert c.last_ts()["x"] == "2026-08-28T00:00:00+00:00"
    assert c.last_ts()["y"] is None


# ══════════════════════════════════════════════════════════════════════════════
# The walker
# ══════════════════════════════════════════════════════════════════════════════

def test_the_walker_canonicalises_a_dynamic_key_that_contains_dots():
    """⚠ THE REASON THE WALKER DESCENDS A TRIE INSTEAD OF SPLITTING A STRING.
    An LLM backend key is a URL — `llm_pool["http://localhost:5000"]` — so a
    path built by joining and then split on "." would shatter the key into four
    segments and match nothing."""
    contract = {"llm_pool.*.weight": tc._k("float", "llm")}
    payload = {"llm_pool": {"http://localhost:5000": {"weight": 1.0}}}
    assert tc.canonical_paths(payload, contract) == {"llm_pool.*.weight"}


def test_an_empty_container_emits_itself_because_it_has_no_leaves():
    contract = {"by_reason": tc._k("dict", "graph"),
                "hubs[]": tc._k("list", "graph")}
    paths = tc.canonical_paths({"by_reason": {}, "hubs": []}, contract)
    assert paths == {"by_reason", "hubs[]"}


def test_an_unknown_key_keeps_its_literal_spelling_so_it_can_be_named():
    contract = {"known": tc._k("int", "liveness")}
    paths = tc.canonical_paths({"known": 1, "surprise": 2}, contract)
    assert "surprise" in paths


def test_int_satisfies_float_but_float_never_satisfies_int():
    """JSON has one number type and a percentile landing on a whole number
    serialises as an int — but a float where an int is declared is a real
    defect, not a serialisation artefact."""
    assert tc.type_matches(3, tc._k("float", "liveness"))
    assert not tc.type_matches(3.5, tc._k("int", "liveness"))


def test_a_bool_is_never_accepted_as_an_int():
    """bool is a subclass of int in Python, so a naive isinstance check would
    let a True/False swap pass the type gate unnoticed."""
    assert not tc.type_matches(True, tc._k("int", "liveness"))
    assert tc.type_matches(True, tc._k("bool", "liveness"))


# ══════════════════════════════════════════════════════════════════════════════
# The document is generated, so it cannot drift
# ══════════════════════════════════════════════════════════════════════════════

DOC = (Path(__file__).resolve().parents[1]
       / "shared-memory" / "Documentation" / "telemetry-contract.md")


def test_the_checked_in_document_matches_the_contract_dict():
    """⛔ THE DOC IS GENERATED, NEVER HAND-EDITED. A key added to the dict and
    not regenerated into the document fails HERE rather than reaching a reader
    as a silently incomplete table.

    Regenerate with:
      uv run python shared-memory/scripts/telemetry_contract.py \\
        > shared-memory/Documentation/telemetry-contract.md
    """
    assert DOC.exists(), f"{DOC} is missing — regenerate it"
    assert DOC.read_text() == tc.render_markdown(), (
        "telemetry-contract.md is stale — regenerate it (see this test's "
        "docstring for the command)")


def test_every_contract_entry_declares_a_known_category_and_type():
    known_types = {"str", "int", "float", "bool", "list", "dict", "null"}
    for endpoint, contract in (("health", tc.HEALTH), ("telemetry", tc.TELEMETRY)):
        for path, spec in contract.items():
            assert spec["category"] in tc.CATEGORIES, f"{endpoint}:{path}"
            assert set(spec["types"]) <= known_types, f"{endpoint}:{path}"
            assert spec["types"], f"{endpoint}:{path} declares no type"


def test_a_moved_key_always_names_the_release_that_removes_it():
    """A `moved_to` with no `removed_in` is a migration nobody can plan: the
    consumer is told to read elsewhere but never told when the old path stops
    answering."""
    for endpoint, contract in (("health", tc.HEALTH), ("telemetry", tc.TELEMETRY)):
        for path, spec in contract.items():
            if spec["moved_to"]:
                assert spec["removed_in"], f"{endpoint}:{path} moves but never expires"


def test_every_conditional_entry_names_a_real_documented_key():
    """The exemption list must not outlive the keys it exempts — a stale entry
    silently exempts nothing and hides that the real key lost its cover."""
    for entry in tc.CONDITIONAL:
        endpoint, _, path = entry.partition(":")
        contract = tc.HEALTH if endpoint == "health" else tc.TELEMETRY
        assert path in contract, f"CONDITIONAL names {entry}, which is not documented"


# ══════════════════════════════════════════════════════════════════════════════
# TWO-WAY: /health
# ══════════════════════════════════════════════════════════════════════════════

_FULL_CAPABILITY = {
    "probed_at": "2026-08-28T00:00:00+00:00",
    "gateway_host_load1": 0.5,
    "status": "ok",
    "embedder": {"probe_chars": 100, "latency_s": 0.1, "throughput_chars_s": 1000,
                 "projected_full_payload_s": 1.0, "ceiling_s": 5.0,
                 "serves_full_payload": True, "status": "ok",
                 "projection_stale": False, "last_ok_at": "2026-08-28T00:00:00+00:00",
                 "projection_age_s": 1.0},
    "reranker": {"probe_chars": 100, "latency_s": 0.1, "throughput_chars_s": 1000,
                 "projected_full_payload_s": 1.0, "ceiling_s": 5.0,
                 "serves_full_payload": True, "status": "ok",
                 "projection_stale": False, "last_ok_at": "2026-08-28T00:00:00+00:00",
                 "projection_age_s": 1.0},
}

_FULL_CAPACITY = {
    "timestamp": "2026-08-28T00:00:00+00:00",
    "trigger": "probe",
    "fingerprint": {
        "hardware": {"nproc": 8, "mem_total_bytes": 1, "gpu_present": True},
        "encoder_config": {"rerank_max_doc_chars": 1, "search_candidate_floor": 1,
                           "embedder_url": "http://e", "reranker_url": "http://r",
                           "cpu_encoder_replicas": "1", "gpu_encoder_replicas": "1"},
    },
    "probe": {"reranker_chars_per_s": 1, "reranker_status": "ok",
              "embedder_chars_per_s": 1, "probed_at": "2026-08-28T00:00:00+00:00",
              "reranker_measured_at": "2026-08-28T00:00:00+00:00",
              "embedder_measured_at": "2026-08-28T00:00:00+00:00",
              "probe_stale": False},
    "derived": {"s_mean_s": 1.0, "s_max_measured_s": 1.0, "s_mean_measured_s": 1.0,
                "payload_basis": "measured", "payload_basis_sample_count": 1,
                "payload_mean_chars_measured": 1.0, "payload_max_chars_measured": 1,
                "client_ceiling_s": 1.0, "queue_bound": 1, "tolerable_wait_s": 1.0,
                "single_search_exceeds_wait": False,
                "recommended_reranker_mem_limit_bytes": 1},
}

_FULL_CONSOLIDATION = {
    "stalled": False,
    "graph_invalid_nodes": 0,
    "project_identity": {"nodes": 1, "unidentified": 0, "mismatched": 0,
                         "unregistered": 0, "complete": True},
    "domain_identity": {"nodes": 1, "registry_rows": 1, "unregistered": 0,
                        "mismatched": 0, "unattached": 0, "complete": True},
    "last_outcome": "success",
    "last_success_age_seconds": 10,
    "last_success_cycle_type": "insight",
    "stalled_types": [],
    "inference_busy": "idle",
    "gpu_probe": {"state": "ok", "consecutive_hangs": 0, "leaked_children": 0},
    "fresh": True,
}


class _OkResp:
    status = 200


class _OkCm:
    async def __aenter__(self):
        return _OkResp()

    async def __aexit__(self, *a):
        return False


class _OkSession:
    def get(self, url, timeout=None, headers=None, **_kw):
        return _OkCm()


class _StubCoordinator:
    pgvector_version = "0.8.6"
    hnsw_iterative_scan = True
    _axis_registry_read_failures = 0

    def consolidation_health(self):
        return dict(_FULL_CONSOLIDATION)

    def dependency_snapshot(self):
        return {
            "postgres": {"state": "ok", "reason": None},
            "neo4j": {"state": "ok", "reason": None},
            "outbox": {"pending": 0, "in_progress": 0, "applied": 1, "failed": 0,
                       "rem_reviewed": 0, "oldest_failed_age_s": None,
                       "oldest_pending_age_s": None},
            "rem": {"dead_lettered": 0},
            "nrem": {"fact_cycles": 1, "decision_cycles": 1, "total_cycles": 2,
                     "fact_threshold": 3},
            "as_of": "2026-08-28T00:00:00+00:00",
            "fresh": True,
        }


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://localhost:5000")
    monkeypatch.setenv("AGENT_TOKENS", "claude:tok_contract_test")
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    monkeypatch.setattr(g, "capability_snapshot", lambda: dict(_FULL_CAPABILITY))
    monkeypatch.setattr(g, "capacity_snapshot", lambda: json.loads(json.dumps(_FULL_CAPACITY)))
    g._daemon_healthy = True
    g._rem_healthy = True
    return g


def _health_payload(g):
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _OkSession()

    async def _run():
        return await g._build_health_checks(proxy, _StubCoordinator())

    checks = asyncio.run(_run())
    # The two identity keys are added by handle_health for an authenticated
    # caller, not by the probe — supplied here so the completeness direction
    # covers them like any other key.
    checks["agent"] = "claude"
    checks["role"] = "write"
    return checks


def test_every_key_health_emits_is_documented(gateway):
    payload = _health_payload(gateway)
    undocumented = sorted(
        p for p in tc.canonical_paths(payload, tc.HEALTH) if p not in tc.HEALTH
    )
    assert not undocumented, (
        "/health emits keys the contract does not document — document them in "
        f"telemetry_contract.HEALTH and regenerate the doc: {undocumented}")


def test_every_documented_health_key_is_emitted(gateway):
    payload = _health_payload(gateway)
    emitted = tc.canonical_paths(payload, tc.HEALTH)
    missing = sorted(tc.required_paths(tc.HEALTH, "health") - emitted)
    assert not missing, (
        "the contract documents /health keys that are not emitted — either the "
        f"key was dropped, or it belongs in CONDITIONAL: {missing}")


def test_every_health_value_has_the_documented_type(gateway):
    payload = _health_payload(gateway)
    wrong = []
    for path, value in tc.walk_payload(payload, tc.HEALTH):
        spec = tc.HEALTH.get(path)
        if spec and not tc.type_matches(value, spec):
            wrong.append((path, tc._json_type(value), spec["types"]))
    assert not wrong, f"/health type mismatches: {wrong}"


def test_an_undocumented_health_key_fails_the_contract(gateway):
    """MUTATION CHECK, run as a test: this is the failure the two-way pin
    exists to produce, so it is proven here rather than assumed."""
    payload = _health_payload(gateway)
    payload["a_key_nobody_documented"] = 1
    undocumented = [p for p in tc.canonical_paths(payload, tc.HEALTH)
                    if p not in tc.HEALTH]
    assert undocumented == ["a_key_nobody_documented"]


def test_a_dropped_health_key_fails_the_contract(gateway):
    """The other direction, proven the same way."""
    payload = _health_payload(gateway)
    del payload["auth_scheme"]
    missing = tc.required_paths(tc.HEALTH, "health") - tc.canonical_paths(payload, tc.HEALTH)
    assert "auth_scheme" in missing


# ══════════════════════════════════════════════════════════════════════════════
# The invariants this release must not break
# ══════════════════════════════════════════════════════════════════════════════

def test_the_503_gate_is_encoders_and_nothing_else(gateway):
    """⛔ THE SAVE MANDATE, UNCHANGED BY v0.9.74. A degraded dependency, a
    failing outbox row, a dead-lettering REM daemon and a raised warning ALL
    leave the status code at 200 — only an encoder that does not answer makes it
    503. Widening this would turn every new signal into an outage for every
    client on every call."""
    g = gateway

    class _Coord(_StubCoordinator):
        def dependency_snapshot(self):
            snap = _StubCoordinator.dependency_snapshot(self)
            snap["postgres"] = {"state": "down", "reason": "OSError"}
            snap["outbox"] = {**snap["outbox"], "failed": 7}
            snap["rem"] = {"dead_lettered": 3}
            return snap

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _OkSession()

    async def _run():
        return await g._build_health_checks(proxy, _Coord())

    checks = asyncio.run(_run())
    # The verdict is honest…
    assert checks["status"] == "down"
    assert checks["dependencies"]["postgres"]["state"] == "down"
    assert checks["dependencies"]["outbox"]["state"] == "degraded"
    assert checks["dependencies"]["rem_daemon"]["state"] == "degraded"
    # …and the encoders, which are what the code speaks for, are fine.
    assert checks["dependencies"]["embedder"]["state"] == "ok"
    assert checks["dependencies"]["reranker"]["state"] == "ok"

    req = MagicMock()
    req.headers = {"Authorization": "Bearer tok_contract_test"}
    req.app = {"proxy": proxy, "coordinator": _Coord()}
    resp = asyncio.run(g.handle_health(req))
    assert resp.status == 200, (
        "a non-encoder dependency must never produce a 503 — the save mandate "
        "is the encoders and nothing else")


def test_a_down_encoder_still_produces_503(gateway):
    """The other half of the same invariant."""
    g = gateway

    class _DownResp:
        status = 500

    class _DownCm:
        async def __aenter__(self):
            return _DownResp()

        async def __aexit__(self, *a):
            return False

    class _DownSession:
        def get(self, url, timeout=None, headers=None, **_kw):
            return _DownCm()

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _DownSession()
    req = MagicMock()
    req.headers = {"Authorization": "Bearer tok_contract_test"}
    req.app = {"proxy": proxy, "coordinator": _StubCoordinator()}
    resp = asyncio.run(g.handle_health(req))
    assert resp.status == 503


def test_the_anonymous_payload_is_still_exactly_three_keys(gateway):
    """decision:1333, unchanged. `dependencies` and `warnings` describe this
    deployment's infrastructure; an unauthenticated peer learns the VERDICT and
    nothing else."""
    g = gateway
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _OkSession()
    req = MagicMock()
    req.headers = {}
    req.app = {"proxy": proxy, "coordinator": _StubCoordinator()}
    body = json.loads(asyncio.run(g.handle_health(req)).body.decode())
    assert set(body) == set(tc.ANONYMOUS_HEALTH_KEYS)


def test_health_makes_no_database_call_at_request_time(gateway):
    """ADR-018 / decision:362, and the reason the 60 s refresher exists at all.
    /health is hit on every client call and every 30 s by an open dashboard.

    Proven by giving the coordinator a pool and a driver that RAISE if anything
    touches them: a probe that reached the database would fail the test rather
    than merely be slow.

    ⚠ THE TRIPWIRE IS A BaseException, AND THAT IS THE WHOLE POINT. A plain
    AssertionError is silently swallowed by the `except Exception` guards
    `_build_health_checks` quite rightly wraps its snapshot reads in — a
    mutation check proved it: inserting a real `coordinator._pool.acquire`
    inside that try left this test GREEN. An invariant whose violation is
    caught by the code under test is not an invariant, it is a wish.
    """
    g = gateway

    class _DatabaseWasTouched(BaseException):
        """Deliberately NOT an Exception subclass — see this test's docstring."""

    class _Exploding:
        def __getattr__(self, name):
            raise _DatabaseWasTouched(
                f"/health touched the database at request time (via .{name})")

    class _Coord(_StubCoordinator):
        _pool = _Exploding()
        _neo4j = _Exploding()

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _OkSession()

    async def _run():
        return await g._build_health_checks(proxy, _Coord())

    checks = asyncio.run(_run())
    assert checks["status"] in ("ok", "degraded", "down")


def test_a_never_probed_dependency_reads_unknown_and_never_elevates(gateway):
    """decision:374 / fact:375 — a never-probed dependency that reads healthy is
    the failure this whole layer exists to avoid. It must also not read as
    BROKEN: a gateway that reported `degraded` for its first 60 seconds after
    every restart would train an operator to ignore the field."""
    g = gateway

    class _Coord(_StubCoordinator):
        def dependency_snapshot(self):
            return {"postgres": None, "neo4j": None, "outbox": None,
                    "registry": None, "rem": None, "nrem": None,
                    "as_of": None, "fresh": False}

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _OkSession()

    async def _run():
        return await g._build_health_checks(proxy, _Coord())

    checks = asyncio.run(_run())
    assert checks["dependencies"]["postgres"]["state"] == "unknown"
    assert checks["dependencies"]["outbox"]["state"] == "unknown"
    assert checks["status"] == "ok", "unknown must not elevate the overall status"


def test_the_old_key_names_are_still_emitted_this_release(gateway):
    """ADDITIVE RELEASE. Every key that existed at v0.9.73 keeps being emitted
    so the monitor migrates without a flag day; 0.9.75 drops the copies."""
    payload = _health_payload(gateway)
    for old in ("daemon", "rem_daemon", "llm_pool", "llm_affinity", "llm_routing",
                "llm_token_usage", "llm_latency", "config", "capacity",
                "project_identity", "domain_identity", "gpu_probe", "pgvector",
                "graph_invalid_nodes"):
        assert old in payload, f"{old} was removed rather than dual-emitted"
    assert payload["daemon"] == payload["nrem_daemon_process"]
    assert payload["rem_daemon"] == payload["rem_daemon_process"]


def test_an_admin_token_is_reported_as_admin_not_write(gateway):
    """RE-RULED at v0.9.74: reporting `write` told an admin caller it may save
    and then 403'd it on every write route."""
    g = gateway
    g._AGENT_ROLES["adminbot"] = "admin"
    assert g._health_role_for("adminbot") == "admin"


# ══════════════════════════════════════════════════════════════════════════════
# TWO-WAY: /memory/telemetry
# ══════════════════════════════════════════════════════════════════════════════

class _FakeRow(dict):
    """A dict that behaves like an asyncpg Record — and RAISES on a key nobody
    planned for, rather than returning None.

    ⛔ THE RAISE IS THE POINT. A fake that answers every column with None lets a
    builder read a column that does not exist and quietly emit a null, which is
    the exact class of defect this fix round exists for: `_registry_census`
    selected FROM a table that does not exist and nothing noticed, because every
    layer above it tolerated an absence.
    """

    def __missing__(self, key):
        raise KeyError(
            f"the fake connection was asked for column {key!r}, which no "
            f"planned row provides — add it to _PG_PLAN, do not let the "
            f"builder read a hole")


class _FakeConn:
    """A Postgres stand-in that DISPATCHES ON THE QUERY and returns rows of the
    documented shape, so every telemetry section runs its REAL builder.

    Replaces the AsyncMock-per-builder the first draft used. A mocked builder
    makes the two-way pin vacuous for that section: the payload comes from the
    mock, so the contract is only ever compared against the test's own
    imagination of the code. Two mutation checks proved it — deleting a key from
    a real builder changed nothing, because no real builder ran.

    An unrecognised query RAISES. A fake that silently returned [] for anything
    it did not know would let a whole section degrade to `{"error": ...}`, and
    the completeness direction would then simply skip it.
    """

    def __init__(self, plan: dict):
        self._plan = plan

    def _match(self, sql: str, kind: str):
        flat = " ".join(sql.split())
        for needle, value in self._plan.get(kind, []):
            if needle in flat:
                return value
        raise AssertionError(
            f"the fake connection has no planned {kind} for this SQL — add one "
            f"rather than letting the section fail into an error branch:\n{flat[:300]}")

    async def fetchrow(self, sql, *args):
        return self._match(sql, "fetchrow")

    async def fetchval(self, sql, *args):
        return self._match(sql, "fetchval")

    async def fetch(self, sql, *args):
        return self._match(sql, "fetch")

    async def execute(self, sql, *args):
        return "SELECT 0"

    async def executemany(self, sql, *args):
        return None


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    async def data(self):
        return list(self._rows)

    async def single(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """Neo4j stand-in, same dispatch-or-raise contract as _FakeConn."""

    def __init__(self, plan):
        self._plan = plan

    async def run(self, cypher, **params):
        flat = " ".join(cypher.split())
        for needle, rows in self._plan:
            if needle in flat:
                return _FakeResult(rows)
        raise AssertionError(
            f"the fake session has no planned result for this Cypher — add one "
            f"rather than letting the section fail into an error branch:\n{flat[:300]}")


_PER_TYPE_ROLLUP = {
    "last_outcome": "success", "last_success_age_seconds": 1,
    "in_flight": False, "consecutive_failures": 0, "backlog": 0,
    "stalled": False,
    "last_error": {"class": "X", "msg": "m", "age_seconds": 1,
                   "superseded": False},
    "eligible_clusters": 0, "eligible_oldest_age_seconds": None,
    "dead_lettered_clusters": 0, "unchanged_clusters": 0,
    "singleton_clusters": 0, "truncation_failures": 0, "slot_failures": 0,
    "last_deferred_reason": None, "cycle_seconds_avg": 1.0, "runs_24h": 1,
    "deferred_24h": 0, "idle_24h": 0, "folds_succeeded_24h": 1,
    "folds_attempted_24h": 1, "truncation_failures_24h": 0,
    "slot_failures_24h": 0, "last_started": "2026-08-28T00:00:00+00:00",
}

#: Every Postgres query the telemetry payload makes, keyed by a distinctive
#: fragment, with a row of the DOCUMENTED shape. Ordered — first match wins.
_PG_PLAN = {
    "fetchrow": [
        ("FILTER (WHERE status='pending')", _FakeRow(
            pending=1, in_progress=0, applied=4, failed=0, rem_reviewed=2,
            oldest_failed_age_s=None, oldest_pending_age_s=12)),
        ("applied_at - created_at", _FakeRow(
            n=4, p50=1.5, p95=2.5, applied_last_min=2)),
        ("FROM project_domains)::int AS domains", _FakeRow(
            projects=38, domains=20, aliases=18)),
        ("rem_timing->>'ts'", _FakeRow(n=7)),
        ("records_with_domains", _FakeRow(
            records_total=1691, records_with_domains=629)),
        ("count(*) FILTER (WHERE metadata->>'kind'='insight')", _FakeRow(
            total=3, superseded=1, insight=1)),
        ("count(*) AS total, count(*) FILTER (WHERE superseded) AS superseded FROM technical_docs",
         _FakeRow(total=1691, superseded=40)),
        ("metadata->'decision' ? 'alternatives'", _FakeRow(
            n=10, grounded=9, alts=8, conf=7, elicited=6)),
        ("NOT IN ('decision', 'retrospective')", _FakeRow(
            n=100, sref=90, elicited=80)),
        ("metadata ? 'target_pg_id'", _FakeRow(
            n=5, rating=5, target=5, grounded=4, elicited=3)),
        ("decision_alternatives", _FakeRow(
            entries=12, decisions=6, embedded=12, pending=0, failing=0,
            oldest_pending_age_s=None)),
        ("FROM consolidation_runs", _FakeRow(n=3, p50=12.0, p95=30.0)),
        ("count(*) FROM refold_ledger", _FakeRow(n=0)),
    ],
    "fetchval": [
        ("FROM neo4j_outbox WHERE status='failed'", None),
        ("count(*) FROM refold_ledger", 0),
    ],
    "fetch": [
        ("FROM neo4j_outbox GROUP BY status", [
            _FakeRow(status="applied", n=4), _FakeRow(status="pending", n=1)]),
        ("metadata->>'type','(untagged)'", [
            _FakeRow(key="fact", count=900), _FakeRow(key="decision", count=200)]),
        ("agent_id AS key", [_FakeRow(key="claude", count=500)]),
        ("metadata->>'source','(none)'", [_FakeRow(key="claude", count=500)]),
        ("jsonb_array_elements_text", [
            _FakeRow(key="architecture", count=301),
            _FakeRow(key="operations", count=184)]),
        ("FROM community_summaries GROUP BY 1", [
            _FakeRow(kind="insight", superseded=0, active=2)]),
        ("AS key, count(*)::int AS count FROM technical_docs GROUP BY 1", [
            _FakeRow(key="shared-memory-GitHub", count=800)]),
        ("jsonb_object_keys(metadata) k", [_FakeRow(k="elicited", n=50)]),
        ("rem_timing->>'model' AS model", [_FakeRow(
            model="qwen3-14b", n=12, n_service=10, svc_p50=100.0, svc_p95=200.0,
            con_p50=1.0, con_p95=2.0, wall_p50=110.0, wall_p95=220.0,
            max_batch=4, backend="http://localhost:5000")]),
        ("GROUP BY status, closed_reason", [
            _FakeRow(status="dropped", closed_reason="out_of_scan", n=37)]),
        ("GROUP BY trigger_kind", [_FakeRow(trigger_kind="technical_docs", n=43)]),
        ("status = 'open' AND summary_kind = 'insight'", []),
    ],
}

_NEO4J_PLAN = [
    ("AND coalesce(n.rem_attempts,0) >= $cap", [{"n": 0}]),
    ("RETURN coalesce(n.rem_attempts,0) AS a", [
        {"a": 0, "p": 0, "n": 40}, {"a": 5, "p": 0, "n": 0}]),
    ("MATCH (f:Fact) WHERE f.pg_id IS NOT NULL", [
        {"rem": True, "con": True, "superseded": False, "n": 900}]),
    ("MATCH (d:Decision) RETURN coalesce(d.rem_processed,false)", [
        {"rem": True, "superseded": False, "n": 200}]),
    ("RETURN count(e) AS total", [{
        "total": 300, "orphans": 2, "unmentioned": 5, "singletons": 40,
        "genuinely_referenced": 250}]),
    ("ORDER BY degree DESC LIMIT 8", [{"name": "outbox", "degree": 30}]),
    ("MATCH ()-[r]->() RETURN type(r) AS name", [
        {"name": "MENTIONS", "c": 6000}, {"name": "ALIASES", "c": 3}]),
    ("UNWIND labels(n) AS l", [
        {"name": "Fact", "c": 900}, {"name": "Decision", "c": 200}]),
    ("coalesce(n.rem_invalid, false) = true", [
        {"label": "Fact", "reason": "label_mismatch", "c": 1}]),
]


def _stub_telemetry_coordinator(g, pg_plan=None, neo4j_plan=None):
    """A coordinator wired to the fakes above, running EVERY real builder."""
    import coordinator as co
    c = co.MemoryCoordinator()
    c._pool = MagicMock()
    c._pool.get_size = MagicMock(return_value=10)
    c._pool.get_idle_size = MagicMock(return_value=7)
    c.pgvector_version = "0.8.6"
    c.hnsw_iterative_scan = True
    c.telemetry_extras_provider = g.telemetry_extras

    conn = _FakeConn(pg_plan or _PG_PLAN)
    c._acquire = MagicMock(return_value=_async_ctx(conn))
    session = _FakeSession(neo4j_plan or _NEO4J_PLAN)
    c._neo4j = MagicMock()
    c._neo4j.session = MagicMock(return_value=_async_ctx(session))

    c._consolidation_health = dict(_FULL_CONSOLIDATION)
    c._dependency_health = _StubCoordinator().dependency_snapshot()

    # The per-cycle-type rollup is left stubbed: modelling
    # _compute_consolidation_health's windowed SQL here would reimplement that
    # function's own test rather than pin this contract.
    c._consolidation_telemetry = AsyncMock(return_value={
        "stall_threshold_seconds": 900,
        "insight": dict(_PER_TYPE_ROLLUP),
        "fact_consolidation": dict(_PER_TYPE_ROLLUP),
        "stalled": False, "stalled_types": [], "last_success_age_seconds": 1,
        "last_success_cycle_type": "insight", "last_outcome": "success",
        "last_deferred_reason": None, "last_active_cycle_type": "insight",
    })
    return c


def _async_ctx(value):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _telemetry_payload(g):
    # Through the CACHE, not _build_telemetry directly: `timestamp` is stamped
    # at SERVE time, so a test that bypassed the cache would be checking a
    # payload no client can ever receive.
    c = _stub_telemetry_coordinator(g)

    async def _run():
        # ⛔ THE REGISTRY NUMBERS COME THROUGH THE REAL CENSUS (F3), via the real
        # refresher step, not hand-crafted ints. Hand-crafting them is how a
        # census query naming a table that does not exist stayed green in every
        # test while failing on every install.
        await c._refresh_registry_census()
        return await c._telemetry_cached()

    return asyncio.run(_run())


def test_every_key_telemetry_emits_is_documented(gateway):
    payload = _telemetry_payload(gateway)
    undocumented = sorted(
        p for p in tc.canonical_paths(payload, tc.TELEMETRY) if p not in tc.TELEMETRY
    )
    assert not undocumented, (
        "/memory/telemetry emits keys the contract does not document — document "
        f"them in telemetry_contract.TELEMETRY and regenerate the doc: {undocumented}")


def test_every_documented_telemetry_key_is_emitted(gateway):
    payload = _telemetry_payload(gateway)
    emitted = tc.canonical_paths(payload, tc.TELEMETRY)
    missing = sorted(tc.required_paths(tc.TELEMETRY, "telemetry") - emitted)
    assert not missing, (
        "the contract documents /memory/telemetry keys that are not emitted — "
        f"either the key was dropped, or it belongs in CONDITIONAL: {missing}")


def test_every_telemetry_value_has_the_documented_type(gateway):
    payload = _telemetry_payload(gateway)
    wrong = []
    for path, value in tc.walk_payload(payload, tc.TELEMETRY):
        spec = tc.TELEMETRY.get(path)
        if spec and not tc.type_matches(value, spec):
            wrong.append((path, tc._json_type(value), spec["types"]))
    assert not wrong, f"/memory/telemetry type mismatches: {wrong}"


def test_outbox_failed_is_present_even_when_it_is_zero(gateway):
    """⛔ ABSENCE IS NOT ZERO — the defect the `outbox` section exists to fix.
    The pre-0.9.74 `postgres.outbox` census was a GROUP BY, so the one key a
    consumer most needs to read went missing exactly when it was healthy.

    ⚠ THIS EXERCISES THE REAL `_outbox_telemetry`, not the stub the contract
    fixtures use. A mutation check proved why: re-introducing the omission
    (`**({"failed": n} if n else {})`) left the payload-level assertion GREEN,
    because that payload came from an AsyncMock that supplies the key
    unconditionally. A test whose instrument answers the question for it is not
    a test of the code (fact:1321).
    """
    import coordinator as co
    c = co.MemoryCoordinator()

    zero_census = {"pending": 0, "in_progress": 0, "applied": 0, "failed": 0,
                   "rem_reviewed": 0, "oldest_failed_age_s": None,
                   "oldest_pending_age_s": None}
    c._outbox_census = AsyncMock(return_value=zero_census)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"n": 0, "p50": None, "p95": None,
                                            "applied_last_min": 0})
    c._acquire = MagicMock(return_value=_async_ctx(conn))

    section = asyncio.run(c._outbox_telemetry())
    for key in ("pending", "applied", "failed", "rem_reviewed"):
        assert key in section, f"{key} vanished at zero — absence is not zero"
        assert section[key] == 0
    # A rate over an empty window is not a measured zero.
    assert section["drain_rate_per_min"] is None
    assert section["apply_latency_p50_s"] is None
    # The limit travels with the number it bounds.
    assert section["age_limit_s"] == co.OUTBOX_AGE_WARN_S


def test_the_nrem_walk_is_served_from_the_refresher_not_the_request(gateway):
    """MEASURED (2026-08-28, this corpus): the insight walk is 149 sequential
    Neo4j round-trips and is unbounded by construction. It must not run per
    request, and the served value must say when it was computed."""
    c = _stub_telemetry_coordinator(gateway)
    c._nrem_cycle_counts = AsyncMock(
        side_effect=AssertionError("the nrem walk ran inside a telemetry request"))
    payload = asyncio.run(c._build_telemetry())
    assert payload["nrem"]["fact_cycles"] == 1
    assert payload["nrem"]["as_of"] == "2026-08-28T00:00:00+00:00"


def test_the_payload_is_cached_and_says_when_it_was_built(gateway):
    """A cached payload is served STALE on purpose; `generated_at` is the build
    time and `timestamp` the serve time, so a reader can tell how stale."""
    c = _stub_telemetry_coordinator(gateway)
    first = asyncio.run(c._telemetry_cached())
    c._build_telemetry = AsyncMock(
        side_effect=AssertionError("rebuilt inside the cache window"))
    second = asyncio.run(c._telemetry_cached())
    assert second["generated_at"] == first["generated_at"]
    assert "timestamp" in second


def test_the_alias_metrics_with_no_writer_are_gone(gateway):
    """REMOVED, not moved: all four could only ever read 0, and the name
    collided with the LIVE alias tables."""
    payload = _telemetry_payload(gateway)
    for gone in ("alias_edges", "alias_covered_entities", "alias_components",
                 "largest_alias_component"):
        assert gone not in payload["entity_graph"]


def test_breakdown_projects_and_domains_are_now_different_questions(gateway):
    """The enumerated meaning change (fact:1626): `breakdown.domains` used to
    carry the PROJECT distribution."""
    payload = _telemetry_payload(gateway)
    assert payload["breakdown"]["projects"] == [
        {"key": "shared-memory-GitHub", "count": 800}]
    assert payload["breakdown"]["domains"] == [
        {"key": "architecture", "count": 301}, {"key": "operations", "count": 184}]
    # F8: the denominator ships WITH the distribution. 301+184 against 1691
    # records is not "records are missing" — it is 1062 records carrying no
    # `domains` key at all, which only these two numbers can say.
    assert payload["breakdown"]["records_with_domains"] == 629
    assert payload["breakdown"]["records_total"] == 1691


def test_the_meaning_change_list_covers_every_re_pointed_key():
    """fact:1626 — a key whose VALUE changes meaning while its NAME stays is
    ENUMERATED. A consumer reading only the key list would see nothing wrong."""
    paths = {(m["endpoint"], m["path"]) for m in tc.MEANING_CHANGES}
    assert ("telemetry", "breakdown.domains") in paths
    assert ("health", "role") in paths
    assert ("health", "llm_backends.*") in paths
    assert ("health", "status") in paths
    # W2 (decision:1832) — the fleet-visibility meaning changes.
    assert ("health", "dependencies.llm_pool.state") in paths
    assert ("health", "dependencies.rem_daemon.state") in paths
    assert ("health", "dependencies.nrem_daemon.state") in paths

    by_path = {(m["endpoint"], m["path"]): m for m in tc.MEANING_CHANGES}
    # The four 0.9.74 entries (:910-913 above) remain MANDATORY at their own
    # frozen stamp — they never get bumped just because VERSION moved on.
    for key in (("telemetry", "breakdown.domains"), ("health", "role"),
                ("health", "llm_backends.*"), ("health", "status")):
        assert by_path[key]["in_version"] == tc.INTRODUCED_0_9_74, (
            f"{key} must stay pinned at INTRODUCED_0_9_74, never re-dated")
    # W2's three new entries carry THIS release, exactly.
    for key in (("health", "dependencies.llm_pool.state"),
               ("health", "dependencies.rem_daemon.state"),
               ("health", "dependencies.nrem_daemon.state")):
        assert by_path[key]["in_version"] == tc.VERSION, (
            f"{key} is new in W2 and must carry tc.VERSION exactly")

    for mc in tc.MEANING_CHANGES:
        # DURABLE INVARIANT (fix round item 8, decision:1832): a hardcoded
        # two-constant allowlist (VERSION, INTRODUCED_0_9_74) EXPIRES the
        # moment a THIRD release adds an entry — the frozen W2 entries above
        # would then equal neither "today's VERSION" nor INTRODUCED_0_9_74
        # and this loop would start failing on records nobody touched. The
        # invariant that survives every future release without editing:
        # no entry may claim a version LATER than the one asserting it here
        # (compared as a TUPLE, never a string — "0.9.9" > "0.9.10" as
        # strings). The specific per-entry checks above catch a genuinely
        # NEW entry landing with a STALE stamp; this general bound catches
        # the nonsensical direction (an entry from the future).
        assert tc._version_tuple(mc["in_version"]) <= tc._version_tuple(tc.VERSION), (
            f"{mc['path']!r} claims in_version {mc['in_version']!r}, which is "
            f"AFTER the current release {tc.VERSION!r}")
        assert mc["was"] and mc["now"] and mc["action"]


def test_dual_emit_drop_target_is_strictly_after_this_release():
    """Fix round item 1c (decision:1832): the target cannot name a release
    that has already happened — DUAL_EMIT_DROP_TARGET must be STRICTLY
    greater than VERSION, compared as a version TUPLE (a string compare
    would rank "0.9.9" ahead of "0.9.10")."""
    assert (tc._version_tuple(tc.DUAL_EMIT_DROP_TARGET)
            > tc._version_tuple(tc.VERSION))


# ══════════════════════════════════════════════════════════════════════════════
# THE CLIENT SIDE (Group 1) — a write-side change is half a fix
# ══════════════════════════════════════════════════════════════════════════════

def test_the_client_renders_the_gateway_s_verdict_not_its_own():
    """⛔ THE THRESHOLD LIVES SERVER-SIDE. This function only renders what it
    was told — it must not recompute a verdict from the numbers, which is
    exactly what every consumer used to do independently."""
    import memory_bridge as mb
    lines = mb.format_health_verdict({
        "status": "degraded",
        "dependencies": {
            "postgres": {"state": "ok", "reason": None},
            "outbox": {"state": "degraded", "reason": "failed:3"},
        },
        "warnings": [{"key": "outbox_oldest_pending_age_s", "limit": 3600,
                      "observed": 7200, "unit": "s"}],
    })
    body = "\n".join(lines)
    assert "gateway: degraded" in body
    assert "outbox: degraded (failed:3)" in body
    assert "⚠ outbox_oldest_pending_age_s: 7200 > 3600 s" in body
    # A healthy dependency is not listed one by one — but its absence must not
    # read as "nothing was checked".
    assert "postgres" not in body


def test_the_client_says_how_many_dependencies_were_checked_when_all_are_ok():
    import memory_bridge as mb
    lines = mb.format_health_verdict({
        "status": "ok",
        "dependencies": {"a": {"state": "ok"}, "b": {"state": "ok"}},
        "warnings": [],
    })
    assert "all 2 dependencies ok" in "\n".join(lines)


def test_an_older_gateway_contributes_no_verdict_rather_than_a_wrong_one():
    """A pre-0.9.74 gateway sends no `dependencies`. That is not an error — it
    is a server that cannot answer the question yet, and inventing a verdict on
    its behalf would be worse than saying nothing."""
    import memory_bridge as mb
    assert mb.format_health_verdict({"status": "ok"}) == []
    assert mb.format_health_verdict(None) == []
    assert mb.format_health_verdict({"status": "ok", "dependencies": "nonsense"}) == []


def test_the_client_keeps_the_body_of_a_503_health_reply():
    """⚠ /health answers 503 when an encoder is down, and THAT response carries
    the verdict. A decoder that treated every non-2xx as an error would discard
    the payload in exactly the state an operator runs `status` to see — while
    still obeying fact:1503, because 503 is ENUMERATED rather than decoded
    blindly."""
    import memory_bridge as mb

    class _R:
        status_code = 503

        @staticmethod
        def json():
            return {"status": "down", "dependencies": {"embedder": {"state": "down"}}}

    assert mb._reply_json(_R(), accept_status=(503,))["status"] == "down"


def test_an_unenumerated_status_is_still_an_error_not_a_decode():
    """The other half: `accept_status` narrows the exemption to what the caller
    named, so the fact:1503 class cannot come back through this door."""
    import memory_bridge as mb

    class _R:
        status_code = 502
        text = "502: Bad Gateway"

        @staticmethod
        def json():
            raise ValueError("not json")

    with pytest.raises(mb.GatewayReplyError):
        mb._reply_json(_R(), accept_status=(503,))


def test_both_front_doors_send_the_same_client_build_header():
    """Group 1 parity: a header only one front door sends produces a
    `clients.versions_seen` census that silently omits half the fleet."""
    import memory_bridge as mb
    vs = Path(__file__).resolve().parents[1] / "mcp" / "vector-skill.py"
    src = vs.read_text()
    assert mb.CLIENT_BUILD_HEADER == "X-Shared-Memory-Client"
    assert 'CLIENT_BUILD_HEADER = "X-Shared-Memory-Client"' in src
    assert "CLIENT_BUILD_HEADER: VERSION" in src


def test_the_two_bridge_copies_stay_byte_identical():
    """The tracked skill copy is what sync_skills.sh ships; a change to one and
    not the other fails silently on every installed client."""
    root = Path(__file__).resolve().parents[1]
    a = root / "shared-memory" / "scripts" / "memory_bridge.py"
    b = root / "shared-memory-skill" / "shared-memory" / "scripts" / "memory_bridge.py"
    assert a.read_text() == b.read_text()


# ══════════════════════════════════════════════════════════════════════════════
# FIX ROUND — F1: the registry census
# ══════════════════════════════════════════════════════════════════════════════

def test_the_registry_census_names_tables_that_exist():
    """⛔ THE DEFECT THIS FILE MISSED FIRST TIME ROUND. The census selected
    `FROM domains` — a table that does not exist and never did — so it raised
    UndefinedTableError on every install, the refresher swallowed it, and
    `registry.*` was null forever with nothing saying why.

    Pinned against the SHIPPED schema rather than a list retyped here, so a
    table renamed in a migration breaks this test instead of the endpoint.
    """
    import re
    import coordinator as co
    schema = (Path(__file__).resolve().parents[1] / "shared-memory" /
              "migrations" / "schema_init.sql").read_text()
    real = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", schema))
    # ⚠ THE DOCSTRING IS STRIPPED FIRST. It NAMES the dead `domains` table in
    # order to explain the defect, so a naive scan of the source would flag the
    # explanation and pass a query that still had the bug.
    fn = ast.parse(inspect.getsource(co.MemoryCoordinator._registry_census).lstrip())
    body = fn.body[0]
    stmts = body.body[1:] if ast.get_docstring(body) else body.body
    sql = " ".join(
        node.value for stmt in stmts for node in ast.walk(stmt)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    named = set(re.findall(r"FROM (\w+)", sql))
    unknown = named - real
    assert not unknown, f"the census selects from tables that do not exist: {unknown}"
    assert {"projects", "project_domains", "project_aliases", "domain_aliases"} <= named


@pytest.mark.asyncio
async def test_a_failed_census_is_counted_logged_once_and_serves_the_last_good(caplog):
    """A probe that CANNOT RUN must never look like a probe that has not run yet
    — and it must reach /health, not only the log."""
    import logging
    import coordinator as co
    c = co.MemoryCoordinator()

    c._acquire = MagicMock(return_value=_async_ctx(_FakeConn(_PG_PLAN)))
    await c._refresh_registry_census()
    assert c._registry_census_last_good == {"projects": 38, "domains": 20, "aliases": 18}
    assert c._registry_census_failures == 0
    good_as_of = c._registry_census_as_of
    assert good_as_of is not None

    class _Broken:
        async def fetchrow(self, sql, *a):
            raise RuntimeError('relation "domains" does not exist')

    c._acquire = MagicMock(return_value=_async_ctx(_Broken()))
    with caplog.at_level(logging.WARNING, logger="coordinator"):
        await c._refresh_registry_census()
        await c._refresh_registry_census()
        await c._refresh_registry_census()

    # Counted every time…
    assert c._registry_census_failures == 3
    # …logged ONCE, at the transition. /health refreshes every 60 s; a line per
    # tick is a log nobody reads.
    lines = [r for r in caplog.records if "health.registry" in r.getMessage()]
    assert len(lines) == 1, f"expected one transition line, got {len(lines)}"
    assert "RuntimeError" in lines[0].getMessage()
    assert "domains" in lines[0].getMessage()
    # …and the numbers an operator was watching survive.
    assert c._registry_census_last_good == {"projects": 38, "domains": 20, "aliases": 18}
    assert c._registry_census_as_of == good_as_of

    section = c._registry_telemetry()
    assert section["projects"] == 38
    assert section["as_of"] == good_as_of
    assert "relation" in section["error"]
    assert section["census_failures_total"] == 3


def test_a_failed_census_degrades_the_registry_dependency():
    """F1's actual requirement: the failure must reach /health. It previously
    could not — the enum read a different counter entirely (the SEARCH path's),
    so a dead census left the dependency reading `ok`."""
    import hive_mind_proxy as g
    assert g._registry_dependency(0, 0)["state"] == "ok"
    degraded = g._registry_dependency(0, 4)
    assert degraded["state"] == "degraded"
    assert "census_failures:4" in degraded["reason"]
    # The two counters name themselves, because they have different fixes.
    both = g._registry_dependency(2, 4)
    assert "read_failures:2" in both["reason"] and "census_failures:4" in both["reason"]


def test_registry_counts_are_never_null(gateway):
    """F3: no null path. Before the first census they are 0 with `as_of: null`
    — "nothing counted yet", not "nothing exists"."""
    payload = _telemetry_payload(gateway)
    for key in ("projects", "domains", "aliases"):
        assert isinstance(payload["registry"][key], int)
    spec = tc.TELEMETRY["registry.projects"]
    assert "null" not in spec["types"], (
        "registry.projects is documented as nullable — either the null path "
        "came back, or the contract is describing one that no longer exists")


# ══════════════════════════════════════════════════════════════════════════════
# FIX ROUND — F4: the shed warning reported a value that could not have raised it
# ══════════════════════════════════════════════════════════════════════════════

def test_the_shed_warning_carries_the_value_that_raised_it(gateway, monkeypatch):
    """⛔ ASSERT THE VALUE, NOT JUST THE PRESENCE (fact:1309). `_gateway_shed_rate`
    MOVES ITS OWN WATERMARK, so calling it twice — once to test and once to
    report — measured the microseconds between the two calls and always reported
    `observed: 0`: a warning that fires and then denies itself."""
    g = gateway
    g._rate_marks.clear()
    seq = iter([{"shed_503_total": 0}, {"shed_503_total": 7}])
    monkeypatch.setattr(g, "telemetry_gateway_counters", lambda: next(seq))
    g._gateway_shed_rate()          # establish the watermark at 0

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _OkSession()

    async def _run():
        return await g._build_health_checks(proxy, _StubCoordinator())

    checks = asyncio.run(_run())
    shed = [w for w in checks["warnings"] if w["key"] == "gateway_shed_503_total"]
    assert shed, "the warning did not fire at all"
    assert shed[0]["observed"] == 7, (
        "the warning reported a value that could not have raised it — "
        "_gateway_shed_rate was called more than once")
    assert shed[0]["limit"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# FIX ROUND — F6/F7: the outbox apply is the OTHER Neo4j caller
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_failed_outbox_apply_counts_as_a_neo4j_failure():
    """F6. Counting only the graph route made `tx_failures_total` read "Neo4j is
    fine" through an outage that was failing every apply — and the write path is
    the one that actually blocks the pipeline."""
    import coordinator as co
    c = co.MemoryCoordinator()
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    c._acquire = MagicMock(return_value=_async_ctx(conn))

    class _DeadNeo4j:
        def session(self, **kw):
            raise RuntimeError("Neo4j unavailable")

    c._neo4j = _DeadNeo4j()
    c._project_identity = AsyncMock(return_value=None)
    c._domain_identities = AsyncMock(return_value=[])
    assert c._neo4j_tx_failures_total == 0
    await c._apply_outbox_row(1, 42, {"content_snippet": "x"}, 0)
    assert c._neo4j_tx_failures_total == 1
    # A rejection is the CALLER's fault, and there is no caller here.
    assert c._cypher_rejected_total == 0


@pytest.mark.asyncio
async def test_a_successful_outbox_apply_is_timed_into_the_neo4j_ring():
    """F7. B2 asked for both callers; timing only the graph route would make
    `neo4j.query_p95_ms` describe ad-hoc read Cypher while the write path stayed
    invisible."""
    import coordinator as co
    c = co.MemoryCoordinator()
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.executemany = AsyncMock()
    c._acquire = MagicMock(return_value=_async_ctx(conn))
    session = AsyncMock()
    session.run = AsyncMock(return_value=AsyncMock())
    c._neo4j = MagicMock()
    c._neo4j.session = MagicMock(return_value=_async_ctx(session))
    c._project_identity = AsyncMock(return_value=None)
    c._domain_identities = AsyncMock(return_value=[])

    assert c._neo4j_ring.snapshot()["window"] == 0
    await c._apply_outbox_row(1, 42, {"content_snippet": "x", "entities": []}, 0)
    assert c._neo4j_ring.snapshot()["window"] == 1
    assert c._neo4j_tx_failures_total == 0


# ══════════════════════════════════════════════════════════════════════════════
# FIX ROUND — F9 / F10 / F12 / the rem-timing guard / the drain rate
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_cancelled_acquire_is_not_a_pool_error():
    """F10. Cancellation — shutdown, a client disconnect, an outer timeout —
    says nothing about whether the pool could have served the request. Counting
    it would make an orderly restart look like a burst of database errors."""
    import coordinator as co
    ring = LatencyRing()

    class _Cancelling:
        async def __aenter__(self):
            raise asyncio.CancelledError()

        async def __aexit__(self, *a):
            return False

    with pytest.raises(asyncio.CancelledError):
        async with co._TimedAcquire(_Cancelling(), ring):
            pass
    assert ring.snapshot()["errors"] == 0

    class _Failing:
        async def __aenter__(self):
            raise RuntimeError("pool exhausted")

        async def __aexit__(self, *a):
            return False

    with pytest.raises(RuntimeError):
        async with co._TimedAcquire(_Failing(), ring):
            pass
    assert ring.snapshot()["errors"] == 1


def test_the_rem_timing_guard_rejects_a_non_numeric_ts():
    """⚠ THE GUARD IS A NO-OP ON THE DEVELOPMENT CORPUS — measured live
    2026-08-28: 188 of 188 rows carry a clean number — which is exactly why it
    needs a test. `rem_timing` is JSONB on a table with rows older than the
    writer that fills `ts`, and ONE unparseable value aborts the whole query,
    taking the REM section down rather than skipping a row.

    The pattern is tested here; it was ALSO run through Postgres' own regex
    against these strings on the live database, and the unguarded cast confirmed
    to raise InvalidTextRepresentationError.
    """
    import re
    import coordinator as co
    ok = re.compile(co.REM_TS_NUMERIC_RE)
    assert ok.match("1756377600")
    assert ok.match("1756377600.123")
    for bad in ("2026-08-28T00:00:00+00:00", "", "NaN", "null", "1e9", " 17"):
        assert not ok.match(bad), f"{bad!r} would reach the cast and abort the query"


@pytest.mark.asyncio
async def test_the_drain_rate_is_null_with_no_basis_and_zero_when_measured():
    """Both directions. A rate over an empty window is not a measured zero — but
    a zero over a real window IS a measurement, and nulling THAT would be the
    same absence-is-not-zero rule pointed backwards."""
    import coordinator as co
    c = co.MemoryCoordinator()
    c._outbox_census = AsyncMock(return_value={
        "pending": 0, "in_progress": 0, "applied": 0, "failed": 0,
        "rem_reviewed": 0, "oldest_failed_age_s": None,
        "oldest_pending_age_s": None})

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"n": 0, "p50": None, "p95": None,
                                            "applied_last_min": 0})
    c._acquire = MagicMock(return_value=_async_ctx(conn))
    assert (await c._outbox_telemetry())["drain_rate_per_min"] is None

    conn.fetchrow = AsyncMock(return_value={"n": 9, "p50": 1.0, "p95": 2.0,
                                            "applied_last_min": 0})
    assert (await c._outbox_telemetry())["drain_rate_per_min"] == 0.0


def test_the_neo4j_ring_is_snapshotted_once_per_payload():
    """F12. Three separate snapshot() calls sorted the ring three times and could
    observe three different windows — p50 and p95 would then describe
    populations that never coexisted, and `window` would name neither."""
    import coordinator as co
    src = inspect.getsource(co.MemoryCoordinator._build_telemetry)
    assert src.count("_neo4j_ring.snapshot()") == 1


def test_each_latency_ring_can_be_sized_on_its_own(monkeypatch):
    """F9. They shared ENCODER_LATENCY_WINDOW, so a name that said "encoder"
    silently sized three unrelated instruments: widening it to chase a slow
    reranker moved the Neo4j percentiles underneath themselves."""
    import importlib
    import coordinator as co
    monkeypatch.setenv("ENCODER_LATENCY_WINDOW", "11")
    monkeypatch.setenv("POOL_WAIT_WINDOW", "22")
    monkeypatch.setenv("NEO4J_LATENCY_WINDOW", "33")
    importlib.reload(co)
    try:
        assert (co.ENCODER_LATENCY_WINDOW, co.POOL_WAIT_WINDOW,
                co.NEO4J_LATENCY_WINDOW) == (11, 22, 33)
        # …and each defaults to the encoder window when unset, so an operator
        # who never heard of them keeps today's behaviour.
        monkeypatch.delenv("POOL_WAIT_WINDOW")
        monkeypatch.delenv("NEO4J_LATENCY_WINDOW")
        importlib.reload(co)
        assert co.POOL_WAIT_WINDOW == co.ENCODER_LATENCY_WINDOW == 11
    finally:
        for k in ("ENCODER_LATENCY_WINDOW", "POOL_WAIT_WINDOW", "NEO4J_LATENCY_WINDOW"):
            monkeypatch.delenv(k, raising=False)
        importlib.reload(co)
