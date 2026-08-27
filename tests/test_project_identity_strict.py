"""Item 6 of the v0.9.69 post-first-write hardening plan — a `:Project` node is
never minted from an ERROR.

Invariant **P3**: a `:Project` node is created only from a REGISTRY IDENTITY.

WHAT WAS WRONG (`fact:1734` C(f)). `_project_identity` returned None from any
cause — an unregistered name or a failed lookup alike — and every caller then
fell back to `project_merge_cypher(None)`, which keys the node on the NAME. The
fallback was a deliberate design rule while an unregistered project name could
still reach a save ("the WRITE must never be lost"). Under the ingress gate it
cannot: every project a save accepts is registered, so a missing identity is a
data-integrity defect or an unreadable registry — and in both cases the
fallback mints a SECOND node for a project that already has one, which is the
exact divergence migration 027 exists to remove.

RULED R3 (strict): raise on a lookup error AND on a missing row for a non-blank
name. Then each surface answers for itself —

  outbox  → the row retries, then goes `failed`, where the failure is VISIBLE
  ingress → 503 `registry_unavailable` (the hard embedding mandate's answer:
            half a save is not a save)
  reader  → degrades to "no identity" and REPORTS the degrade, never a 500

`project_axis.project_merge_cypher`'s docstring carried the superseded rule and
has been rewritten rather than edited around.

Mutation checks (RUN, recorded in HANDOFF.md) — see each test.
"""
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

import coordinator as coordinator_mod  # noqa: E402
from coordinator import (  # noqa: E402
    MemoryCoordinator, ProjectIdentityUnavailable,
)


class _AsyncCtx:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


def _coord(fetchval):
    c = MemoryCoordinator()
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=fetchval)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="")
    conn.fetchrow = AsyncMock(return_value={"id": 99})
    conn.transaction = MagicMock(return_value=_AsyncCtx(None))
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    c._pool = pool
    session = AsyncMock()
    session.run = AsyncMock()
    neo4j = MagicMock()
    neo4j.session = MagicMock(return_value=_AsyncCtx(session))
    c._neo4j = neo4j
    return c, conn


# ── The method itself ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_project_identity_error_raises_not_name_fallback():
    """MUTATION CHECK: restore `except Exception: return None` and this test
    fails — the caller would go on to key the node on the name."""
    async def boom(sql, *args):
        raise RuntimeError("registry unreadable")

    c, _ = _coord(boom)
    with pytest.raises(ProjectIdentityUnavailable):
        await c._project_identity("shared-memory-GitHub")


@pytest.mark.asyncio
async def test_a_missing_registry_row_raises_too():
    """The half Gemini's review added and the ruling adopted: under the ingress
    gate a missing row is not an unknown name, it is a defect.

    MUTATION CHECK: delete the `if project_id is None: raise` block and this
    test fails."""
    c, _ = _coord(lambda sql, *a: None)
    with pytest.raises(ProjectIdentityUnavailable):
        await c._project_identity("shared-memory-GitHub")


@pytest.mark.asyncio
async def test_a_blank_name_still_answers_none():
    """The ONE surviving None: the parked-record sentinel path, where
    `project_for_graph` hands in nothing at all. It must not raise — there is
    no identity to fail to find."""
    c, _ = _coord(lambda sql, *a: None)
    assert await c._project_identity(None) is None
    assert await c._project_identity("") is None
    assert await c._project_identity("   ") is None


@pytest.mark.asyncio
async def test_a_registered_name_still_returns_its_id():
    c, _ = _coord(lambda sql, *a: 42)
    assert await c._project_identity("shared-memory-GitHub") == 42


# ── Ingress: 503, never a silent half-filed record ────────────────────────────

@pytest.mark.asyncio
async def test_ingress_turns_an_unreadable_registry_into_503():
    """The domain axis resolves through the project's identity, so an
    unreadable registry means this record cannot be FILED.

    MUTATION CHECK: remove the `except ProjectIdentityUnavailable` handler from
    handle_save and this test fails with a 500 (an unhandled exception) instead
    of the 503 — which is the difference between "retry, my database is down"
    and "the gateway is broken"."""
    calls = {"n": 0}

    async def fetchval(sql, *args):
        if "SELECT id FROM projects" in sql:
            raise RuntimeError("registry unreadable")
        if "content_hash" in sql:
            return None
        calls["n"] += 1
        return 1                      # the project IS registered

    c, _ = _coord(fetchval)
    req = MagicMock()
    req.json = AsyncMock(return_value={
        "content": "a fact in a section of a project",
        "agent_id": "claude-code",
        "metadata": {"source": "claude-code", "project": "alpha",
                     "domains": ["architecture"]},
    })
    req.get = MagicMock(return_value=None)
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)) as embed:
        resp = await c.handle_save(req)
    assert resp.status == 503
    assert json.loads(resp.text)["error"] == "registry_unavailable"
    embed.assert_not_called()


@pytest.mark.asyncio
async def test_a_record_naming_no_domain_never_asks_for_an_identity():
    """Most records name no section, and the identity is only needed to resolve
    one — so an unreadable registry must not take those saves down with it."""
    async def fetchval(sql, *args):
        if "SELECT id FROM projects" in sql:
            raise RuntimeError("registry unreadable")
        if "content_hash" in sql:
            return None
        return 1

    c, _ = _coord(fetchval)
    req = MagicMock()
    req.json = AsyncMock(return_value={
        "content": "a fact filed under its project and nothing narrower",
        "agent_id": "claude-code",
        "metadata": {"source": "claude-code", "project": "alpha"},
    })
    req.get = MagicMock(return_value=None)
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        resp = await c.handle_save(req)
    assert resp.status == 200


# ── Outbox: the row retries, and then fails visibly ───────────────────────────

@pytest.mark.asyncio
async def test_an_outbox_row_retries_when_the_identity_is_unavailable():
    """P3's teeth: the write is NOT completed with a name-keyed node. The row
    goes back to `pending` with a backoff, and after OUTBOX_MAX_RETRIES it is
    `failed` — a state a human can see, which the silent fallback never was."""
    async def fetchval(sql, *args):
        if "SELECT id FROM projects" in sql:
            raise RuntimeError("registry unreadable")
        return 1

    c, conn = _coord(fetchval)
    await c._apply_outbox_row(7, 42, {"project": "alpha", "entities": []}, 0)
    statements = [k.args[0] for k in conn.execute.call_args_list]
    assert any("status='pending'" in s and "retries=retries+1" in s
               for s in statements), statements
    assert not any("status='applied'" in s for s in statements)


@pytest.mark.asyncio
async def test_the_last_attempt_marks_the_row_failed():
    async def fetchval(sql, *args):
        if "SELECT id FROM projects" in sql:
            raise RuntimeError("registry unreadable")
        return 1

    c, conn = _coord(fetchval)
    await c._apply_outbox_row(
        7, 42, {"project": "alpha", "entities": []},
        coordinator_mod.OUTBOX_MAX_RETRIES - 1)
    statements = [k.args[0] for k in conn.execute.call_args_list]
    assert any("status='failed'" in s for s in statements), statements


# ── Readers: degrade, and SAY SO ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_search_filter_degrades_and_reports_instead_of_500ing():
    """Group 3's question — can this be seen FAILING? Without an identity the
    domain half of the filter resolves to nothing, which is indistinguishable
    from "nobody registered that section". So the degrade is counted and named.

    MUTATION CHECK: remove the `except ProjectIdentityUnavailable` around the
    search-side `_project_identity` call and this test fails with the raised
    exception instead of a reported degrade."""
    async def fetchval(sql, *args):
        if "SELECT id FROM projects" in sql:
            raise RuntimeError("registry unreadable")
        return 1

    c, conn = _coord(fetchval)
    # The project resolves — it IS registered — so the filter reaches the
    # identity lookup, which is the call under test.
    conn.fetch = AsyncMock(return_value=[{"name": "alpha", "alias": "alpha",
                                          "canonical": "alpha"}])
    before = c._axis_registry_read_failures
    _project_values, _domain_values, resolved = await c._resolve_search_filters(
        "alpha", ["architecture"])
    assert resolved.get("error"), \
        "a degrade that changes the answer must be visible"
    assert c._axis_registry_read_failures == before + 1
