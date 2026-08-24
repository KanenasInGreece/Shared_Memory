import pytest
pytest.importorskip("fastmcp")
import json
import asyncio
import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch, AsyncMock

# Dynamic load of vector-skill.py
def load_vector_skill():
    path = os.path.join(os.path.dirname(__file__), "..", "mcp", "vector-skill.py")
    spec = importlib.util.spec_from_file_location("vector_skill", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["vector_skill"] = module
    spec.loader.exec_module(module)
    return module

vector_skill = load_vector_skill()

# Mock data
MOCK_EMBEDDING = [0.1] * 1024
MOCK_PG_ID = 42
MOCK_CONTENT = "MCP test content"
MOCK_QUERY = "MCP test query"

@pytest.mark.asyncio
async def test_thin_client_owns_no_database_handles():
    """The whole point of the re-cut. This MCP server used to run its own copy of
    the retrieval chain against Postgres and Neo4j — which meant a second
    implementation of the read path, and therefore a second implementation of its
    ACCESS CONTROL that simply did not have any: the gateway filters every read on
    `visibility` (global / own private / matching scope), a direct
    `SELECT ... WHERE NOT superseded` filters on none. It also drifted, and it
    imported server-side modules into a client.

    If any of these names come back, that whole class of defect comes back with
    them."""
    for name in ("get_pg_conn", "release_pg_conn", "get_neo4j",
                 "_graph_entity_fallback", "get_embedding",
                 "DB_CONN", "NEO4J_URI", "NEO4J_AUTH"):
        assert not hasattr(vector_skill, name), (
            f"{name} is back — vector-skill must reach memory only through the gateway")
    # Scan CODE only — the module docstring narrates the removed design on
    # purpose, and a prose mention of it is the opposite of a regression.
    src = open(os.path.join(os.path.dirname(__file__), "..", "mcp", "vector-skill.py")).read()
    code = "\n".join(l for l in src.splitlines()
                     if l.strip() and not l.startswith(("#", " ", "\t")) or
                     l.lstrip().startswith(("import ", "from ", "sys.path")))
    for banned in ("import psycopg2", "from neo4j import",
                   "from ontology import", "sys.path.insert"):
        assert banned not in code, f"{banned!r} must not appear in a thin client"


@pytest.mark.asyncio
async def test_client_speaks_the_gateway_wire_version():
    """A stale API_VERSION makes the gateway log skew on every single request."""
    bridge = open(os.path.join(os.path.dirname(__file__), "..",
                               "shared-memory", "scripts", "memory_bridge.py")).read()
    expected = int(next(l for l in bridge.splitlines()
                        if l.startswith("API_VERSION")).split("=")[1])
    assert vector_skill.API_VERSION == expected


@pytest.mark.asyncio
async def test_mcp_save_artifact_success():
    """save_artifact routes through the coordinator (POST /memory/save) — no direct
    Postgres/Neo4j writes — and returns the pg_id from the gateway response."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {
        "status": "success", "pg_id": MOCK_PG_ID, "neo4j": "pending",
        "message": f"Artifact stored with ID {MOCK_PG_ID}.",
    }

    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        result = await vector_skill.save_artifact(
            MOCK_CONTENT, '{"source":"qwen3-27b","project":"shared-memory-GitHub","entities":["TestEntity"]}'
        )

    assert "Success" in result
    assert f"pg_id={MOCK_PG_ID}" in result
    # Routed to the gateway save endpoint with metadata as an OBJECT (the codec
    # serialises once — a stringified metadata here would double-encode).
    call = mock_post.call_args
    assert call.args[0].endswith("/memory/save")
    payload = call.kwargs["json"]
    assert isinstance(payload["metadata"], dict)
    assert payload["metadata"]["entities"] == ["TestEntity"]
    # Loaded model name preserved even though auth may overwrite source.
    assert payload["metadata"]["model"] == "qwen3-27b"


@pytest.mark.asyncio
async def test_mcp_save_artifact_gateway_down():
    """save_artifact returns a readable error when the gateway is unreachable."""
    with patch("httpx.AsyncClient.post", side_effect=Exception("connection refused")):
        result = await vector_skill.save_artifact(
            MOCK_CONTENT, '{"source":"qwen3-27b","project":"shared-memory-GitHub"}'
        )
    assert "Error" in result
    assert "hive_mind_proxy.py" in result


@pytest.mark.asyncio
async def test_mcp_save_artifact_surfaces_coordinator_error():
    """A 503 (embedder down) or other coordinator rejection is surfaced verbatim."""
    mock_response = MagicMock()
    mock_response.status_code = 503
    mock_response.json = lambda: {
        "status": "error",
        "message": "Embedding service unreachable after 4 attempts.",
    }
    with patch("httpx.AsyncClient.post", return_value=mock_response):
        result = await vector_skill.save_artifact(
            MOCK_CONTENT, '{"source":"qwen3-27b","project":"shared-memory-GitHub"}'
        )
    assert "Error" in result
    assert "Embedding service unreachable" in result


@pytest.mark.asyncio
async def test_mcp_save_artifact_missing_source_rejected_client_side():
    """source is required — rejected before any network call."""
    with patch("httpx.AsyncClient.post") as mock_post:
        result = await vector_skill.save_artifact(MOCK_CONTENT, '{"entities":["X"]}')
    assert "Error" in result
    assert "source is required" in result
    mock_post.assert_not_called()

@pytest.mark.asyncio
async def test_mcp_hybrid_search_goes_through_the_gateway():
    """Search delegates the entire chain — embedding, vector search, reranking,
    graph expansion AND read authorization — to POST /memory/search, and renders
    what comes back. The qualified `ref` is surfaced verbatim rather than reduced
    to a bare integer, because a bare integer taken off a summary result resolves
    against the facts table (decision 822)."""
    payload = {"results": [
        {"pg_id": 87, "ref": "summary:87", "record_type": "summary", "tier": 3,
         "content": "Global summary text", "source_pg_ids": [1, 2], "score": None},
        {"pg_id": 92, "ref": "insight:92", "record_type": "insight", "tier": 3,
         "content": "Cross-project principle", "source_pg_ids": [3], "score": None},
        {"pg_id": MOCK_PG_ID, "ref": f"fact:{MOCK_PG_ID}", "record_type": "fact",
         "tier": 1, "content": MOCK_CONTENT, "metadata": {"source": "mcp"},
         "score": 0.88, "graph_context": "BELONGS_TO -> SharedMem"},
    ]}
    mock_response = MagicMock(status_code=200, json=lambda: payload)
    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        result = await vector_skill.hybrid_search_and_rerank(MOCK_QUERY)

    call = mock_post.call_args
    assert call.args[0].endswith("/memory/search")
    assert call.kwargs["json"]["query"] == MOCK_QUERY
    # rendered
    assert "Global Context Summary" in result and "Global summary text" in result
    assert "Insight (cross-project principle)" in result
    assert "Unified Memory Results" in result
    assert "Score: 0.88" in result
    assert "BELONGS_TO -> SharedMem" in result
    # qualified refs surfaced, never a bare id for a summary
    assert "summary:87" in result and f"fact:{MOCK_PG_ID}" in result


@pytest.mark.asyncio
async def test_mcp_hybrid_search_reports_a_down_gateway_plainly():
    """The gateway is now the only path to memory, so an outage is a hard failure.
    Saying so beats returning an empty result set that reads as 'nothing found'."""
    with patch("httpx.AsyncClient.post", side_effect=RuntimeError("connection refused")):
        result = await vector_skill.hybrid_search_and_rerank(MOCK_QUERY)
    assert "unreachable" in result.lower()
    assert "hive-mind-gateway" in result


@pytest.mark.asyncio
async def test_mcp_archive_reasoning_trace_saves_a_record():
    """It used to CREATE ReasoningTrace/ReasoningStep nodes straight in Neo4j,
    which bypasses the outbox (the thing that makes a save atomic across both
    stores) and bypasses read authorization — durable in one store, visible to
    everyone. Now it is an ordinary record on the ordinary save path."""
    mock_response = MagicMock(status_code=200, json=lambda: {
        "status": "success", "pg_id": MOCK_PG_ID, "neo4j": "pending", "message": "ok"})
    steps = [{"thought": "research", "tool": "grep", "result": "found"}]
    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        result = await vector_skill.archive_reasoning_trace("sess_1", "test task", steps, project="shared-memory-GitHub")

    assert "Success" in result
    call = mock_post.call_args
    assert call.args[0].endswith("/memory/save")
    meta = call.kwargs["json"]["metadata"]
    assert meta["type"] == "reasoning_trace"
    assert meta["session_id"] == "sess_1"
    assert meta["step_count"] == 1
    assert "research" in call.kwargs["json"]["content"]


@pytest.mark.asyncio
async def test_mcp_archive_reasoning_trace_rejects_empty():
    result = await vector_skill.archive_reasoning_trace("sess_1", "t", [])
    assert "Error" in result


@pytest.mark.asyncio
async def test_mcp_save_decision_success():
    """save_decision routes through coordinator and returns pg_id on success."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {"status": "success", "pg_id": 77}

    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        result = await vector_skill.save_decision(
            title="Use asyncpg over psycopg2",
            decided_by="Xenofon",
            project="shared-memory",
            rationale="asyncpg does not block the event loop",
            source="qwen3-30b",
            assisted_by="claude-sonnet-4-6",
            confidence="high",
            entities="asyncpg,PostgreSQL",
        )

    assert "pg_id=77" in result
    assert "Use asyncpg over psycopg2" in result
    call_kwargs = mock_post.call_args
    payload = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs.kwargs["json"]
    assert payload["metadata"]["type"] == "decision"
    assert payload["metadata"]["decision"]["decided_by"] == "Xenofon"
    assert payload["metadata"]["decision"]["confidence"] == "high"
    assert "asyncpg" in payload["metadata"]["entities"]


@pytest.mark.asyncio
async def test_mcp_save_decision_grounded_in_and_elicited():
    """save_decision must expose grounded_in/elicited (capture-surface parity
    with memory_bridge.py's build_decision_metadata) — before this fix an LM
    Studio agent had no way to ground a decision in supporting facts or mark
    a field elicited at all, regardless of intent."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {"status": "success", "pg_id": 78}

    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        await vector_skill.save_decision(
            title="T", decided_by="X", project="P", rationale="R", source="qwen3",
            grounded_in="601:considered,602,603:rejected",
            elicited=True,
        )

    call_kwargs = mock_post.call_args
    payload = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs.kwargs["json"]
    meta = payload["metadata"]
    assert meta["grounded_in"] == [601, 602, 603]
    assert meta["grounded_roles"] == {"601": "considered", "603": "rejected"}
    assert meta["elicited"] is True


@pytest.mark.asyncio
async def test_mcp_save_decision_omits_grounded_in_and_elicited_when_absent():
    """Defaults stay silent — no empty grounded_in/elicited keys clutter every
    plain decision save."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {"status": "success", "pg_id": 79}

    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        await vector_skill.save_decision(
            title="T", decided_by="X", project="P", rationale="R", source="qwen3",
        )

    call_kwargs = mock_post.call_args
    payload = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs.kwargs["json"]
    meta = payload["metadata"]
    assert "grounded_in" not in meta
    assert "grounded_roles" not in meta
    assert "elicited" not in meta


@pytest.mark.asyncio
async def test_mcp_save_decision_stores_an_alternative_containing_a_comma_whole():
    """Group 1 parity: this client carried the SAME `.split(",")` as
    memory_bridge.py, so the shredding was never a CLI defect — both front doors
    fragmented a well-written alternative, in Postgres and in the graph. A
    capture surface must not accept a value it cannot faithfully represent."""
    alt = "use explicit Neo4j transactions for atomicity (APOC not available, auto-commit is the existing pattern)"
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {"status": "success", "pg_id": 80}

    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        await vector_skill.save_decision(
            title="T", decided_by="X", project="P", rationale="R", source="qwen3",
            alternatives=[alt],
        )

    call_kwargs = mock_post.call_args
    payload = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs.kwargs["json"]
    assert payload["metadata"]["decision"]["alternatives"] == [alt]


@pytest.mark.asyncio
async def test_mcp_save_decision_treats_a_lone_string_as_one_alternative():
    """Under-splitting is the safe direction — it never invents an option."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {"status": "success", "pg_id": 81}

    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        await vector_skill.save_decision(
            title="T", decided_by="X", project="P", rationale="R", source="qwen3",
            alternatives="psycopg2, aiopg",
        )

    call_kwargs = mock_post.call_args
    payload = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs.kwargs["json"]
    assert payload["metadata"]["decision"]["alternatives"] == ["psycopg2, aiopg"]


@pytest.mark.asyncio
async def test_mcp_save_decision_coordinator_down():
    """save_decision returns a readable error when the coordinator is unreachable."""
    with patch("httpx.AsyncClient.post", side_effect=Exception("connection refused")):
        result = await vector_skill.save_decision(
            title="T", decided_by="X", project="P",
            rationale="R", source="test-model",
        )
    assert "Error" in result
    assert "hive_mind_proxy.py" in result


@pytest.mark.asyncio
async def test_mcp_save_decision_coordinator_returns_400():
    """save_decision surfaces coordinator error messages (e.g. missing required fields).

    The status code is now the one this test's name always claimed — 400, not
    the 200 an unset mock attribute silently implied. A validation refusal must
    reach the caller carrying the gateway's own words, whatever the status
    class it arrives under.
    """
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.json = lambda: {
        "status": "error",
        "message": "decision save missing required fields: ['rationale']",
    }
    with patch("httpx.AsyncClient.post", return_value=mock_response):
        result = await vector_skill.save_decision(
            title="T", decided_by="X", project="P",
            rationale="", source="test-model",
        )
    assert "Error" in result
    assert "rationale" in result


@pytest.mark.asyncio
async def test_mcp_save_retrospective_success():
    """save_retrospective routes through the coordinator and reports the
    target decision's pg_id plus this record's own pg_id. No prior test
    covered this tool at all."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {"status": "success", "pg_id": 91, "target_pg_id": 43}

    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        result = await vector_skill.save_retrospective(
            pg_id=43, rating="validated", notes="held up in prod", source="qwen3",
        )

    assert "Decision pg_id=43" in result
    assert "record pg_id=91" in result
    call_kwargs = mock_post.call_args
    payload = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs.kwargs["json"]
    assert payload["pg_id"] == 43
    assert payload["rating"] == "validated"


@pytest.mark.asyncio
async def test_mcp_save_retrospective_grounded_in_and_elicited():
    """save_retrospective must expose grounded_in/elicited too — same capture-
    surface parity gap as save_decision, same fix."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {"status": "success", "pg_id": 92, "target_pg_id": 43}

    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        await vector_skill.save_retrospective(
            pg_id=43, rating="validated", notes="n", source="qwen3",
            grounded_in="601,602:considered",
            elicited=True,
        )

    call_kwargs = mock_post.call_args
    payload = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs.kwargs["json"]
    assert payload["grounded_in"] == [601, 602]
    assert payload["grounded_roles"] == {"602": "considered"}
    assert payload["elicited"] is True


@pytest.mark.asyncio
async def test_mcp_check_memory_health_asks_the_gateway():
    """Health is what the gateway reports — daemons, backends, consolidation
    liveness — not a row count from a database handle this client should not
    hold. It is also the only check that exercises the path the client uses."""
    gw = {"status": "ok", "version": "0.7.7", "api_version": 4,
          "daemon": "running", "rem_daemon": "running", "embedder": "ok"}
    mock_response = MagicMock(status_code=200, json=lambda: gw)
    with patch("httpx.AsyncClient.get", return_value=mock_response) as mock_get:
        result = json.loads(await vector_skill.check_memory_health())

    assert mock_get.call_args.args[0].endswith("/health")
    assert result["status"] == "ok"
    assert result["client"]["version"] == vector_skill.VERSION
    assert "version_skew" not in result["client"]


@pytest.mark.asyncio
async def test_mcp_check_memory_health_names_version_skew():
    gw = {"status": "ok", "api_version": 99}
    mock_response = MagicMock(status_code=200, json=lambda: gw)
    with patch("httpx.AsyncClient.get", return_value=mock_response):
        result = json.loads(await vector_skill.check_memory_health())
    assert "version_skew" in result["client"]


@pytest.mark.asyncio
async def test_mcp_check_memory_health_unreachable_gateway():
    with patch("httpx.AsyncClient.get", side_effect=RuntimeError("refused")):
        result = json.loads(await vector_skill.check_memory_health())
    assert result["status"] == "unreachable"


@pytest.mark.asyncio
async def test_mcp_record_lineage_requires_a_valid_ref():
    """A bare id is accepted for compatibility; a malformed or wrongly-typed
    qualified ref is refused before it can resolve against the wrong table."""
    bad = await vector_skill.record_lineage("summary:notanumber")
    assert "Error" in bad
    bad2 = await vector_skill.record_lineage("widget:12")
    assert "Error" in bad2

    mock_response = MagicMock(status_code=200, json=lambda: {"pg_id": 87, "exists": True})
    with patch("httpx.AsyncClient.get", return_value=mock_response) as mock_get:
        ok = await vector_skill.record_lineage("summary:87")
    assert mock_get.call_args.args[0].endswith("/memory/status/summary:87")
    assert json.loads(ok)["exists"] is True


@pytest.mark.asyncio
async def test_mcp_review_edges_validates_family():
    bad = await vector_skill.review_edges("not_a_family")
    assert "Error" in bad
    assert vector_skill.RELATION_FAMILIES == ("entity_relation", "evidential")


# ── supersede / review_hold MCP tools (fact supersession, decision 381/384) ──

@pytest.mark.asyncio
async def test_mcp_supersede_bare_retract():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {
        "status": "success", "superseded": 7, "superseded_by": None,
        "purged_outbox": 1, "message": "Fact 7 superseded (retracted, no replacement).",
    }
    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        result = await vector_skill.supersede(7)
    assert "superseded" in result.lower()
    call = mock_post.call_args
    assert call.args[0].endswith("/memory/supersede")
    assert call.kwargs["json"] == {"pg_id": 7}          # no `by` when omitted


@pytest.mark.asyncio
async def test_mcp_supersede_with_successor():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {
        "status": "success", "superseded": 7, "superseded_by": 9,
        "purged_outbox": 0, "message": "Fact 7 superseded by 9.",
    }
    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        result = await vector_skill.supersede(7, by=9)
    assert "9" in result
    assert mock_post.call_args.kwargs["json"] == {"pg_id": 7, "by": 9}


@pytest.mark.asyncio
async def test_mcp_supersede_surfaces_coordinator_error():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {"status": "error", "message": "fact 7 is already superseded"}
    with patch("httpx.AsyncClient.post", return_value=mock_response):
        result = await vector_skill.supersede(7)
    assert "Error" in result and "already superseded" in result


@pytest.mark.asyncio
async def test_mcp_review_hold():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {
        "status": "success", "summary_id": 3, "reviewed": {"old": 5, "by": 6},
        "message": "Summary 3: supersession of 5 marked reviewed-and-held.",
    }
    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        result = await vector_skill.review_hold(3, 5)
    assert "reviewed-and-held" in result
    call = mock_post.call_args
    assert call.args[0].endswith("/memory/review_hold")
    assert call.kwargs["json"] == {"summary_id": 3, "pg_id": 5}


# ── Client-scoped env: never load the framework/server env ───────────────────

def _vs_module():
    """Load vector-skill.py fresh (module name has a dash, so import by path)."""
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), "..", "mcp", "vector-skill.py")
    spec = importlib.util.spec_from_file_location("vector_skill_env", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_server_env_is_recognised_and_refused(tmp_path):
    """A client must never inherit the framework env: it carries the whole
    AGENT_TOKENS registry plus both DB passwords, so loading it would hand this
    one client every other agent's credentials and defeat per-origin tokens."""
    vs = _vs_module()
    for key in ("AGENT_TOKENS=claude:tok_a,grok:tok_b",
                "PG_PASSWORD=hunter2",
                "NEO4J_PASSWORD=hunter2"):
        f = tmp_path / f"{key.split('=')[0]}.env"
        f.write_text(f"COORDINATOR_URL=http://localhost:8888\n{key}\n")
        assert vs._looks_like_server_env(str(f)) is True, key


def test_client_env_is_accepted(tmp_path):
    """AGENT_TOKEN (singular) is exactly what a client SHOULD hold."""
    vs = _vs_module()
    f = tmp_path / "client.env"
    f.write_text("AGENT_TOKEN=tok_mine\nCOORDINATOR_URL=http://localhost:8888\n"
                 "AGENT_ID=some_other_host\n")
    assert vs._looks_like_server_env(str(f)) is False


def test_agent_token_singular_is_not_mistaken_for_the_registry(tmp_path):
    """The obvious footgun: AGENT_TOKEN vs AGENT_TOKENS differ by one character,
    and refusing a legitimate client env would break every MCP install."""
    vs = _vs_module()
    f = tmp_path / "singular.env"
    f.write_text("AGENT_TOKEN=tok_mine\n")
    assert vs._looks_like_server_env(str(f)) is False


def test_commented_server_keys_do_not_trigger_the_refusal(tmp_path):
    """.env files are copied from .env.example, which carries commented keys."""
    vs = _vs_module()
    f = tmp_path / "commented.env"
    f.write_text("# AGENT_TOKENS=agent:tok\n# PG_PASSWORD=\nAGENT_TOKEN=tok_mine\n")
    assert vs._looks_like_server_env(str(f)) is False


def test_missing_file_is_not_a_server_env(tmp_path):
    vs = _vs_module()
    assert vs._looks_like_server_env(str(tmp_path / "nope.env")) is False


def test_agent_id_is_read_in_exactly_one_place():
    """AGENT_ID is a LOCAL label only — the gateway overwrites metadata["source"]
    with the authenticated token identity (coordinator.py), so per-origin
    differentiation is by TOKEN, server-side, and never by this value. It still
    wants one source: it used to default to "vector_skill" at module level and
    "lm_studio" at three call sites, so logs and the search agent_id field
    disagreed within a single process depending on which tool ran.
    """
    src = open(os.path.join(os.path.dirname(__file__), "..", "mcp", "vector-skill.py"),
               encoding="utf-8").read()
    assert src.count('os.environ.get("AGENT_ID"') == 1, (
        "AGENT_ID is read in more than one place — a second default can diverge "
        "from the first, which is the inconsistency this pins"
    )


def test_every_401_routes_through_the_auth_helper():
    """Six tools — all of them WRITE tools — inlined their own 401 message and so
    never logged the auth failure, which is the worse half to lose from the audit
    trail. Three different message texts had already drifted apart. One helper
    now owns the response, and it is the single place the token-source guidance
    lives, so it cannot go stale in five copies again.
    """
    path = os.path.join(os.path.dirname(__file__), "..", "mcp", "vector-skill.py")
    src = open(path, encoding="utf-8").read()
    lines = src.split("\n")

    checks = [i for i, l in enumerate(lines) if "status_code == 401" in l]
    assert checks, "no 401 handling found at all"
    for i in checks:
        following = lines[i + 1]
        assert "_auth_rejected(" in following, (
            f"line {i + 2} handles a 401 without the helper — it will not log the "
            f"failure: {following.strip()[:70]}"
        )

    assert "rejected token" not in src.lower().replace(
        "rejected this client's token", ""), (
        "an inline 401 message is back; _auth_rejected owns that text"
    )


def test_auth_helper_is_called_with_the_calling_tool_name():
    """The log's `tool` field was the literal "vector_skill" at every site, so it
    never said which call was rejected."""
    path = os.path.join(os.path.dirname(__file__), "..", "mcp", "vector-skill.py")
    src = open(path, encoding="utf-8").read()
    assert '_auth_rejected("vector_skill")' not in src, (
        "the helper is being passed the client name instead of the tool name"
    )


# ── The docs that describe this surface must track it (change Group 1) ────────

def _repo(*parts):
    return os.path.join(os.path.dirname(__file__), "..", *parts)


def _registered_tools() -> list:
    """Tool names actually registered with @mcp.tool, read from the source."""
    import ast
    tree = ast.parse(open(_repo("mcp", "vector-skill.py"), encoding="utf-8").read())
    names = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(target, ast.Attribute) and target.attr == "tool":
                    names.append(node.name)
    return names


def test_system_prompt_names_every_registered_mcp_tool():
    """The MCP surface grew to 13 tools while its system prompt described 8 — so a
    model driven by it never knew it could trace lineage, query the graph, or take
    part in relation calibration. Adding a tool without documenting it makes the
    tool unreachable in practice."""
    tools = _registered_tools()
    assert len(tools) >= 13, f"expected the full tool surface, found {tools}"
    prompt = open(_repo("mcp", "system-prompt.md"), encoding="utf-8").read()
    missing = [t for t in tools if t not in prompt]
    assert not missing, f"system-prompt.md does not mention MCP tool(s): {missing}"


def test_system_prompt_does_not_send_the_model_to_a_database_mcp():
    """It used to list `neo4j-memory` as search step 2. A direct-bolt database MCP
    connects past the gateway, and the gateway is what applies the read-visibility
    predicate — so that fallback pointed at the exact unauthorized path removed
    from this client in v0.8.0."""
    prompt = open(_repo("mcp", "system-prompt.md"), encoding="utf-8").read()
    assert "Never register or reach for a database MCP" in prompt, (
        "the prohibition on database MCPs is missing"
    )
    # BOTH stores, named. Postgres is the more tempting one — a generic SQL MCP
    # looks harmless next to a graph driver, and it reaches the same rows with the
    # same absence of a visibility predicate.
    for store in ("Postgres", "Neo4j"):
        assert store in prompt.split("Never register or reach for a database MCP")[1][:600], (
            f"the prohibition does not name {store} — a reader will assume it is "
            f"about the other store only"
        )
    section = prompt.split("# SEARCH-FIRST MANDATE")[1].split("\n# ", 1)[0]
    # Only the ENUMERATED steps — the prohibition below them names the forbidden
    # servers on purpose, so scanning the whole section would flag its own warning.
    steps = [l for l in section.splitlines()
             if l.lstrip().startswith(("1.", "2.", "3.", "4."))]
    assert steps, "the search hierarchy has no enumerated steps"
    joined = "\n".join(steps).lower()
    for forbidden in ("neo4j-memory", "server-postgres", "postgres-mcp",
                      "mcp-postgres", "server-neo4j"):
        assert forbidden not in joined, (
            f"a direct database MCP ({forbidden}) is back among the recommended "
            f"search steps — it reaches the rows with no visibility predicate"
        )
    assert "graph_query" in joined, (
        "the authorized graph fallback should be the escalation step"
    )


def test_shipped_mcp_config_registers_no_database_server():
    """The config we ship must not itself register the thing the prompt forbids.
    A database MCP alongside rag-orchestrator re-opens read authorization from the
    other side: the gateway filters every read on `visibility`, a raw SQL or Bolt
    connection filters on nothing."""
    import json as _json
    cfg = _json.load(open(_repo("mcp", "mcp.json"), encoding="utf-8"))
    servers = cfg.get("mcpServers") or cfg
    for name, entry in servers.items():
        blob = (name + " " + _json.dumps(entry)).lower()
        for forbidden in ("server-postgres", "postgres-mcp", "mcp-postgres",
                          "neo4j-memory", "server-neo4j", "psycopg", "bolt://"):
            assert forbidden not in blob, (
                f"mcp.json server '{name}' looks like a direct database MCP "
                f"({forbidden}) — memory access goes through the gateway only"
            )


def test_readme_mcp_config_block_carries_the_token():
    """README's rag-orchestrator example had no env block at all, so anyone
    following it built a client with no AGENT_TOKEN — auth has been mandatory
    since v0.3.5, so every call would 401. Broken on arrival, not merely stale."""
    readme = open(_repo("README.md"), encoding="utf-8").read()
    # Anchor on the example itself, not a numbered heading — the 0.9.0 README
    # rewrite renumbered every section (the old pin was "## 16. LM Studio MCP
    # Configuration"), and the contract is about the example's content, which
    # must hold wherever the section lands.
    assert '"rag-orchestrator"' in readme, (
        "the README no longer shows a rag-orchestrator MCP example at all"
    )
    block = readme.split('"rag-orchestrator"')[1].split("```")[0]
    assert '"AGENT_TOKEN"' in block, (
        "the rag-orchestrator example omits AGENT_TOKEN — following it yields a "
        "client that 401s on every call"
    )
    assert '"COORDINATOR_URL"' in block
