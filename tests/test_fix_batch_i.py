"""Section suggestions also match words in descriptions (decision:3261: strict word
similarity at 0.40)."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

from domain_axis import DOMAIN_PROPOSALS_SQL  # noqa: E402
from coordinator import MemoryCoordinator  # noqa: E402


def _acquire_stub(fetch):
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=fetch)
    conn.fetchval = AsyncMock(return_value=None)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=acq), conn


def test_domain_proposals_sql_pinned():
    """The SQL pins the strict word similarity arm, ranking, and parameter bindings."""
    assert DOMAIN_PROPOSALS_SQL.count("strict_word_similarity($2, coalesce(description, ''))") == 2
    assert "strict_word_similarity($2, coalesce(description, '')) >= $4" in DOMAIN_PROPOSALS_SQL
    assert "similarity(coalesce(description" not in DOMAIN_PROPOSALS_SQL
    assert DOMAIN_PROPOSALS_SQL.count("similarity(name, $2)") == 2
    assert "similarity(name, $2) >= $3" in DOMAIN_PROPOSALS_SQL
    assert "LIMIT $5" in DOMAIN_PROPOSALS_SQL
    assert "ORDER BY score DESC, name_hit DESC, name" in DOMAIN_PROPOSALS_SQL
    assert "bool_or(from_name)" in DOMAIN_PROPOSALS_SQL


@pytest.mark.asyncio
async def test_domain_proposals_fetch_arguments():
    """Calling the real _domain_proposals passes the expected literal parameter values."""
    calls = []

    def fetch(sql, *args):
        calls.append((sql, args))
        return [{"name": "security"}]

    c = MemoryCoordinator()
    c._acquire, _ = _acquire_stub(fetch)

    project_id = 42
    name = "crypto"
    proposals = await c._domain_proposals(project_id, name)

    assert proposals == ["security"]
    assert len(calls) == 1
    sql, args = calls[0]
    assert sql == DOMAIN_PROPOSALS_SQL
    assert args == (project_id, name, 0.25, 0.40, 5)
