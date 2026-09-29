"""Tests for supersession last ground refusal and acknowledgement (PR 2b).

Rule: superseding a decision's last standing fact is refused (409 decision_loses_last_ground)
until the operator answers.
Records: decision:2802, fact:2800, decision:2801, decision:2774, decision:2751,
         operator rulings R1 + R2 (fact:2809, retrospective:2810).

Every test names the mutation it kills.
Unit tests never reach the network.
"""

import importlib.util
import inspect
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def load_coordinator():
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    path = os.path.join(scripts_dir, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["coordinator"] = mod
    spec.loader.exec_module(mod)
    return mod


coordinator_mod = load_coordinator()
MemoryCoordinator = coordinator_mod.MemoryCoordinator
threads_left_ungrounded = coordinator_mod.threads_left_ungrounded
SUPERSEDE_LOCK_KEY = coordinator_mod.SUPERSEDE_LOCK_KEY


class _async_ctx:
    def __init__(self, val):
        self._val = val

    async def __aenter__(self):
        return self._val

    async def __aexit__(self, *_):
        pass


def _make_request(body: dict, principal: dict | None = None, authenticated_agent: str | None = "claude") -> MagicMock:
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.rel_url.query.get = MagicMock(return_value=None)
    data = {
        "authenticated_agent": authenticated_agent,
        "principal": principal,
    }
    req.get = MagicMock(side_effect=lambda k, d=None: data.get(k, d))
    req.__getitem__ = MagicMock(side_effect=lambda k: data[k])
    return req


def _coord():
    c = MemoryCoordinator()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": 100})
    conn.fetchval = AsyncMock(return_value=1)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.transaction = MagicMock(return_value=_async_ctx(None))
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_async_ctx(conn))
    c._pool = pool
    session = AsyncMock()
    session.run = AsyncMock()
    neo4j = MagicMock()
    neo4j.session = MagicMock(return_value=_async_ctx(session))
    c._neo4j = neo4j
    return c, conn, session


class _recording_tx:
    """Records the exception that left the block. A normal return commits; a raise rolls back."""

    def __init__(self):
        self.exc_type = None

    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        self.exc_type = exc_type
        return False


def _losing_decision(did=2522, title="Use SQLite WAL mode", visibility="global",
                     agent_id="claude", rationale="In context of deadlocks, chose WAL mode"):
    return {
        "id": did,
        "title": title,
        "rationale": rationale,
        "visibility": visibility,
        "agent_id": agent_id,
        "scope": "global",
        "retrospectives": [],
        "grounds": [{"id": 2672, "type": "fact", "superseded": False, "role": "based_on", "exists": True}],
    }


def _load_py(mod_name: str, path: str):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_memory_bridge(mod_name: str, path: str):
    """Load a client copy without reading a skill or gateway .env."""
    saved = os.environ.get("SECURE_ENV_FILE", None)
    os.environ["SECURE_ENV_FILE"] = ""
    try:
        return _load_py(mod_name, path)
    finally:
        if saved is None:
            os.environ.pop("SECURE_ENV_FILE", None)
        else:
            os.environ["SECURE_ENV_FILE"] = saved


def _load_vector_skill(mod_name: str = "vector_skill_fr2b"):
    """Load the MCP client without reading mcp/.env."""
    saved = os.environ.get("VECTOR_SKILL_ENV", None)
    os.environ["VECTOR_SKILL_ENV"] = "/tmp/fr2b-no-such-env"
    try:
        path = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "mcp", "vector-skill.py"))
        return _load_py(mod_name, path)
    finally:
        if saved is None:
            os.environ.pop("VECTOR_SKILL_ENV", None)
        else:
            os.environ["VECTOR_SKILL_ENV"] = saved


def _http(status: int, body: dict) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=body)
    r.text = json.dumps(body)
    return r


# ── T1 Pure ───────────────────────────────────────────────────────────────────

def test_t1_pure_thread_single_ground_is_ungrounded():
    """T1 Pure: D grounded [N] -> ungrounded.
    Kills mutation: count N itself as standing.
    """
    rows = [
        {
            "id": 2522,
            "title": "Use SQLite WAL mode",
            "rationale": "In context of deadlocks, chose WAL mode",
            "grounds": [
                {"id": 2672, "type": "fact", "superseded": False, "role": "based_on", "exists": True}
            ],
            "retrospectives": [],
        }
    ]
    ungrounded = threads_left_ungrounded(rows, fact_id=2672)
    assert len(ungrounded) == 1
    assert ungrounded[0]["id"] == 2522


# ── T2 Pure ───────────────────────────────────────────────────────────────────

def test_t2_pure_thread_reduced_support_passes():
    """T2 Pure: D grounded [N, M], M a standing fact -> passes (reduced support).
    Kills mutation: fire on any thread citing N (the rejected alternative).
    """
    rows = [
        {
            "id": 2522,
            "title": "Use SQLite WAL mode",
            "rationale": "In context of deadlocks, chose WAL mode",
            "grounds": [
                {"id": 2672, "type": "fact", "superseded": False, "role": "based_on", "exists": True},
                {"id": 2675, "type": "fact", "superseded": False, "role": "based_on", "exists": True},
            ],
            "retrospectives": [],
        }
    ]
    ungrounded = threads_left_ungrounded(rows, fact_id=2672)
    assert len(ungrounded) == 0


# ── T3 Pure ───────────────────────────────────────────────────────────────────

def test_t3_pure_thread_superseded_sibling_leaves_ungrounded():
    """T3 Pure: Same, M superseded -> ungrounded.
    Kills mutation: ignore `superseded`.
    """
    rows = [
        {
            "id": 2522,
            "title": "Use SQLite WAL mode",
            "rationale": "In context of deadlocks, chose WAL mode",
            "grounds": [
                {"id": 2672, "type": "fact", "superseded": False, "role": "based_on", "exists": True},
                {"id": 2675, "type": "fact", "superseded": True, "role": "based_on", "exists": True},
            ],
            "retrospectives": [],
        }
    ]
    ungrounded = threads_left_ungrounded(rows, fact_id=2672)
    assert len(ungrounded) == 1
    assert ungrounded[0]["id"] == 2522


# ── T4 Pure ───────────────────────────────────────────────────────────────────

def test_t4_pure_decision_grounds_and_missing_rows_do_not_count():
    """T4 Pure: D grounded [N, D2] -> ungrounded (decision grounds do not count);
    a type-null ground counts as a fact; missing ground row = not standing (Opus R4).
    Kills mutation: count every ground.
    """
    # D2 is a decision, does not count
    rows_d2 = [
        {
            "id": 2522,
            "grounds": [
                {"id": 2672, "type": "fact", "superseded": False, "role": "based_on", "exists": True},
                {"id": 3000, "type": "decision", "superseded": False, "role": "based_on", "exists": True},
            ],
        }
    ]
    assert len(threads_left_ungrounded(rows_d2, fact_id=2672)) == 1

    # Type None counts as a fact
    rows_null_type = [
        {
            "id": 2522,
            "grounds": [
                {"id": 2672, "type": "fact", "superseded": False, "role": "based_on", "exists": True},
                {"id": 3001, "type": None, "superseded": False, "role": "based_on", "exists": True},
            ],
        }
    ]
    assert len(threads_left_ungrounded(rows_null_type, fact_id=2672)) == 0

    # Missing row in technical_docs (exists=False) does not count as standing (Opus R4)
    rows_missing = [
        {
            "id": 2522,
            "grounds": [
                {"id": 2672, "type": "fact", "superseded": False, "role": "based_on", "exists": True},
                {"id": 3002, "type": "fact", "superseded": False, "role": "based_on", "exists": False},
            ],
        }
    ]
    assert len(threads_left_ungrounded(rows_missing, fact_id=2672)) == 1


# ── T5 Pure ───────────────────────────────────────────────────────────────────

def test_t5_pure_retrospective_grounds_in_thread():
    """T5 Pure: Retrospective grounds: D [N] + retro [M] -> passes.
    D [M' superseded] + retro [N] -> ungrounded.
    Kills mutation: drop retrospective grounds from the thread.
    """
    # D citing N, but retrospective on D cites standing M -> thread has standing ground M
    rows_pass = [
        {
            "id": 2522,
            "grounds": [
                {"id": 2672, "type": "fact", "superseded": False, "role": "based_on", "exists": True},
                {"id": 4000, "type": "fact", "superseded": False, "role": "based_on", "exists": True},
            ],
            "retrospectives": [5000],
        }
    ]
    assert len(threads_left_ungrounded(rows_pass, fact_id=2672)) == 0

    # D citing M' (superseded), retro citing N -> superseding N leaves thread with only M' (superseded) -> ungrounded
    rows_fail = [
        {
            "id": 2522,
            "grounds": [
                {"id": 3999, "type": "fact", "superseded": True, "role": "based_on", "exists": True},
                {"id": 2672, "type": "fact", "superseded": False, "role": "based_on", "exists": True},
            ],
            "retrospectives": [5001],
        }
    ]
    assert len(threads_left_ungrounded(rows_fail, fact_id=2672)) == 1


# ── R1 Pure ───────────────────────────────────────────────────────────────────

def test_r1_only_based_on_facts_count_as_ground():
    """R1: Only based_on facts count as a ground. An informed_by fact never keeps
    a decision supported.
    Kills mutation: treat informed_by or non-based_on roles as standing grounds.
    """
    # Ground M has explicit role 'informed_by' -> fails to keep decision standing
    rows_informed = [
        {
            "id": 2709,
            "grounds": [
                {"id": 2672, "type": "fact", "superseded": False, "role": "based_on", "exists": True},
                {"id": 2673, "type": "fact", "superseded": False, "role": "informed_by", "exists": True},
            ],
        }
    ]
    assert len(threads_left_ungrounded(rows_informed, fact_id=2672)) == 1

    # Ground M has explicit role 'based_on' -> keeps decision standing
    rows_based = [
        {
            "id": 2709,
            "grounds": [
                {"id": 2672, "type": "fact", "superseded": False, "role": "based_on", "exists": True},
                {"id": 2673, "type": "fact", "superseded": False, "role": "based_on", "exists": True},
            ],
        }
    ]
    assert len(threads_left_ungrounded(rows_based, fact_id=2672)) == 0

    # Ground M has no explicit role, source_ref defaults to discussion -> informed_by -> ungrounded
    rows_discussion_default = [
        {
            "id": 1250,
            "grounds": [
                {"id": 2672, "type": "fact", "superseded": False, "role": "based_on", "exists": True},
                {"id": 2674, "type": "fact", "superseded": False, "source_ref": "discussion_context", "exists": True},
            ],
        }
    ]
    assert len(threads_left_ungrounded(rows_discussion_default, fact_id=2672)) == 1

    # Ground M has no explicit role, source_ref is a code file -> measured -> based_on -> standing
    rows_measured_default = [
        {
            "id": 1250,
            "grounds": [
                {"id": 2672, "type": "fact", "superseded": False, "role": "based_on", "exists": True},
                {"id": 2674, "type": "fact", "superseded": False, "source_ref": "src/coordinator.py", "exists": True},
            ],
        }
    ]
    assert len(threads_left_ungrounded(rows_measured_default, fact_id=2672)) == 0


# ── T6 Handler ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_t6_supersede_refusal_writes_nothing_and_fires_pre_check():
    """T6 Handler: /memory/supersede, _thread_grounds patched to an ungrounded D ->
    409 with exact payload, conn.transaction never entered, no UPDATE/outbox INSERT.
    Kills mutation: delete the pre-check.
    """
    c, conn, _ = _coord()
    # Pre-check target fact lookup returns standing fact
    conn.fetchrow = AsyncMock(return_value={"superseded": False, "type": "fact"})

    dummy_thread = [
        {
            "id": 2522,
            "title": "Use SQLite WAL mode",
            "rationale": "In context of deadlocks, chose WAL mode",
            "visibility": "global",
            "agent_id": "claude",
            "scope": "global",
            "retrospectives": [2530],
            "grounds": [
                {"id": 2672, "type": "fact", "superseded": False, "role": "based_on", "exists": True}
            ],
        }
    ]

    with patch.object(c, "_thread_grounds", new=AsyncMock(return_value=dummy_thread)):
        req = _make_request({"pg_id": 2672})
        resp = await c.handle_supersede(req)

    assert resp.status == 409
    body = json.loads(resp.text)
    assert body["status"] == "error"
    assert body["error"] == "decision_loses_last_ground"
    assert body["fact"] == 2672
    assert body["stage"] == "pre-check"
    assert body["decisions_needing_answer"] == [2522]
    assert "acknowledge_standing" not in body
    assert ":based_on" in body["message"]
    assert len(body["decisions"]) == 1
    assert body["decisions"][0]["pg_id"] == 2522
    assert body["decisions"][0]["title"] == "Use SQLite WAL mode"
    assert body["decisions"][0]["rationale"] == "In context of deadlocks, chose WAL mode"

    # conn.transaction was NEVER entered, no UPDATE or INSERT was executed
    conn.transaction.assert_not_called()
    for call in conn.execute.call_args_list:
        sql = str(call.args[0])
        assert "UPDATE" not in sql
        assert "INSERT" not in sql


# ── T7 Handler ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_t7_acknowledgement_handling_and_metadata():
    """T7 Handler: Acknowledgement [D] >= ungrounded -> UPDATE carries
    supersession_ack.decisions == [D] and the attested principal.
    Partial acknowledgement -> 409 listing only the rest.
    true -> 400.
    Kills mutation: accept any non-empty acknowledgement.
    """
    c, conn, _ = _coord()
    dummy_threads = [
        {
            "id": 2522,
            "title": "Decision 1",
            "rationale": "Rationale 1",
            "visibility": "global",
            "agent_id": "claude",
            "scope": "global",
            "retrospectives": [],
            "grounds": [{"id": 2672, "type": "fact", "superseded": False, "role": "based_on", "exists": True}],
        },
        {
            "id": 2675,
            "title": "Decision 2",
            "rationale": "Rationale 2",
            "visibility": "global",
            "agent_id": "claude",
            "scope": "global",
            "retrospectives": [],
            "grounds": [{"id": 2672, "type": "fact", "superseded": False, "role": "based_on", "exists": True}],
        },
    ]

    # Sub-case A: true -> 400 (shape error)
    conn.fetchrow = AsyncMock(return_value={"superseded": False, "type": "fact"})
    with patch.object(c, "_thread_grounds", new=AsyncMock(return_value=dummy_threads)):
        req_bad = _make_request({"pg_id": 2672, "acknowledge_standing": True})
        resp_bad = await c.handle_supersede(req_bad)
        assert resp_bad.status == 400

    # Sub-case B: Partial acknowledgement -> 409 listing only the rest
    with patch.object(c, "_thread_grounds", new=AsyncMock(return_value=dummy_threads)):
        req_partial = _make_request({
            "pg_id": 2672,
            "acknowledge_standing": {"2522": "Operator confirmed 2522 still stands"},
        })
        resp_partial = await c.handle_supersede(req_partial)
        assert resp_partial.status == 409
        body_partial = json.loads(resp_partial.text)
        assert body_partial["error"] == "decision_loses_last_ground"
        assert body_partial["decisions_needing_answer"] == [2675]
        assert "acknowledge_standing" not in body_partial
        assert [d["pg_id"] for d in body_partial["decisions"]] == [2675]

    # Sub-case C: Complete acknowledgement [2522, 2675] -> UPDATE carries supersession_ack.decisions == [2522, 2675]
    conn.fetchrow = AsyncMock(side_effect=[
        {"superseded": False, "type": "fact"},  # pre-check
        {"superseded": False},                   # in-transaction FOR UPDATE
    ])
    with patch.object(c, "_thread_grounds", new=AsyncMock(return_value=dummy_threads)):
        req_full = _make_request(
            {
                "pg_id": 2672,
                "acknowledge_standing": {
                    "2522": "Operator confirmed 2522",
                    "2675": "Operator confirmed 2675",
                },
            },
            principal={"user": "operator_xenofon"},
        )
        resp_full = await c.handle_supersede(req_full)
        assert resp_full.status == 200
        body_full = json.loads(resp_full.text)
        assert body_full["status"] == "success"
        assert body_full["acknowledged_standing"] == [2522, 2675]

        # Verify UPDATE query execution and payload
        update_calls = [call for call in conn.execute.call_args_list if "UPDATE technical_docs" in str(call.args[0])]
        assert len(update_calls) == 1
        update_args = update_calls[0].args
        assert update_args[1] == 2672
        assert update_args[2] is None
        # $3 is the dict the pool's jsonb codec encodes. A str here is the double-encode.
        ack_payload = update_args[3]
        assert isinstance(ack_payload, dict)
        sql = str(update_args[0])
        assert "NOT superseded" in sql
        assert "COALESCE(metadata, '{}'::jsonb)" in sql
        assert ack_payload["decisions"] == [2522, 2675]
        assert ack_payload["acknowledged_by"] == "operator_xenofon"
        assert ack_payload["answers"]["2522"] == "Operator confirmed 2522"
        assert ack_payload["answers"]["2675"] == "Operator confirmed 2675"


# ── T8 Handler ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_t8_save_path_refusal_and_ack_handling():
    """T8 Handler: Save path: refused with _embed and _commit_axis_registrations
    not awaited; an acknowledged save's INSERT metadata lacks acknowledge_standing.
    Kills mutation: move the check after the axis commit.
    """
    c, conn, _ = _coord()
    dummy_thread = [
        {
            "id": 2522,
            "title": "Use SQLite WAL mode",
            "rationale": "In context of deadlocks, chose WAL mode",
            "visibility": "global",
            "agent_id": "claude",
            "scope": "global",
            "retrospectives": [],
            "grounds": [{"id": 2672, "type": "fact", "superseded": False, "role": "based_on", "exists": True}],
        }
    ]

    # Save target fact 2672 is not superseded
    conn.fetchrow = AsyncMock(return_value={"superseded": False, "type": "fact"})

    embed_mock = AsyncMock(return_value=[0.1] * 1024)
    axis_commit_mock = AsyncMock()

    with patch.object(c, "_thread_grounds", new=AsyncMock(return_value=dummy_thread)), \
         patch.object(c, "_embed", new=embed_mock), \
         patch.object(c, "_commit_axis_registrations", new=axis_commit_mock):

        # 1. Unacknowledged save -> 409
        req = _make_request({
            "content": "new correction fact",
            "metadata": {
                "project": "shared-memory-GitHub",
                "source": "claude",
                "supersedes": 2672,
            },
        })
        resp = await c.handle_save(req)
        assert resp.status == 409
        assert json.loads(resp.text)["error"] == "decision_loses_last_ground"
        # Neither _embed nor _commit_axis_registrations was called
        embed_mock.assert_not_called()
        axis_commit_mock.assert_not_called()

        # 2. Acknowledged save -> passes, INSERT metadata lacks acknowledge_standing and supersession_ack
        conn.fetchrow = AsyncMock(side_effect=[
            {"superseded": False, "type": "fact"},  # pre-check
            {"superseded": False},                   # in-transaction FOR UPDATE
            {"id": 999},                             # INSERT RETURNING id
        ])
        req_ack = _make_request({
            "content": "new correction fact",
            "metadata": {
                "project": "shared-memory-GitHub",
                "source": "claude",
                "supersedes": 2672,
                "acknowledge_standing": {"2522": "Still stands"},
                "supersession_ack": {"forged": True},  # Attempted forgery
            },
        })
        resp_ack = await c.handle_save(req_ack)
        assert resp_ack.status == 200
        assert embed_mock.called
        assert axis_commit_mock.called

        # Verify inserted row's metadata argument
        insert_calls = [call for call in conn.fetchrow.call_args_list if "INSERT INTO technical_docs" in str(call.args[0])]
        assert len(insert_calls) == 1
        inserted_metadata = insert_calls[0].args[2]
        assert "acknowledge_standing" not in inserted_metadata
        assert "supersession_ack" not in inserted_metadata
        insert_sql = str(insert_calls[0].args[0])
        assert "metadata ? 'supersession_ack'" in insert_sql

        update_calls = [
            call for call in conn.execute.call_args_list
            if "UPDATE technical_docs" in str(call.args[0])
        ]
        assert len(update_calls) == 1
        update_sql = str(update_calls[0].args[0])
        assert "NOT superseded" in update_sql
        assert "COALESCE(metadata, '{}'::jsonb)" in update_sql
        assert isinstance(update_calls[0].args[3], dict)
        assert update_calls[0].args[3]["answers"]["2522"] == "Still stands"


# ── T9 Handler ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_t9_in_transaction_order_and_advisory_lock():
    """T9 Handler: In-transaction order: advisory lock -> FOR UPDATE -> check -> UPDATE.
    Kills mutation: delete the lock call.
    """
    c, conn, _ = _coord()
    conn.fetchrow = AsyncMock(side_effect=[
        {"superseded": False, "type": "fact"},  # pre-check
        {"superseded": False},                   # in-transaction FOR UPDATE
    ])

    call_order = []

    async def mock_execute(sql, *args):
        if "pg_advisory_xact_lock" in str(sql):
            call_order.append(("lock", args[0]))
        elif "UPDATE technical_docs" in str(sql):
            call_order.append(("update", args[0]))
        return "UPDATE 1"

    async def mock_fetchrow(sql, *args):
        if "FOR UPDATE" in str(sql):
            call_order.append(("for_update", args[0]))
            return {"superseded": False}
        return {"superseded": False, "type": "fact"}

    conn.execute = AsyncMock(side_effect=mock_execute)
    conn.fetchrow = AsyncMock(side_effect=mock_fetchrow)

    with patch.object(c, "_thread_grounds", new=AsyncMock(return_value=[])):
        req = _make_request({"pg_id": 2672})
        resp = await c.handle_supersede(req)
        assert resp.status == 200

    order_names = [item[0] for item in call_order]
    assert order_names == ["lock", "for_update", "update"]
    assert call_order[0][1] == SUPERSEDE_LOCK_KEY


# ── T10 Clients ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_t10_clients_preserve_409_rationale():
    """T10 Clients: mb returns decisions[0]["rationale"] intact from a 409 body;
    vs renders the title and rationale; a bare id is refused and words stay a map.
    Kills mutation: fall back to _reply_json -> rationale gone -> dies.
    """
    mb_path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts", "memory_bridge.py")
    )
    mb_mod = _load_memory_bridge("memory_bridge_t10", mb_path)

    # 1. Test mb supersede_fact preserves full 409 body
    mock_resp_409 = MagicMock()
    mock_resp_409.status_code = 409
    payload_409 = {
        "status": "error",
        "error": "decision_loses_last_ground",
        "fact": 2672,
        "decisions": [
            {
                "ref": "decision:2522",
                "pg_id": 2522,
                "title": "Ship skill index",
                "rationale": "Untruncated critical rationale that must not be lost",
            }
        ],
        "decisions_needing_answer": [2522],
    }
    mock_resp_409.json = MagicMock(return_value=payload_409)

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp_409)
    with patch.object(mb_mod, "_async_client", return_value=_async_ctx(mock_client)):
        res = await mb_mod.supersede_fact(2672)
        assert res["status"] == "error"
        assert res["error"] == "decision_loses_last_ground"
        assert res["decisions"][0]["rationale"] == "Untruncated critical rationale that must not be lost"

    # 2. Bare ids, empty words, placeholders, and colon tokens are refused. Words stay a map.
    with pytest.raises(ValueError):
        mb_mod._normalize_cli_ack([["5"], ["7"]])
    with pytest.raises(ValueError):
        mb_mod._normalize_cli_ack([["5", "   "]])
    with pytest.raises(ValueError):
        mb_mod._normalize_cli_ack([["5", "Operator acknowledged"]])
    with pytest.raises(ValueError) as colon_exc:
        mb_mod._normalize_cli_ack([["5:words", "still stands"]])
    assert colon_exc.value.__cause__ is None
    assert "id:answer" in str(colon_exc.value)

    sargs_words = mb_mod._normalize_cli_ack([["5", "stands firmly"], ["7", "verified again"]])
    assert sargs_words == {5: "stands firmly", 7: "verified again"}

    # 3. Test vector_skill render
    vs_mod = _load_vector_skill("vector_skill_t10")

    rendered = vs_mod._render_decision_loses_last_ground(payload_409)
    assert "Ship skill index" in rendered
    assert "Untruncated critical rationale that must not be lost" in rendered
    assert "decision:2522" in rendered
    assert "provide acknowledge_standing with:" not in rendered
    assert "save the new fact first" in rendered
    assert "NEW_ID:based_on" in rendered
    assert "decision:2802" in rendered
    assert "fact:2809" in rendered
    assert "decisions_needing_answer" in inspect.getsource(vs_mod._render_decision_loses_last_ground)
    assert 'get("acknowledge_standing"' not in inspect.getsource(vs_mod._render_decision_loses_last_ground)

    # 4. Test vector_skill supersede tool
    with patch.object(vs_mod.httpx.AsyncClient, "post", return_value=mock_resp_409):
        res_vs = await vs_mod.supersede(2672)
        assert "Refusal (HTTP 409 decision_loses_last_ground)" in res_vs
        assert "Ship skill index" in res_vs
        assert "Untruncated critical rationale that must not be lost" in res_vs

    # 5. Test vector_skill save_artifact tool
    with patch.object(vs_mod.httpx.AsyncClient, "post", return_value=mock_resp_409):
        res_save = await vs_mod.save_artifact(
            "some content",
            json.dumps({"source": "claude", "project": "shared-memory", "supersedes": 2672})
        )
        assert "Refusal (HTTP 409 decision_loses_last_ground)" in res_save
        assert "Ship skill index" in res_save
        assert "Untruncated critical rationale that must not be lost" in res_save


# ── T11 Documentation ─────────────────────────────────────────────────────────

def test_t11_documentation_names_refusal_code():
    """T11 Documentation: system-prompt.md and both USAGE.md copies name the code.
    Kills mutation: omit error code from docs.
    """
    root = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
    sys_prompt = open(os.path.join(root, "mcp", "system-prompt.md"), encoding="utf-8").read()
    usage1 = open(os.path.join(root, "shared-memory", "USAGE.md"), encoding="utf-8").read()
    usage2 = open(os.path.join(root, "shared-memory-skill", "shared-memory", "USAGE.md"), encoding="utf-8").read()

    assert "decision_loses_last_ground" in sys_prompt
    assert "decision_loses_last_ground" in usage1
    assert "decision_loses_last_ground" in usage2
    assert usage1 == usage2
    assert "decision:2802" in usage1 and "fact:2809" in usage1
    assert "decision:2751" not in usage1
    assert 'NEW_ID:based_on' in usage1
    assert "decision_not_visible" in usage1
    assert "not a role refusal" in usage1
    assert "decisions_needing_answer" in usage1
    assert "There is no `id:answer` syntax" in usage1
    assert "(or `--acknowledge-standing ID" not in usage1

    skill1 = open(os.path.join(root, "shared-memory", "SKILL.md"), encoding="utf-8").read()
    skill2 = open(os.path.join(root, "shared-memory-skill", "shared-memory", "SKILL.md"), encoding="utf-8").read()
    assert skill1 == skill2
    assert '--acknowledge-standing ID "operator\'s words"' in skill1
    assert "--acknowledge-standing ID]" not in skill1
    assert "Decision id(s) or" not in skill1
    assert "decision_not_visible" in skill1
    assert "Save the new fact first" in skill1
    assert 'NEW_ID:based_on' in skill1
    assert "decisions_needing_answer" in skill1
    assert "decision:2802" in skill1 and "fact:2809" in skill1
    assert "decision:2751" not in skill1

    assert "decision:2751" not in sys_prompt
    assert "list of IDs" not in sys_prompt
    assert "save the new fact first" in sys_prompt
    assert "NEW_ID:based_on" in sys_prompt
    assert "decision_not_visible" in sys_prompt
    assert "not a role refusal" in sys_prompt
    assert "decisions_needing_answer" in sys_prompt
    assert "decision:2802" in sys_prompt and "fact:2809" in sys_prompt

    snippet = open(os.path.join(root, "mcp", "CONSTITUTION_SNIPPET_MCP.md"), encoding="utf-8").read()
    assert "decision:2802" in snippet and "fact:2809" in snippet
    assert "decision:2751" not in snippet
    assert "save the new fact" in snippet
    assert "NEW_ID:based_on" in snippet
    assert "decision_not_visible" in snippet
    assert "not a role refusal" in snippet
    assert "decisions_needing_answer" in snippet

    schema1 = open(os.path.join(root, "shared-memory", "Documentation", "schema.md"), encoding="utf-8").read()
    schema2 = open(
        os.path.join(root, "shared-memory-skill", "shared-memory", "Documentation", "schema.md"),
        encoding="utf-8",
    ).read()
    assert schema1 == schema2
    assert "as a JSON object" in schema1
    assert "decision:2802" in schema1 and "fact:2809" in schema1


# ── Double Supersession & Control Chars & Visibility ──────────────────────────

@pytest.mark.asyncio
async def test_double_supersession_both_stages_both_paths():
    """Under the same lock, supersede of an already superseded fact is refused (409 fact_already_superseded).
    Kills mutation: allow double supersession or return generic 400.
    """
    c, conn, _ = _coord()

    # Pre-check supersede: target already superseded -> 409 fact_already_superseded
    conn.fetchrow = AsyncMock(return_value={"superseded": True, "type": "fact"})
    resp_sup_pre = await c.handle_supersede(_make_request({"pg_id": 5}))
    assert resp_sup_pre.status == 409
    assert json.loads(resp_sup_pre.text)["error"] == "fact_already_superseded"

    # Pre-check save: supersedes target already superseded -> 409 fact_already_superseded
    resp_save_pre = await c.handle_save(_make_request({
        "content": "x",
        "metadata": {"project": "p", "source": "s", "supersedes": 5},
    }))
    assert resp_save_pre.status == 409
    assert json.loads(resp_save_pre.text)["error"] == "fact_already_superseded"


def test_control_character_stripping():
    """Control characters must be stripped from quoted title and rationale in refusal.
    Kills mutation: keep control characters in refusal payload.
    """
    clean_fn = coordinator_mod._clean_control_chars
    dirty = "Title\x1b[31m with escape\x00 and \x07bell\nkeep newline\tkeep tab"
    cleaned = clean_fn(dirty)
    assert "\x1b" not in cleaned
    assert "\x00" not in cleaned
    assert "\x07" not in cleaned
    assert "\n" in cleaned
    assert "\t" in cleaned
    assert "Title" in cleaned


@pytest.mark.asyncio
async def test_lineage_returns_supersession_ack():
    """Lineage (handle_status) returns supersession_ack from metadata.
    Kills mutation: omit supersession_ack from lineage status response.
    """
    c, conn, _ = _coord()
    rec_row = {
        "type": "fact",
        "created_at": None,
        "superseded": True,
        "superseded_by": 2680,
        "grounded_in": None,
        "supersession_ack": {
            "decisions": [2522],
            "acknowledged_by": "operator",
        },
    }
    conn.fetchrow = AsyncMock(side_effect=[rec_row, None])

    req = MagicMock()
    req.match_info = {"pg_id": "fact:2672"}
    req.rel_url.query = {}
    req.get = MagicMock(return_value=None)

    resp = await c.handle_status(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["pg_id"] == 2672
    assert body["record_type"] == "fact"
    assert body["superseded"] is True
    assert body["superseded_by"] == 2680
    assert "supersession_ack" in body
    assert body["supersession_ack"] == {
        "decisions": [2522],
        "acknowledged_by": "operator",
    }


def test_visibility_filter_pure():
    """_is_doc_visible respects private/scope visibility.
    Kills mutation: leak private decision titles across agent boundaries.
    """
    is_vis = coordinator_mod._is_doc_visible
    # global visibility is visible to anyone (viewer or anonymous)
    assert is_vis("global", "agent_a", "global", "agent_b", None) is True
    assert is_vis("global", "agent_a", "project_x", "agent_b", "project_x") is True
    assert is_vis("global", "agent_a", "project_x", None, None) is True

    # private visibility is visible only to owning agent
    assert is_vis("private", "agent_a", "global", "agent_a", None) is True
    assert is_vis("private", "agent_a", "global", "agent_b", None) is False

    # scope visibility matches principal scope
    assert is_vis("scope", "agent_a", "project_x", "agent_b", "project_x") is True
    assert is_vis("scope", "agent_a", "project_x", "agent_b", "project_y") is False
    assert is_vis("scope", "agent_a", "project_x", "agent_b", None) is False

    # anonymous caller (viewer is None) cannot see private or scoped docs
    assert is_vis("private", "agent_a", "global", None, None) is False
    assert is_vis("scope", "agent_a", "project_x", None, "project_x") is False


def _root() -> str:
    return os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


def _bridge_paths() -> list[str]:
    root = _root()
    return [
        os.path.join(root, "shared-memory", "scripts", "memory_bridge.py"),
        os.path.join(root, "shared-memory-skill", "shared-memory", "scripts", "memory_bridge.py"),
    ]


async def _fetchrow_success(sql, *args):
    text = str(sql)
    if "INSERT INTO technical_docs" in text:
        return {"id": 999}
    if "FOR UPDATE" in text:
        return {"superseded": False}
    return {"superseded": False, "type": "fact"}


# ── M2b / M3b: _thread_grounds role resolution ───────────────────────────────

@pytest.mark.asyncio
async def test_thread_grounds_resolves_roles_like_the_edge():
    """Explicit informed_by stays informed_by. No role uses the fact_kind default.
    An unknown explicit word falls back the same way. Kills M2b and M3b.
    """
    c, conn, _ = _coord()

    def dec(did, gid, roles=None):
        meta = {"type": "decision", "grounded_in": [gid], "decision": {"title": f"D{did}", "rationale": "r"}}
        if roles is not None:
            meta["grounded_roles"] = roles
        return {
            "id": did, "content": f"D{did} title", "metadata": meta,
            "agent_id": "claude", "scope": "global", "visibility": "global",
        }

    decisions = [
        dec(1, 10, {"10": "informed_by"}),
        dec(2, 11),
        dec(3, 12),
        dec(4, 13, {"13": "not_a_role"}),
        dec(5, 14),
        dec(6, 16, {"16": "not_a_role"}),
    ]
    retros = [{
        "id": 90,
        "target_pg_id": 1,
        "metadata": {
            "type": "retrospective",
            "target_pg_id": 1,
            "grounded_in": [15],
            "grounded_roles": {"15": "informed_by"},
        },
    }]
    grounds = [
        {"id": 10, "type": "fact", "source_ref": "shared-memory/scripts/coordinator.py", "superseded": False},
        {"id": 11, "type": "fact", "source_ref": "discussion_context", "superseded": False},
        {"id": 12, "type": "fact", "source_ref": "tests/test_supersession_last_ground.py", "superseded": False},
        {"id": 13, "type": "fact", "source_ref": "tests/test_foo.py", "superseded": False},
        {"id": 14, "type": "fact", "source_ref": "shared-memory/scripts/coordinator.py", "superseded": False},
        {"id": 15, "type": "fact", "source_ref": "shared-memory/scripts/coordinator.py", "superseded": False},
        {"id": 16, "type": "fact", "source_ref": "discussion_context", "superseded": False},
    ]
    conn.fetch = AsyncMock(side_effect=[decisions, retros, grounds])
    rows = await c._thread_grounds(conn, 10)
    role = {}
    for row in rows:
        for g in row["grounds"]:
            role[(row["id"], g["id"])] = g["role"]
    assert role[(1, 10)] == "informed_by"
    assert role[(1, 15)] == "informed_by"
    assert role[(2, 11)] == "informed_by"
    assert role[(3, 12)] == "based_on"
    assert role[(4, 13)] == "based_on"
    assert role[(5, 14)] == "based_on"
    assert role[(6, 16)] == "informed_by"


@pytest.mark.asyncio
async def test_retrospective_based_on_keeps_a_fact_the_decision_lists_as_informed_by():
    """A decision's informed_by citation must not hide a standing retrospective's based_on of the same fact.
    Superseding the decision's other based_on fact then passes: the thread still stands, so no 409.
    Kills mutation: keep the first role seen and ignore a later based_on.
    """
    c, conn, _ = _coord()
    decision_id = 2522
    losing = 2672
    kept = 2673

    def decision_row(roles):
        return {
            "id": decision_id,
            "content": "Keep the index\nbecause the split shipped",
            "metadata": {
                "type": "decision",
                "grounded_in": [losing, kept],
                "grounded_roles": roles,
                "decision": {"title": "Keep the index", "rationale": "the split shipped"},
            },
            "agent_id": "claude",
            "scope": "global",
            "visibility": "global",
        }

    def retro_row(roles):
        return {
            "id": 9001,
            "target_pg_id": decision_id,
            "metadata": {
                "type": "retrospective",
                "target_pg_id": decision_id,
                "grounded_in": [kept],
                "grounded_roles": roles,
            },
        }

    grounds = [
        {"id": losing, "type": "fact", "source_ref": "discussion_context", "superseded": False},
        {"id": kept, "type": "fact", "source_ref": "discussion_context", "superseded": False},
    ]

    async def fetch(sql, *args):
        text = str(sql)
        if "source_ref" in text:
            return grounds
        if "FROM technical_docs d" in text:
            return [decision_row({"2672": "based_on", "2673": "informed_by"})]
        if "ANY($1::bigint[])" in text:
            return [retro_row({"2673": "based_on"})]
        return []

    async def fetchrow(sql, *args):
        if "FOR UPDATE" in str(sql):
            return {"superseded": False}
        return {"superseded": False, "type": "fact"}

    conn.fetch = AsyncMock(side_effect=fetch)
    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetchval = AsyncMock(return_value=None)

    rows = await c._thread_grounds(conn, losing)
    role = {g["id"]: g["role"] for g in rows[0]["grounds"]}
    assert role[kept] == "based_on"
    assert role[losing] == "based_on"

    # The other order must not downgrade: a retrospective's informed_by cannot erase based_on.
    async def fetch_keep(sql, *args):
        text = str(sql)
        if "source_ref" in text:
            return grounds
        if "FROM technical_docs d" in text:
            return [decision_row({"2672": "based_on", "2673": "based_on"})]
        if "ANY($1::bigint[])" in text:
            return [retro_row({"2673": "informed_by"})]
        return []

    conn.fetch = AsyncMock(side_effect=fetch_keep)
    kept_rows = await c._thread_grounds(conn, losing)
    kept_role = {g["id"]: g["role"] for g in kept_rows[0]["grounds"]}
    assert kept_role[kept] == "based_on"

    conn.fetch = AsyncMock(side_effect=fetch)
    resp = await c.handle_supersede(_make_request({"pg_id": losing}))
    body = json.loads(resp.text)
    assert resp.status != 409
    assert body.get("error") != "decision_loses_last_ground"
    assert resp.status == 200


def test_target_pg_id_cast_rejects_non_integers():
    """A retrospective target casts only as a digit string. 1.5 and 1e20 must not reach ::bigint.
    Kills mutation: guard with jsonb_typeof = number, which still admits both.
    """
    import re
    src = inspect.getsource(MemoryCoordinator._thread_grounds)
    needle = (
        "CASE WHEN (r.metadata->>'target_pg_id') ~ '^[0-9]+$' "
        "AND length(r.metadata->>'target_pg_id') <= 18 "
        "THEN (r.metadata->>'target_pg_id')::bigint END"
    )
    assert src.count(needle) >= 2
    assert "jsonb_typeof(r.metadata->'target_pg_id')" not in src
    found = re.findall(r"~ '(\^\[0-9\]\+\$)'", src)
    assert len(found) >= 2
    rx = re.compile(found[0])
    mirror = coordinator_mod._target_pg_id_as_bigint
    digits_18 = "1" * 18
    samples = {
        "1.5": None,
        "1e20": None,
        "1E20": None,
        "1e+20": None,
        "12": 12,
        "0": 0,
        "": None,
        " 12": None,
        "+12": None,
        "12.0": None,
        digits_18: int(digits_18),
        "1" * 21: None,
    }
    for text, expected in samples.items():
        assert mirror(text) == expected, text
        sql_accepts = rx.fullmatch(text) is not None and len(text) <= 18
        assert sql_accepts == (expected is not None), text


def test_upsert_and_updates_keep_ack_shape():
    """Kills M7 (drop the keep-on-upsert) and M8/M8b (drop NOT superseded / COALESCE)."""
    save_src = inspect.getsource(MemoryCoordinator.handle_save)
    sup_src = inspect.getsource(MemoryCoordinator.handle_supersede)
    assert "metadata ? 'supersession_ack'" in save_src
    assert save_src.count("AND NOT superseded") >= 2
    assert sup_src.count("AND NOT superseded") >= 2
    needle = "COALESCE(metadata, '{}'::jsonb) || jsonb_build_object('supersession_ack'"
    assert needle in save_src
    assert needle in sup_src


def test_equal_lock_keys_refuse_boot():
    """An env value equal to the backup lock must not boot. Kills a missing startup check."""
    assert "require_distinct_advisory_lock_keys()" in inspect.getsource(MemoryCoordinator.start)
    old_s = coordinator_mod.SUPERSEDE_LOCK_KEY
    old_b = coordinator_mod.BACKUP_ADVISORY_LOCK_KEY
    try:
        coordinator_mod.SUPERSEDE_LOCK_KEY = 7
        coordinator_mod.BACKUP_ADVISORY_LOCK_KEY = 7
        with pytest.raises(RuntimeError):
            coordinator_mod.require_distinct_advisory_lock_keys()
    finally:
        coordinator_mod.SUPERSEDE_LOCK_KEY = old_s
        coordinator_mod.BACKUP_ADVISORY_LOCK_KEY = old_b
    coordinator_mod.require_distinct_advisory_lock_keys()


# ── M4 / M4b / alias ──────────────────────────────────────────────────────────

_BAD_ACKS = [
    {"2522": ""},
    {"2522": "   "},
    {"2522": "Operator acknowledged"},
    {"2522": "TODO"},
    {"2522": "<operator reason>"},
    {"2522": "n/a"},
    {},
    [2522, 2675],
    ["2522"],
    True,
]


@pytest.mark.asyncio
async def test_wordless_map_and_list_are_400_on_both_handlers():
    """Kills M4 and M4b. A wordless map or a list is 400, not a fabricated acknowledgement."""
    for raw in _BAD_ACKS:
        c, conn, _ = _coord()
        conn.fetchrow = AsyncMock(side_effect=_fetchrow_success)
        with patch.object(c, "_thread_grounds", new=AsyncMock(return_value=[_losing_decision()])):
            resp = await c.handle_supersede(_make_request({
                "pg_id": 2672,
                "acknowledge_standing": raw,
            }))
        assert resp.status == 400, raw
        assert "acknowledge_standing" in json.loads(resp.text)["message"]
        assert conn.transaction.call_count == 0

        c2, conn2, _ = _coord()
        conn2.fetchrow = AsyncMock(side_effect=_fetchrow_success)
        with patch.object(c2, "_thread_grounds", new=AsyncMock(return_value=[_losing_decision()])), \
             patch.object(c2, "_embed", new=AsyncMock(return_value=[0.1])), \
             patch.object(c2, "_commit_axis_registrations", new=AsyncMock()):
            resp2 = await c2.handle_save(_make_request({
                "content": "new correction fact",
                "metadata": {
                    "project": "shared-memory-GitHub",
                    "source": "claude",
                    "supersedes": 2672,
                    "acknowledge_standing": raw,
                },
            }))
        assert resp2.status == 400, raw
        assert "acknowledge_standing" in json.loads(resp2.text)["message"]
        assert conn2.transaction.call_count == 0


@pytest.mark.asyncio
async def test_body_acknowledge_alias_does_not_count():
    """The undocumented body key `acknowledge` is not an acknowledgement."""
    c, conn, _ = _coord()
    conn.fetchrow = AsyncMock(return_value={"superseded": False, "type": "fact"})
    with patch.object(c, "_thread_grounds", new=AsyncMock(return_value=[_losing_decision()])):
        resp = await c.handle_supersede(_make_request({
            "pg_id": 2672,
            "acknowledge": {"2522": "the operator says it still stands"},
        }))
    body = json.loads(resp.text)
    assert resp.status == 409
    assert body["error"] == "decision_loses_last_ground"
    assert conn.transaction.call_count == 0


# ── M5 / M14 ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_private_title_is_absent_from_the_payload():
    """Kills M14. A private decision the caller cannot read contributes no title or rationale."""
    c, conn, _ = _coord()
    conn.fetchrow = AsyncMock(return_value={"superseded": False, "type": "fact"})
    private = _losing_decision(2522, title="SECRET TITLE", visibility="private",
                               agent_id="owner", rationale="SECRET RATIONALE")
    public = _losing_decision(2675, title="Public title", rationale="Public rationale")
    with patch.object(c, "_thread_grounds", new=AsyncMock(return_value=[private, public])):
        resp = await c.handle_supersede(_make_request(
            {"pg_id": 2672, "agent_id": "owner"},
            authenticated_agent="attacker",
        ))
    body = json.loads(resp.text)
    assert resp.status == 409
    by_id = {d["pg_id"]: d for d in body["decisions"]}
    assert "title" not in by_id[2522]
    assert "rationale" not in by_id[2522]
    assert by_id[2675]["title"] == "Public title"
    assert "SECRET TITLE" not in resp.text
    assert "SECRET RATIONALE" not in resp.text


@pytest.mark.asyncio
async def test_ack_of_unreadable_decision_is_403():
    """Kills M5. Viewer is the authenticated agent. A missing row is unreadable.
    A body agent_id does not widen the read.
    """
    thread = [_losing_decision(2522, title="SECRET", visibility="private", agent_id="owner")]

    c, conn, _ = _coord()
    conn.fetchrow = AsyncMock(return_value={"superseded": False, "type": "fact"})
    with patch.object(c, "_thread_grounds", new=AsyncMock(return_value=thread)):
        resp = await c.handle_supersede(_make_request(
            {
                "pg_id": 2672,
                "agent_id": "owner",
                "acknowledge_standing": {"2522": "the operator says it still stands"},
            },
            authenticated_agent=None,
        ))
    assert resp.status == 403
    assert json.loads(resp.text)["error"] == "decision_not_visible"
    assert conn.transaction.call_count == 0

    c2, conn2, _ = _coord()
    conn2.fetchrow = AsyncMock(side_effect=[
        {"superseded": False, "type": "fact"},
        None,
    ])
    with patch.object(c2, "_thread_grounds", new=AsyncMock(return_value=[])):
        resp2 = await c2.handle_supersede(_make_request({
            "pg_id": 2672,
            "acknowledge_standing": {"9999": "the operator says it still stands"},
        }))
    assert resp2.status == 403
    assert json.loads(resp2.text)["error"] == "decision_not_visible"
    assert conn2.transaction.call_count == 0

    c3, conn3, _ = _coord()
    conn3.fetchrow = AsyncMock(side_effect=_fetchrow_success)
    with patch.object(c3, "_thread_grounds", new=AsyncMock(return_value=thread)):
        resp3 = await c3.handle_supersede(_make_request(
            {"pg_id": 2672, "acknowledge_standing": {"2522": "the operator says it still stands"}},
            authenticated_agent="owner",
        ))
    assert resp3.status == 200

    c4, conn4, _ = _coord()
    conn4.fetchrow = AsyncMock(return_value={"superseded": False, "type": "fact"})
    embed = AsyncMock(return_value=[0.1])
    with patch.object(c4, "_thread_grounds", new=AsyncMock(return_value=thread)), \
         patch.object(c4, "_embed", new=embed), \
         patch.object(c4, "_commit_axis_registrations", new=AsyncMock()):
        resp4 = await c4.handle_save(_make_request(
            {
                "content": "new correction fact",
                "agent_id": "owner",
                "metadata": {
                    "project": "shared-memory-GitHub",
                    "source": "claude",
                    "supersedes": 2672,
                    "acknowledge_standing": {"2522": "the operator says it still stands"},
                },
            },
            authenticated_agent="attacker",
        ))
    assert resp4.status == 403
    assert json.loads(resp4.text)["error"] == "decision_not_visible"
    embed.assert_not_called()


# ── M12 / M8 update-0 / M15 ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_locked_row_already_superseded_rolls_back():
    """Kills M12. FOR UPDATE seeing superseded raises, so the transaction rolls back."""
    c, conn, _ = _coord()
    tx = _recording_tx()
    conn.transaction = MagicMock(return_value=tx)
    conn.fetchrow = AsyncMock(side_effect=[
        {"superseded": False, "type": "fact"},
        {"superseded": True},
    ])
    with patch.object(c, "_thread_grounds", new=AsyncMock(return_value=[])):
        resp = await c.handle_supersede(_make_request({"pg_id": 2672}))
    assert resp.status == 409
    assert json.loads(resp.text)["error"] == "fact_already_superseded"
    assert tx.exc_type is coordinator_mod._FactAlreadySuperseded
    assert not any("UPDATE technical_docs" in str(call.args[0]) for call in conn.execute.call_args_list)


@pytest.mark.asyncio
async def test_update_zero_rolls_back_on_both_paths():
    """Kills M8/M8b/M12. UPDATE 0 raises inside the transaction instead of returning."""
    c, conn, _ = _coord()
    tx = _recording_tx()
    conn.transaction = MagicMock(return_value=tx)
    conn.fetchrow = AsyncMock(side_effect=[
        {"superseded": False, "type": "fact"},
        {"superseded": False},
    ])

    async def exec_zero(sql, *args):
        if "UPDATE technical_docs" in str(sql):
            return "UPDATE 0"
        return "UPDATE 1"

    conn.execute = AsyncMock(side_effect=exec_zero)
    with patch.object(c, "_thread_grounds", new=AsyncMock(return_value=[])):
        resp = await c.handle_supersede(_make_request({"pg_id": 2672}))
    assert resp.status == 409
    assert json.loads(resp.text)["error"] == "fact_already_superseded"
    assert tx.exc_type is coordinator_mod._FactAlreadySuperseded
    sqls = [str(call.args[0]) for call in conn.execute.call_args_list]
    assert any("UPDATE technical_docs" in s and "NOT superseded" in s for s in sqls)
    assert not any("neo4j_outbox" in s for s in sqls)

    c2, conn2, _ = _coord()
    tx2 = _recording_tx()
    conn2.transaction = MagicMock(return_value=tx2)
    conn2.fetchrow = AsyncMock(side_effect=_fetchrow_success)
    conn2.execute = AsyncMock(side_effect=exec_zero)
    with patch.object(c2, "_thread_grounds", new=AsyncMock(return_value=[_losing_decision()])), \
         patch.object(c2, "_embed", new=AsyncMock(return_value=[0.1])), \
         patch.object(c2, "_commit_axis_registrations", new=AsyncMock()):
        resp2 = await c2.handle_save(_make_request({
            "content": "new correction fact",
            "metadata": {
                "project": "shared-memory-GitHub",
                "source": "claude",
                "supersedes": 2672,
                "acknowledge_standing": {"2522": "still stands without the new fact"},
            },
        }))
    assert resp2.status == 409
    assert json.loads(resp2.text)["error"] == "fact_already_superseded"
    assert tx2.exc_type is coordinator_mod._FactAlreadySuperseded


@pytest.mark.asyncio
async def test_in_transaction_recheck_fires_when_precheck_was_clean():
    """Kills M15. Pre-check [] and the locked read [D] is 409 stage in-transaction, and nothing is written."""
    decision = _losing_decision()

    c, conn, _ = _coord()
    conn.fetchrow = AsyncMock(side_effect=[
        {"superseded": False, "type": "fact"},
        {"superseded": False},
    ])
    grounds = AsyncMock(side_effect=[[], [decision]])
    with patch.object(c, "_thread_grounds", new=grounds):
        resp = await c.handle_supersede(_make_request({"pg_id": 2672}))
    body = json.loads(resp.text)
    assert resp.status == 409
    assert body["error"] == "decision_loses_last_ground"
    assert body["stage"] == "in-transaction"
    assert grounds.await_count == 2
    assert not any("UPDATE technical_docs" in str(call.args[0]) for call in conn.execute.call_args_list)

    c2, conn2, _ = _coord()
    conn2.fetchrow = AsyncMock(side_effect=_fetchrow_success)
    grounds2 = AsyncMock(side_effect=[[], [decision]])
    with patch.object(c2, "_thread_grounds", new=grounds2), \
         patch.object(c2, "_embed", new=AsyncMock(return_value=[0.1])), \
         patch.object(c2, "_commit_axis_registrations", new=AsyncMock()):
        resp2 = await c2.handle_save(_make_request({
            "content": "new correction fact",
            "metadata": {
                "project": "shared-memory-GitHub",
                "source": "claude",
                "supersedes": 2672,
            },
        }))
    body2 = json.loads(resp2.text)
    assert resp2.status == 409
    assert body2["stage"] == "in-transaction"
    assert grounds2.await_count == 2
    assert not any(
        "INSERT INTO technical_docs" in str(call.args[0]) for call in conn2.fetchrow.call_args_list
    )


# ── M10a / M10b ───────────────────────────────────────────────────────────────

def test_other_409_still_raises_on_both_clients_and_mcp():
    """Kills M10a and M10b. Only decision_loses_last_ground is returned; axis_conflict still raises."""
    body_other = {"status": "error", "error": "axis_conflict", "message": "axes are fixed"}
    body_ground = {"status": "error", "error": "decision_loses_last_ground", "decisions": []}
    for i, path in enumerate(_bridge_paths()):
        mb = _load_memory_bridge(f"memory_bridge_m10_{i}", path)
        with pytest.raises(mb.GatewayReplyError):
            mb._reply_json(_http(409, body_other))
        kept = mb._reply_json(_http(409, body_ground))
        assert kept["error"] == "decision_loses_last_ground"
        with pytest.raises(ValueError):
            mb._normalize_cli_ack([["5"], ["7"]])
        assert "Operator acknowledged" not in inspect.getsource(mb._normalize_cli_ack)

    vs = _load_vector_skill("vector_skill_m10")
    with pytest.raises(vs.GatewayReplyError):
        vs._reply_json(_http(409, body_other), "supersede")
    kept_vs = vs._reply_json(_http(409, body_ground), "supersede")
    assert kept_vs["error"] == "decision_loses_last_ground"


@pytest.mark.asyncio
async def test_mcp_refuses_list_and_placeholder_before_post():
    """The MCP tool is a map only. A list, an empty string, or a placeholder never reaches the gateway."""
    vs = _load_vector_skill("vector_skill_mcp_ack")
    fn = vs.supersede
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    src = inspect.getsource(fn)
    assert "dict[str, str]" in src
    assert "list[int]" not in src
    doc = inspect.getdoc(fn) or ""
    assert "save the new fact" in doc
    assert "NEW_ID:based_on" in doc
    assert "decision_not_visible" in doc
    assert "not a role refusal" in doc
    assert "decision:2802" in doc and "fact:2809" in doc
    assert "decision:2751" not in doc

    class _NoPost:
        def __init__(self, *args, **kwargs):
            raise AssertionError("refused acknowledgement was posted")

    with patch.object(vs.httpx, "AsyncClient", _NoPost):
        for raw in ([5], {"5": ""}, {"5": "TODO"}, {"5": "Operator acknowledged"}, {}):
            result = await vs.supersede(2672, acknowledge_standing=raw)
            assert "400" in result
            assert "fact:2809" in result


@pytest.mark.asyncio
async def test_client_list_ack_does_not_post():
    """supersede_fact refuses a list locally. Kills M4b on both copies."""
    for i, path in enumerate(_bridge_paths()):
        mb = _load_memory_bridge(f"memory_bridge_list_{i}", path)

        def _boom(*args, **kwargs):
            raise AssertionError("list acknowledgement was posted")

        with patch.object(mb, "_async_client", _boom):
            res = await mb.supersede_fact(2672, acknowledge_standing=[5, 7])
        assert res["status"] == "error"
        assert "400" in res["message"]
        assert "fact:2809" in res["message"]


def test_ground_with_no_role_and_no_source_follows_standing_role():
    """A ground with no role and no source resolves like _standing_role, which is not based_on.
    Kills mutation: treat that gap as a standing based_on fact.
    """
    role = coordinator_mod._standing_role(None, None)
    rows = [{
        "id": 2522,
        "grounds": [
            {"id": 2672, "type": "fact", "superseded": False, "exists": True, "role": "based_on"},
            {"id": 2673, "type": "fact", "superseded": False, "exists": True},
        ],
    }]
    lost = threads_left_ungrounded(rows, 2672)
    if role == "based_on":
        assert lost == []
    else:
        assert [r["id"] for r in lost] == [2522]


_PUNCT_PLACEHOLDERS = (
    "Placeholder.",
    "TODO!",
    "operator acknowledged.",
    "  N/A.  ",
    "tbd...",
    "<operator reason>.",
)
_REAL_ACK = "The operator confirmed this decision still stands."


def test_placeholder_folds_case_punctuation_and_whitespace():
    """A stand-in is refused after case, trailing punctuation, and whitespace are folded.
    Kills mutation: exact-match the raw string, so 'Placeholder.' is stored.
    """
    assert coordinator_mod._is_placeholder_words(_REAL_ACK) is False
    for words in _PUNCT_PLACEHOLDERS:
        assert coordinator_mod._is_placeholder_words(words) is True, words
    for i, path in enumerate(_bridge_paths()):
        mb = _load_memory_bridge(f"memory_bridge_punct_{i}", path)
        assert mb._is_placeholder_words(_REAL_ACK) is False
        for words in _PUNCT_PLACEHOLDERS:
            with pytest.raises(ValueError):
                mb._normalize_cli_ack([["5", words]])


@pytest.mark.asyncio
async def test_placeholder_punctuation_is_400_on_the_gateway_and_mcp():
    """The gateway and the MCP tool refuse a punctuated stand-in before any write."""
    c, conn, _ = _coord()
    conn.fetchrow = AsyncMock(return_value={"superseded": False, "type": "fact"})
    with patch.object(c, "_thread_grounds", new=AsyncMock(return_value=[_losing_decision()])):
        resp = await c.handle_supersede(_make_request({
            "pg_id": 2672,
            "acknowledge_standing": {"2522": "Placeholder."},
        }))
    assert resp.status == 400
    assert conn.transaction.call_count == 0

    vs = _load_vector_skill("vector_skill_punct")

    class _NoPost:
        def __init__(self, *args, **kwargs):
            raise AssertionError("punctuated placeholder was posted")

    with patch.object(vs.httpx, "AsyncClient", _NoPost):
        result = await vs.supersede(2672, acknowledge_standing={"2522": "TODO!"})
    assert "400" in result
    assert vs._is_placeholder_words(_REAL_ACK) is False


def test_clients_surface_decision_not_visible_code_on_403():
    """A 403 branch names the error code, not only the sentence.
    Kills mutation: render message and drop error, so decision_not_visible looks like a role refusal.
    """
    body = {
        "status": "error",
        "error": "decision_not_visible",
        "message": "decision 2522 is not readable by the authenticated agent",
    }
    for i, path in enumerate(_bridge_paths()):
        mb = _load_memory_bridge(f"memory_bridge_403_{i}", path)
        with pytest.raises(mb.GatewayReplyError) as caught:
            mb._reply_json(_http(403, body))
        payload = caught.value.payload
        assert payload.get("error") == "decision_not_visible"
        assert "decision_not_visible" in payload["message"]
        assert "decision 2522 is not readable" in payload["message"]

    vs = _load_vector_skill("vector_skill_403_code")
    with pytest.raises(vs.GatewayReplyError) as caught_vs:
        vs._reply_json(_http(403, body), "supersede")
    assert "decision_not_visible" in caught_vs.value.message
    assert "decision 2522 is not readable" in caught_vs.value.message


@pytest.mark.asyncio
async def test_locked_row_already_superseded_on_save_rolls_back():
    """The save path raises when FOR UPDATE sees the target already superseded, so the insert does not commit.
    Kills mutation: drop the locked already-superseded check on handle_save.
    """
    c, conn, _ = _coord()
    tx = _recording_tx()
    conn.transaction = MagicMock(return_value=tx)

    async def fetchrow(sql, *args):
        text = str(sql)
        if "FOR UPDATE" in text:
            return {"superseded": True}
        if "INSERT INTO technical_docs" in text:
            return {"id": 999}
        return {"superseded": False, "type": "fact"}

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    with patch.object(c, "_thread_grounds", new=AsyncMock(return_value=[])), \
         patch.object(c, "_embed", new=AsyncMock(return_value=[0.1])), \
         patch.object(c, "_commit_axis_registrations", new=AsyncMock()):
        resp = await c.handle_save(_make_request({
            "content": "new correction fact",
            "metadata": {
                "project": "shared-memory-GitHub",
                "source": "claude",
                "supersedes": 2672,
            },
        }))
    body = json.loads(resp.text)
    assert resp.status == 409
    assert body["error"] == "fact_already_superseded"
    assert tx.exc_type is coordinator_mod._FactAlreadySuperseded
    assert not any(
        "INSERT INTO technical_docs" in str(call.args[0]) for call in conn.fetchrow.call_args_list
    )
