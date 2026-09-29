"""Tests for supersession last ground refusal and acknowledgement (PR 2b).

Rule: superseding a decision's last standing fact is refused (409 decision_loses_last_ground)
until the operator answers.
Records: decision:2802, fact:2800, decision:2801, decision:2774, decision:2751,
         operator rulings R1 + R2 (fact:2809, retrospective:2810).

Every test names the mutation it kills.
Unit tests never reach the network.
"""

import importlib.util
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
    assert body["acknowledge_standing"] == [2522]
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
        assert body_partial["acknowledge_standing"] == [2675]
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
        # $3 is json string of supersession_ack
        ack_payload = json.loads(update_args[3])
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
    vs renders the title and rationale; --acknowledge-standing 5 --acknowledge-standing 7 gives [5, 7].
    Kills mutation: fall back to _reply_json -> rationale gone -> dies.
    """
    # Test memory_bridge.py
    mb_path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts", "memory_bridge.py")
    )
    spec = importlib.util.spec_from_file_location("memory_bridge", mb_path)
    mb_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mb_mod)

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
        "acknowledge_standing": [2522],
    }
    mock_resp_409.json = MagicMock(return_value=payload_409)

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp_409)
    with patch.object(mb_mod, "_async_client", return_value=_async_ctx(mock_client)):
        res = await mb_mod.supersede_fact(2672)
        assert res["status"] == "error"
        assert res["error"] == "decision_loses_last_ground"
        assert res["decisions"][0]["rationale"] == "Untruncated critical rationale that must not be lost"

    # 2. Test CLI parsing of --acknowledge-standing
    sargs_5_7 = mb_mod._normalize_cli_ack([["5"], ["7"]])
    assert sargs_5_7 == [5, 7]

    sargs_words = mb_mod._normalize_cli_ack([["5", "stands firmly"], ["7", "verified again"]])
    assert sargs_words == {5: "stands firmly", 7: "verified again"}

    # 3. Test vector_skill render
    vs_path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "mcp", "vector-skill.py")
    )
    spec_vs = importlib.util.spec_from_file_location("vector_skill", vs_path)
    vs_mod = importlib.util.module_from_spec(spec_vs)
    spec_vs.loader.exec_module(vs_mod)

    rendered = vs_mod._render_decision_loses_last_ground(payload_409)
    assert "Ship skill index" in rendered
    assert "Untruncated critical rationale that must not be lost" in rendered
    assert "decision:2522" in rendered

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
