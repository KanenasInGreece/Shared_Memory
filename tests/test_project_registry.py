"""PR 3 — the projects registry, the 400, proposals, and the sentinel.

Invariants P4 (project required, unconditional), P5 (the sentinel is
fold-excluded but fully searchable and enriched), P8 (the sentinel mints no
:Project node), P9 (the second submission is accepted, in three forms).

No DB required — the registry lookup is stubbed; migration 022 and the trigram
proposals are verified against the live database separately, because the suite
stubs all SQL and proves nothing about it.
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

from project_axis import (
    SENTINEL, PROJECT_EXISTS_SQL, PROJECT_PROPOSALS_SQL,
    fold_eligible, project_for_graph, resolve_project,
)


def _coord(registered=("shared-memory-GitHub",), proposals=()):
    """A coordinator whose registry answers from a fixed set."""
    from coordinator import MemoryCoordinator
    c = MemoryCoordinator()
    c._project_registered = AsyncMock(side_effect=lambda n: n in registered)
    c._project_proposals = AsyncMock(return_value=list(proposals))
    c._register_project = AsyncMock()
    return c


# ── P4 — required, unconditional, and never satisfied by a chain ─────────────

@pytest.mark.asyncio
async def test_a_fact_with_no_project_is_rejected():
    err = await _coord()._project_ingress_error({"source": "claude"}, "claude")
    assert err is not None
    assert err["error"] == "project_required"
    assert err["status"] == "error"


@pytest.mark.asyncio
async def test_a_domain_only_fact_is_not_accepted_as_tagged():
    """A domain is a SECTION of a project. Accepting it here would let a part
    vouch for the whole — the exact fallback this work removed."""
    err = await _coord()._project_ingress_error(
        {"source": "claude", "domain": "operations"}, "claude")
    assert err is not None and err["error"] == "project_required"


@pytest.mark.asyncio
async def test_a_scope_only_fact_is_not_accepted_as_tagged():
    err = await _coord()._project_ingress_error(
        {"source": "claude", "scope": "team-a"}, "claude")
    assert err is not None and err["error"] == "project_required"


@pytest.mark.asyncio
async def test_a_registered_project_passes():
    assert await _coord()._project_ingress_error(
        {"source": "claude", "project": "shared-memory-GitHub"}, "claude") is None


@pytest.mark.asyncio
async def test_an_unregistered_project_is_rejected_with_proposals():
    coord = _coord(proposals=["shared-memory-GitHub", "shared-memory-monitor"])
    err = await coord._project_ingress_error(
        {"source": "claude", "project": "shared memory"}, "claude")
    assert err["error"] == "project_unknown"
    assert err["proposals"] == ["shared-memory-GitHub", "shared-memory-monitor"]


@pytest.mark.asyncio
async def test_judgements_are_out_of_scope_here():
    """Decisions already fail without decision.project further down handle_save;
    retrospectives use their own endpoint and inherit the target's project.
    Checking them again here would reject every retrospective."""
    coord = _coord()
    assert await coord._project_ingress_error({"type": "decision"}, "c") is None
    assert await coord._project_ingress_error({"type": "retrospective"}, "c") is None


# ── P9 — the second submission is accepted, in three forms ──────────────────

@pytest.mark.asyncio
async def test_second_submission_form_1_a_proposal():
    coord = _coord(registered=("shared-memory-GitHub",))
    assert await coord._project_ingress_error(
        {"source": "c", "project": "shared-memory-GitHub"}, "c") is None


@pytest.mark.asyncio
async def test_second_submission_form_2_declares_a_new_project():
    coord = _coord(registered=())
    assert await coord._project_ingress_error(
        {"source": "c", "project": "brand-new", "new_project": True}, "c") is None
    coord._register_project.assert_awaited_once()
    assert coord._register_project.await_args[0][0] == "brand-new"


@pytest.mark.asyncio
async def test_second_submission_form_3_the_sentinel():
    coord = _coord(registered=())
    assert await coord._project_ingress_error(
        {"source": "c", "project": SENTINEL}, "c") is None
    coord._register_project.assert_not_awaited()   # P8 — never registered


@pytest.mark.asyncio
async def test_the_same_unregistered_name_is_refused_however_often_it_is_sent():
    """There is no round counter, and that is the point: the bound comes from the
    three accepting forms, not from per-caller state. Re-sending a bare
    unregistered name must never soften into acceptance on some later try."""
    coord = _coord(registered=())
    body = {"source": "c", "project": "still-not-registered"}
    for _ in range(4):
        err = await coord._project_ingress_error(body, "c")
        assert err is not None and err["error"] == "project_unknown"
    coord._register_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_project_must_be_the_boolean_true_not_merely_truthy():
    """A JSON client sending the string "false" would otherwise register it."""
    coord = _coord(registered=())
    for value in ("true", 1, "yes", [1]):
        err = await coord._project_ingress_error(
            {"source": "c", "project": "sneaky", "new_project": value}, "c")
        assert err is not None, f"new_project={value!r} was accepted"


# ── P5 / P8 — the sentinel saves and searches, but is neither subject nor node ──

def test_the_sentinel_is_not_a_fold_key():
    """P5. It is a real value, not an absence — but folding on it would rebuild
    the shared bucket under a new name."""
    assert fold_eligible(SENTINEL) is False
    assert fold_eligible("shared-memory-GitHub") is True


def test_the_sentinel_still_resolves_as_a_project_value():
    """P5's other half: it must reach Postgres and be searchable. If resolution
    dropped it, the record would be rejected at ingress instead of parked."""
    assert resolve_project({"project": SENTINEL}) == SENTINEL


def test_the_sentinel_mints_no_project_node():
    """P8 — a sentinel inside the project set would be counted by the insight
    gate's '>= 2 distinct projects' rule and fold as though it were a subject."""
    assert project_for_graph({"project": SENTINEL}) is None
    assert project_for_graph({"project": "shared-memory-GitHub"}) == "shared-memory-GitHub"
    assert project_for_graph({"decision": {"project": SENTINEL}}) is None


def test_the_registry_statements_have_the_shape_the_gateway_binds():
    """asyncpg positional parameters — a mismatch here fails only at runtime,
    because the suite stubs all SQL."""
    assert "$1" in PROJECT_EXISTS_SQL
    assert PROJECT_PROPOSALS_SQL.count("$1") == 2      # filter + ORDER BY
    assert "$2" in PROJECT_PROPOSALS_SQL               # similarity floor
    assert "$3" in PROJECT_PROPOSALS_SQL               # limit
    assert "similarity(" in PROJECT_PROPOSALS_SQL


@pytest.mark.asyncio
async def test_the_rejection_tells_the_model_to_ask_not_to_infer():
    """Capture-surface contract: an agent that guesses produces a record filed
    under a plausible wrong name, which is worse than one that is parked."""
    coord = _coord()
    required = await coord._project_ingress_error({"source": "c"}, "c")
    unknown = await coord._project_ingress_error(
        {"source": "c", "project": "nope"}, "c")
    for body in (required, unknown):
        assert "ASK THE OPERATOR" in body["message"]
        assert SENTINEL in body["message"]


# ── Group 1 — the client surface ships as TWO tracked trees ─────────────────

def test_every_manifest_file_is_byte_identical_across_both_tracked_copies():
    """Generalises the hand-listed parity checks, which covered SKILL.md and
    memory_bridge.py and therefore could not see schema.md drift — it had fallen
    several releases behind in the SHIPPED copy while the source moved on.

    Reading MANIFEST.txt rather than a literal list is the point: the manifest is
    already the definition of what ships, so a file added there is covered here
    automatically, and this test cannot fall behind the way a hardcoded list did.
    """
    root = os.path.join(os.path.dirname(__file__), "..")
    skill_root = os.path.join(root, "shared-memory-skill", "shared-memory")
    source_root = os.path.join(root, "shared-memory")

    manifest = os.path.join(skill_root, "MANIFEST.txt")
    entries = [
        line.strip() for line in open(manifest, encoding="utf-8")
        if line.strip() and not line.strip().startswith("#")
    ]
    assert entries, "MANIFEST.txt parsed to nothing — the check would pass vacuously"

    # .env.example is NOT a copy — it is a DIFFERENT file that shares a name. The
    # client env may hold only this agent's AGENT_TOKEN; the server env holds
    # PG_PASSWORD, NEO4J_PASSWORD and every agent's token. vector-skill.py refuses
    # to load one that looks like the other, so requiring them to match would
    # demand exactly the mistake that guard exists to prevent.
    NOT_A_COPY = {".env.example"}

    drifted, missing_source = [], []
    for rel in entries:
        if rel in NOT_A_COPY:
            continue
        shipped = os.path.join(skill_root, rel)
        source = os.path.join(source_root, rel)
        if not os.path.exists(source):
            # Some shipped files (update_skill.sh, .env.example) live only in the
            # skill tree — no source twin to compare against.
            continue
        if not os.path.exists(shipped):
            missing_source.append(rel)
            continue
        with open(source, "rb") as a, open(shipped, "rb") as b:
            if a.read() != b.read():
                drifted.append(rel)

    assert not missing_source, f"listed in MANIFEST but absent from the skill tree: {missing_source}"
    assert not drifted, (
        f"these shipped files have diverged from their source: {drifted}. "
        "Clients are receiving the stale copy. Run: bash shared-memory/scripts/sync_skills.sh"
    )


def test_a_reasoning_trace_carries_a_project_like_any_other_record():
    """It is deliberately NOT exempt: a trace belongs to the work that produced
    it, and exempting it would quietly rebuild the untagged population. It is
    also not DEFAULTED to the sentinel — that would park records without anyone
    deciding to."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "vector-skill.py"),
               encoding="utf-8").read()
    body = src.split("async def archive_reasoning_trace")[1].split("\n@mcp.tool()")[0]
    signature = body.split(")")[0]
    assert "project: str" in signature, "the tool must take a project"
    assert f'project: str = "{SENTINEL}"' not in signature, (
        "the sentinel must not be the DEFAULT — that parks records without "
        "anyone deciding to. Naming it in the docstring as a deliberate choice "
        "is the opposite, and is correct.")
    assert 'metadata["project"] = project' in body
