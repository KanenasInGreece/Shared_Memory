"""Operator relation review/label surface (REM rebuild stage 4, decisions 726/727).

Coverage:
  - api_version bumped to 3 consistently in coordinator.py AND memory_bridge.py
  - route auth: read-only role gets 403 on BOTH /memory/relations/* routes;
    a full-role token passes; the label route is backup-quiesce shed (write)
  - /memory/relations/review: stratified rows + calibration envelope, JSON-safe
    datetimes, limit cap, unknown-family 400, evidential snippet enrichment
  - /memory/relations/label: labels applied; promote flips asserted_by via a
    KNOWN_RELATIONSHIPS-guarded rel_type interpolation; 'incorrect' deletes ONLY
    machine-asserted edges (the Cypher guard is asserted); reject-verdict rows
    delete nothing; ledger rows are never deleted
  - memory_bridge review-edges / label-edges hit the right endpoints with the
    right payloads (mocked httpx) and render the calibration line
"""

import importlib.util
import json
import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Dynamic imports (test_coordinator / test_memory_bridge conventions) ────────

def load_coordinator():
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    path = os.path.join(scripts_dir, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator_review_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_coordinator_with_auth(agent_tokens: str, agent_roles: str = ""):
    """Fresh module with AGENT_TOKENS / AGENT_ROLES pre-set (test_auth pattern)."""
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    if agent_tokens:
        os.environ["AGENT_TOKENS"] = agent_tokens
    else:
        os.environ.pop("AGENT_TOKENS", None)
    if agent_roles:
        os.environ["AGENT_ROLES"] = agent_roles
    else:
        os.environ.pop("AGENT_ROLES", None)
    path = os.path.join(scripts_dir, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator_review_auth_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    os.environ.pop("AGENT_TOKENS", None)
    os.environ.pop("AGENT_ROLES", None)
    return mod


def load_memory_bridge():
    path = os.path.join(os.path.dirname(__file__), "..", "shared-memory-skill",
                        "shared-memory", "scripts", "memory_bridge.py")
    spec = importlib.util.spec_from_file_location("memory_bridge_review_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


coordinator_mod = load_coordinator()
memory_bridge = load_memory_bridge()
MemoryCoordinator = coordinator_mod.MemoryCoordinator


# ── Helpers (mirrors test_coordinator.py) ─────────────────────────────────────

class _async_ctx:
    def __init__(self, val):
        self._val = val

    async def __aenter__(self):
        return self._val

    async def __aexit__(self, *_):
        pass


def _make_request(body: dict) -> MagicMock:
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.get = MagicMock(return_value=None)
    return req


def _make_auth_request(path: str, auth_header: str | None = None,
                       method: str = "POST") -> MagicMock:
    req = MagicMock()
    req.path = path
    req.method = method
    headers = {}
    if auth_header is not None:
        headers["Authorization"] = auth_header
    req.headers = headers
    req.get = MagicMock(return_value=None)
    req.__setitem__ = MagicMock()
    return req


async def _noop_handler(request):
    from aiohttp import web
    return web.json_response({"ok": True})


def _neo4j_result(n: int = 1):
    res = MagicMock()
    res.single = AsyncMock(return_value={"n": n})
    return res


def _coordinator_with_mocks():
    c = MemoryCoordinator()
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.execute = AsyncMock()
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_async_ctx(mock_conn))
    c._pool = mock_pool
    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=_neo4j_result())
    mock_neo4j = MagicMock()
    mock_neo4j.session = MagicMock(return_value=_async_ctx(mock_session))
    c._neo4j = mock_neo4j
    return c, mock_conn, mock_session


def _entity_row(**over):
    row = {"id": 12, "family": "entity_relation", "src_name": "Gateway",
           "tgt_name": "coordinator.py", "src_pg_id": None, "tgt_pg_id": None,
           "rel_type": "DEPENDS_ON", "verdict": "accept",
           "operator_label": None, "promoted_at": None}
    row.update(over)
    return row


def _evidential_row(**over):
    row = {"id": 33, "family": "evidential", "src_name": None, "tgt_name": None,
           "src_pg_id": 601, "tgt_pg_id": 640, "rel_type": "INFORMED_BY",
           "verdict": "accept", "operator_label": None, "promoted_at": None}
    row.update(over)
    return row


# ── api_version contract — bumped consistently in BOTH files ──────────────────

def test_api_version_is_3_in_coordinator_and_client():
    assert coordinator_mod.API_VERSION == 3
    assert memory_bridge.API_VERSION == 3
    assert coordinator_mod.API_VERSION == memory_bridge.API_VERSION


# ── Route auth: operator-grade, read-only 403, label route sheds on quiesce ──

@pytest.mark.asyncio
async def test_read_role_denied_on_relations_review():
    from aiohttp.web_exceptions import HTTPForbidden
    mod = load_coordinator_with_auth("monitor:tok_m", agent_roles="monitor:read")
    req = _make_auth_request("/memory/relations/review", "Bearer tok_m")
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req, _noop_handler)


@pytest.mark.asyncio
async def test_read_role_denied_on_relations_label():
    from aiohttp.web_exceptions import HTTPForbidden
    mod = load_coordinator_with_auth("monitor:tok_m", agent_roles="monitor:read")
    req = _make_auth_request("/memory/relations/label", "Bearer tok_m")
    with pytest.raises(HTTPForbidden):
        await mod.auth_middleware(req, _noop_handler)


@pytest.mark.asyncio
async def test_full_role_token_passes_relations_review():
    mod = load_coordinator_with_auth("claude:tok_abc", agent_roles="monitor:read")
    req = _make_auth_request("/memory/relations/review", "Bearer tok_abc")
    resp = await mod.auth_middleware(req, _noop_handler)
    assert resp.status == 200


def test_label_route_is_a_write_route_review_is_not():
    """Labeling mutates ledger + graph → sheds during a backup quiesce; the
    review sample is a pure read and must keep flowing."""
    assert ("POST", "/memory/relations/label") in coordinator_mod._WRITE_ROUTES
    assert ("POST", "/memory/relations/review") not in coordinator_mod._WRITE_ROUTES
    # Neither route is on the read-role allowlist — labeling is operator-grade.
    assert ("POST", "/memory/relations/review") not in coordinator_mod._READ_ROLE_ROUTES
    assert ("POST", "/memory/relations/label") not in coordinator_mod._READ_ROLE_ROUTES


# ── POST /memory/relations/review ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_review_returns_rows_and_calibration_envelope():
    c, mock_conn, _ = _coordinator_with_mocks()
    sample = [{"id": 12, "family": "entity_relation", "src_name": "Gateway",
               "tgt_name": "coordinator.py", "src_pg_id": None, "tgt_pg_id": None,
               "rel_type": "DEPENDS_ON", "verdict": "accept", "method": "llm_sweep",
               "confidence": 0.8, "support": "graph_evidence",
               "signals": {"cooccur_count": 3}, "rationale": "why",
               "created_at": datetime(2026, 7, 15, 12, 0)}]
    calib = [{"band": 8, "labeled": 3, "correct": 2}]
    mock_conn.fetch = AsyncMock(side_effect=[sample, calib])

    resp = await c.handle_relations_review(_make_request({}))
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["status"] == "success"
    assert body["family"] == "entity_relation"          # default family
    row = body["rows"][0]
    assert row["src_name"] == "Gateway" and row["rel_type"] == "DEPENDS_ON"
    assert row["created_at"] == "2026-07-15T12:00:00"   # JSON-safe datetime
    cal = body["calibration"]
    assert cal["family"] == "entity_relation"
    assert cal["labels"] == 3 and cal["calibrated"] is False
    assert cal["min_labels"] == coordinator_mod.RELCONF_MIN_LABELS
    # Read from the module rather than restated: the consumption threshold moves
    # with the WRITE floor (989), and a literal here would just have to be
    # chased each time instead of proving the envelope reports what is configured.
    assert cal["threshold"] == pytest.approx(
        coordinator_mod.RELCONF_CONSUME_THRESHOLD["entity_relation"])
    assert cal["bands"][0]["precision"] == pytest.approx(0.667)
    # stratified sample: deciles round-robin, newest-first inside each decile
    sql = mock_conn.fetch.await_args_list[0].args[0]
    assert "width_bucket" in sql and "operator_label IS NULL" in sql


@pytest.mark.asyncio
async def test_review_evidential_enriches_endpoint_snippets():
    c, mock_conn, _ = _coordinator_with_mocks()
    sample = [{"id": 33, "family": "evidential", "src_name": None, "tgt_name": None,
               "src_pg_id": 601, "tgt_pg_id": 640, "rel_type": "INFORMED_BY",
               "verdict": "accept", "method": "rem_k3", "confidence": 0.55,
               "support": None, "signals": {}, "rationale": None,
               "created_at": datetime(2026, 7, 15)}]
    snips = [{"id": 601, "snippet": "decision text..."},
             {"id": 640, "snippet": "fact text..."}]
    calib = []
    mock_conn.fetch = AsyncMock(side_effect=[sample, snips, calib])

    resp = await c.handle_relations_review(_make_request({"family": "evidential"}))
    body = json.loads(resp.text)
    row = body["rows"][0]
    assert row["src_snippet"] == "decision text..."
    assert row["tgt_snippet"] == "fact text..."
    # snippet fetch is LEFT(content, 160) over technical_docs for both endpoints
    snip_call = mock_conn.fetch.await_args_list[1]
    assert "LEFT(content" in snip_call.args[0]
    assert snip_call.args[1] == [601, 640]
    assert snip_call.args[2] == coordinator_mod.RELATION_SNIPPET_CHARS
    assert body["calibration"]["threshold"] == pytest.approx(0.70)


@pytest.mark.asyncio
async def test_review_rejects_unknown_family_and_bad_limit():
    c, _, _ = _coordinator_with_mocks()
    resp = await c.handle_relations_review(_make_request({"family": "aliases"}))
    assert resp.status == 400
    resp = await c.handle_relations_review(_make_request({"limit": 0}))
    assert resp.status == 400
    resp = await c.handle_relations_review(_make_request({"limit": True}))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_review_caps_limit_at_100():
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(side_effect=[[], []])
    await c.handle_relations_review(_make_request({"limit": 5000}))
    assert mock_conn.fetch.await_args_list[0].args[2] == 100


# ── POST /memory/relations/label ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_label_applies_and_promote_flips_asserted_by():
    c, mock_conn, mock_session = _coordinator_with_mocks()
    row = _entity_row()
    mock_conn.fetch = AsyncMock(return_value=[row])

    resp = await c.handle_relations_label(_make_request(
        {"labels": {"12": "correct"}, "promote": [12]}))
    assert resp.status == 200
    out = json.loads(resp.text)["outcomes"]["12"]
    assert out["labeled"] == "correct"
    assert out["promoted"] is True and out["edges_updated"] == 1

    executes = [c_.args[0] for c_ in mock_conn.execute.call_args_list]
    assert any("SET operator_label=$2" in s for s in executes)
    assert any("SET promoted_at=now()" in s for s in executes)

    cypher = mock_session.run.await_args.args[0]
    # rel_type interpolation happened through the KNOWN_RELATIONSHIPS guard
    assert "-[r:DEPENDS_ON]->" in cypher
    assert "r.asserted_by = 'operator'" in cypher
    kwargs = mock_session.run.await_args.kwargs
    assert kwargs["src"] == "Gateway" and kwargs["tgt"] == "coordinator.py"


@pytest.mark.asyncio
async def test_promote_refuses_row_not_labeled_correct():
    c, mock_conn, mock_session = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=[_entity_row(operator_label=None)])
    resp = await c.handle_relations_label(_make_request({"promote": [12]}))
    out = json.loads(resp.text)["outcomes"]["12"]
    assert out["promoted"] is False and "correct" in out["error"]
    mock_session.run.assert_not_awaited()
    # no promoted_at stamp either
    assert not any("promoted_at=now()" in c_.args[0]
                   for c_ in mock_conn.execute.call_args_list)


@pytest.mark.asyncio
async def test_promote_accepts_previously_recorded_correct_label():
    c, mock_conn, mock_session = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=[_entity_row(operator_label="correct")])
    resp = await c.handle_relations_label(_make_request({"promote": [12]}))
    out = json.loads(resp.text)["outcomes"]["12"]
    assert out["promoted"] is True
    mock_session.run.assert_awaited()


@pytest.mark.asyncio
async def test_promote_never_interpolates_non_schema_rel_type():
    """A ledger row whose rel_type is outside KNOWN_RELATIONSHIPS (e.g. the
    reject sentinel 'NONE', or a poisoned string) must never reach Cypher."""
    c, mock_conn, mock_session = _coordinator_with_mocks()
    evil = "DEPENDS_ON]->(x) DETACH DELETE x //"
    mock_conn.fetch = AsyncMock(return_value=[_entity_row(
        rel_type=evil, operator_label="correct")])
    resp = await c.handle_relations_label(_make_request({"promote": [12]}))
    out = json.loads(resp.text)["outcomes"]["12"]
    assert out["promoted"] is True and out["edges_updated"] == 0
    mock_session.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_incorrect_label_deletes_only_machine_asserted_edges():
    """The delete Cypher carries the asserted_by IN [rem, rem_sweep] guard —
    an operator-asserted edge survives an 'incorrect' label; the ledger row
    is updated, never deleted."""
    c, mock_conn, mock_session = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=[_evidential_row()])

    resp = await c.handle_relations_label(_make_request(
        {"labels": {"33": "incorrect"}}))
    out = json.loads(resp.text)["outcomes"]["33"]
    assert out["labeled"] == "incorrect" and out["edge_deleted"] == 1

    cypher = mock_session.run.await_args.args[0]
    assert "r.asserted_by IN $machine" in cypher      # the operator-edge guard
    assert "DELETE r" in cypher
    assert "-[r:INFORMED_BY]->" in cypher
    # evidential endpoints matched across the record labels, by pg_id
    for lbl in ("a:Fact", "a:Decision", "a:Retrospective"):
        assert lbl in cypher
    kwargs = mock_session.run.await_args.kwargs
    assert kwargs["machine"] == ["rem", "rem_sweep"]
    assert kwargs["src"] == 601 and kwargs["tgt"] == 640
    # ledger row is never deleted — only UPDATEd
    assert not any("DELETE FROM relation_adjudications" in c_.args[0]
                   for c_ in mock_conn.execute.call_args_list)


@pytest.mark.asyncio
async def test_incorrect_label_on_reject_verdict_touches_no_edge():
    c, mock_conn, mock_session = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=[_entity_row(
        rel_type="NONE", verdict="reject")])
    resp = await c.handle_relations_label(_make_request(
        {"labels": {"12": "incorrect"}}))
    out = json.loads(resp.text)["outcomes"]["12"]
    assert out["labeled"] == "incorrect" and "edge_deleted" not in out
    mock_session.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_label_validation_errors():
    c, _, _ = _coordinator_with_mocks()
    resp = await c.handle_relations_label(_make_request({}))
    assert resp.status == 400                                  # nothing to do
    resp = await c.handle_relations_label(_make_request(
        {"labels": {"12": "wrong"}}))
    assert resp.status == 400                                  # bad vocabulary
    resp = await c.handle_relations_label(_make_request(
        {"labels": {"twelve": "correct"}}))
    assert resp.status == 400                                  # non-int id


@pytest.mark.asyncio
async def test_label_batches_ledger_fetch_into_one_query():
    """Code-review finding: both loops re-fetched each ledger row individually
    (fetchrow per id) rather than one `WHERE id = ANY($1)` fetch. With 2 label
    rows and 1 promote row (one id, 12, shared by both labels and promote — it
    must be fetched exactly once, not twice), conn.fetch must be called
    exactly ONCE for the ledger rows, not per-row."""
    c, mock_conn, mock_session = _coordinator_with_mocks()
    rows = [
        _entity_row(id=12, operator_label="correct"),
        _evidential_row(id=33),
    ]
    mock_conn.fetch = AsyncMock(return_value=rows)

    resp = await c.handle_relations_label(_make_request(
        {"labels": {"33": "correct"}, "promote": [12]}))

    assert resp.status == 200
    out = json.loads(resp.text)["outcomes"]
    assert out["33"]["labeled"] == "correct"
    assert out["12"]["promoted"] is True
    # Exactly one batched ledger fetch, covering the union of both loops' ids.
    assert mock_conn.fetch.await_count == 1
    fetched_ids = mock_conn.fetch.await_args.args[1]
    assert sorted(fetched_ids) == [12, 33]


@pytest.mark.asyncio
async def test_label_missing_row_reports_per_row_error():
    c, mock_conn, _ = _coordinator_with_mocks()
    mock_conn.fetch = AsyncMock(return_value=[])   # batched fetch: no row for id 999
    resp = await c.handle_relations_label(_make_request(
        {"labels": {"999": "correct"}}))
    assert resp.status == 200
    assert "not found" in json.loads(resp.text)["outcomes"]["999"]["error"]


# ── memory_bridge — review-edges / label-edges client surface ─────────────────

@pytest.mark.asyncio
async def test_fetch_review_edges_posts_right_endpoint_and_payload():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: {"status": "success", "family": "evidential",
                              "rows": [], "calibration": {}}
    with patch("httpx.AsyncClient.post",
               new=AsyncMock(return_value=mock_resp)) as mock_post:
        result = await memory_bridge.fetch_review_edges("evidential", 7)
    assert result["status"] == "success"
    assert mock_post.await_args.args[0].endswith("/memory/relations/review")
    assert mock_post.await_args.kwargs["json"] == {"family": "evidential", "limit": 7}


@pytest.mark.asyncio
async def test_apply_edge_labels_posts_right_endpoint_and_payload():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: {"status": "success", "outcomes": {}}
    with patch("httpx.AsyncClient.post",
               new=AsyncMock(return_value=mock_resp)) as mock_post:
        result = await memory_bridge.apply_edge_labels(
            {"12": "correct", "13": "incorrect"}, [12])
    assert result["status"] == "success"
    assert mock_post.await_args.args[0].endswith("/memory/relations/label")
    assert mock_post.await_args.kwargs["json"] == {
        "labels": {"12": "correct", "13": "incorrect"}, "promote": [12]}


@pytest.mark.asyncio
async def test_review_edges_403_names_the_operator_grade_route():
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_resp)):
        result = await memory_bridge.fetch_review_edges()
    assert result["status"] == "error"
    assert "operator-grade" in result["message"]


def test_parse_edge_labels_grammar():
    assert memory_bridge._parse_edge_labels("12=correct, 13=incorrect") == {
        "12": "correct", "13": "incorrect"}
    assert memory_bridge._parse_edge_labels("") == {}


def test_format_review_edges_renders_rows_and_uncalibrated_line():
    payload = {
        "status": "success", "family": "evidential",
        "rows": [{"id": 33, "verdict": "accept", "confidence": 0.55,
                  "src_name": None, "tgt_name": None,
                  "src_pg_id": 601, "tgt_pg_id": 640,
                  "rel_type": "INFORMED_BY", "method": "rem_k3", "support": None,
                  "rationale": "vote share 2/3",
                  "src_snippet": "decision text", "tgt_snippet": "fact text"}],
        "calibration": {"family": "evidential", "labels": 3, "calibrated": False,
                        "min_labels": 20, "threshold": 0.70, "bands": []},
    }
    out = memory_bridge.format_review_edges(payload)
    assert "id=33" in out and "record 601 -INFORMED_BY-> record 640" in out
    assert "rationale: vote share 2/3" in out
    assert "src: decision text" in out and "tgt: fact text" in out
    assert ("family evidential: 3/20 labels — UNCALIBRATED, "
            "machine edges not consumed by synthesis") in out


def test_format_review_edges_calibrated_line_shows_threshold():
    payload = {"status": "success", "family": "entity_relation", "rows": [],
               "calibration": {"family": "entity_relation", "labels": 24,
                               "calibrated": True, "min_labels": 20,
                               "threshold": 0.60, "bands": []}}
    out = memory_bridge.format_review_edges(payload)
    assert "family entity_relation: 24 labels — calibrated, threshold 0.6" in out


def test_format_label_outcomes_per_row_lines():
    payload = {"status": "success", "outcomes": {
        "12": {"labeled": "correct", "promoted": True, "edges_updated": 1},
        "13": {"labeled": "incorrect", "edge_deleted": 1},
        "99": {"error": "ledger row not found"},
    }}
    out = memory_bridge.format_label_outcomes(payload)
    assert "id 12: labeled correct, PROMOTED to operator-asserted (edges updated: 1)" in out
    assert "id 13: labeled incorrect, machine edge(s) deleted: 1" in out
    assert "id 99: ERROR: ledger row not found" in out
