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
    # No confusable neighbours unless a test says otherwise — the ordinary case
    # for a genuinely new name, and the one the guard must stay quiet for.
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    c._acquire = MagicMock(return_value=acq)
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
async def test_a_retrospective_is_out_of_scope_here():
    """It arrives on its own endpoint and inherits the project of the decision
    it judges — a decision that passed this check itself. Re-checking here would
    demand a value the caller never supplies and reject every retrospective."""
    coord = _coord()
    assert await coord._project_ingress_error({"type": "retrospective"}, "c") is None


@pytest.mark.asyncio
async def test_a_decision_naming_an_unregistered_project_is_rejected():
    """v0.8.44. Decisions were excluded from this check, and the reasoning that
    excluded them mistook PRESENCE for VALIDITY: a decision does fail without
    decision.project, but a present name no registry knew was accepted, and the
    outbox then minted a project node for it. That is the one way the graph can
    hold a project the registry does not — and unlike the ingress→outbox window
    (which leaves the graph BEHIND the registry, always safe) it never resolves
    itself."""
    coord = _coord(proposals=["shared-memory-GitHub"])
    err = await coord._project_ingress_error(
        {"type": "decision", "decision": {"project": "shared-memry-GitHub"}}, "c")
    assert err is not None
    assert err["error"] == "project_unknown"
    assert err["proposals"] == ["shared-memory-GitHub"]


@pytest.mark.asyncio
async def test_a_decision_on_a_registered_project_passes():
    coord = _coord()
    assert await coord._project_ingress_error(
        {"type": "decision", "decision": {"project": "shared-memory-GitHub"}}, "c") is None


@pytest.mark.asyncio
async def test_a_decision_may_declare_a_new_project_with_the_operator_s_confirmation():
    """The flow this exists for: a discussion produces an idea, the idea is
    saved as a fact, and a decision grounded on that fact commits to acting on
    it — and until that moment the project does not exist. So a decision must be
    able to introduce one. What it must NOT be able to do is introduce one
    silently, which is why the declaration is an explicit flag and not a
    fallback."""
    coord = _coord(registered=())
    assert await coord._project_ingress_error(
        {"type": "decision", "new_project": True,
         "decision": {"project": "brand-new"}}, "c") is None
    coord._register_project.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_decision_declared_once_needs_no_flag_on_the_records_that_follow():
    """It is declared ONCE, on the first record that names it. Everything saved
    afterwards in the same flow finds it registered — which is what makes the
    confirmation a single deliberate act rather than a prompt on every save."""
    registry = set()
    coord = _coord(registered=registry)
    coord._project_registered = AsyncMock(side_effect=lambda n: n in registry)
    coord._register_project = AsyncMock(side_effect=lambda n, a: registry.add(n))

    first = await coord._project_ingress_error(
        {"source": "c", "project": "brand-new", "new_project": True}, "c")
    second = await coord._project_ingress_error(
        {"type": "decision", "decision": {"project": "brand-new"}}, "c")
    assert first is None and second is None
    coord._register_project.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_decisions_retired_spelling_is_rewritten_in_the_decision_blob():
    """The alias rewrite has always known where a decision keeps its project;
    only the early return kept decisions from reaching it."""
    coord = _coord(registered=("shared-memory-GitHub",))
    coord._resolve_project_alias = AsyncMock(return_value="shared-memory-GitHub")
    metadata = {"type": "decision", "decision": {"project": "shared_memory"}}
    assert await coord._project_ingress_error(metadata, "c") is None
    assert metadata["decision"]["project"] == "shared-memory-GitHub"


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


# ── P23 — a declaration is not a defence ────────────────────────────────────
#
# The agent that sets new_project is the agent that makes the spelling error, so
# the flag on its own guards nothing. Floor and populations measured on a live
# registry: the closest legitimately DISTINCT pair of 37 registered projects
# scored 0.500 and no pair reached 0.6, while typos of a registered name scored
# 0.78–1.00 and separator/case variants scored exactly 1.00.

def _coord_near(near, registered=()):
    """A coordinator whose confusable lookup answers from a fixed list."""
    coord = _coord(registered=registered)
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{"name": n} for n in near])
    conn.fetchval = AsyncMock(return_value=None)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    coord._acquire = MagicMock(return_value=acq)
    return coord


@pytest.mark.asyncio
async def test_a_separator_variant_can_never_be_declared_new():
    """`alpha_service` is not a new project beside `alpha-service`; it is how
    that project is spelled today by a machine that has the old folder name.
    Unconfirmable on purpose — a rename is a deliberate operation with a ledger,
    never a side effect of a save."""
    coord = _coord_near(["alpha-service"])
    err = await coord._project_ingress_error(
        {"source": "c", "project": "Alpha_Service", "new_project": True}, "c")
    assert err["error"] == "project_spelling_variant"
    assert "alpha-service" in err["message"]
    coord._register_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirming_a_spelling_variant_does_not_help():
    """The override exists for names that are genuinely different strings. A
    variant is the SAME name, and no confirmation makes it another project."""
    coord = _coord_near(["alpha-service"])
    err = await coord._project_ingress_error(
        {"source": "c", "project": "alpha_service", "new_project": True,
         "confirm_distinct_from": ["alpha-service"]}, "c")
    assert err["error"] == "project_spelling_variant"
    coord._register_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_confusable_name_is_refused_until_the_neighbour_is_named():
    coord = _coord_near(["alpha-service"])
    err = await coord._project_ingress_error(
        {"source": "c", "project": "alpha-servize", "new_project": True}, "c")
    assert err["error"] == "project_confusable"
    assert "alpha-service" in err["message"]
    assert err["proposals"] == ["alpha-service"]
    coord._register_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_naming_the_neighbour_lets_a_genuinely_separate_project_through():
    """The check must not block real work: a spin-off with a similar name is a
    real project, and the operator is the one who knows."""
    coord = _coord_near(["alpha-service"])
    err = await coord._project_ingress_error(
        {"source": "c", "project": "alpha-servize", "new_project": True,
         "confirm_distinct_from": ["Alpha-Service"]}, "c")   # key-compared
    assert err is None
    coord._register_project.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirming_one_neighbour_does_not_confirm_another():
    """Each near match is its own claim. Confirming the one you noticed must not
    wave through the one you did not."""
    coord = _coord_near(["alpha-service", "alpha-tool"])
    err = await coord._project_ingress_error(
        {"source": "c", "project": "alpha-servize", "new_project": True,
         "confirm_distinct_from": ["alpha-service"]}, "c")
    assert err["error"] == "project_confusable"
    assert "alpha-tool" in err["message"]
    assert "alpha-service" not in err["message"]


@pytest.mark.asyncio
async def test_an_unmistakable_new_project_registers_with_no_extra_step():
    """The guard must stay quiet for the ordinary case, or it trains the reflex
    to override it."""
    coord = _coord_near([])
    assert await coord._project_ingress_error(
        {"source": "c", "project": "unrelated-thing", "new_project": True}, "c") is None
    coord._register_project.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_decision_faces_the_same_two_refusals():
    """Same rule on both record types — a decision is the likelier place for
    this to happen, because it is where 'let us act on this' is recorded."""
    coord = _coord_near(["alpha-service"])
    err = await coord._project_ingress_error(
        {"type": "decision", "new_project": True,
         "decision": {"project": "alpha_service"}}, "c")
    assert err["error"] == "project_spelling_variant"
    # Refused means refused: nothing reaches the registry on this path.
    coord._register_project.assert_not_awaited()


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


# ── The spelling gate must not depend on the trigram gate (v0.8.48) ──────────

def test_spelling_variant_is_found_without_any_similarity_filtering():
    """The shared helper, directly. A SPELLING is exact equality on a normalised
    key; it must never be gated behind a fuzzy score. Measured live: `testing`
    vs `Test_Ing` scores 0.545 against a floor of 0.6, so the variant never
    reached this check and registered as new."""
    from project_axis import spelling_variant_of
    assert spelling_variant_of("Test_Ing", ["testing"]) == "testing"
    assert spelling_variant_of("Alpha-Service", ["alpha_service"]) == "alpha_service"
    assert spelling_variant_of("genuinely-new", ["testing", "alpha_service"]) is None


@pytest.mark.asyncio
async def test_a_project_spelling_variant_below_the_floor_is_still_refused():
    """The project half of the same defect. `near` is EMPTY on purpose — that is
    what a below-floor confusable query returns — so this dies if the spelling
    check is ever fed the trigram neighbours again."""
    c = _coord(registered=("alpha_service",))
    conn = c._acquire.return_value.__aenter__.return_value
    conn.fetch = AsyncMock(side_effect=[[], [{"name": "alpha_service"}]])
    err = await c._project_ingress_error(
        {"project": "Alpha-Service", "new_project": True}, "claude")
    assert err["error"] == "project_spelling_variant"
    c._register_project.assert_not_awaited()
