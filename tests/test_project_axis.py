"""Invariant P1 — one project resolution, used by every reader.

Before this, the same COALESCE existed in eight places across five files: three
already canonical, two falling back to `domain`, one also to `scope`. The same
record answered "which project?" differently depending on who asked, and 219 of
261 decisions read as untagged while carrying a project all along.

Also covers the replacement of the `decision_cycles` gauge: it partitioned every
eligible Decision node by Postgres project — no shared grounded entity, no
≥2-distinct-projects rule, no HAD_OUTCOME — and so reported a backlog the daemon
could not fold. It now runs the daemon's own predicate, count-only.

No DB or Neo4j required.
"""
import os
import re
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

from project_axis import PROJECT_SQL, PROJECT_MATCH_SQL, SENTINEL, resolve_project


# ── The resolution itself ────────────────────────────────────────────────────

def test_resolution_prefers_the_decision_blob():
    """A judgement carries its project inside metadata.decision; a fact carries
    it at the top level. The decision branch must come FIRST and must exist —
    dropping it is what made 219 decisions read as untagged."""
    assert "metadata->'decision'->>'project'" in PROJECT_SQL
    assert PROJECT_SQL.index("decision") < PROJECT_SQL.index("metadata->>'project'")
    assert resolve_project({"decision": {"project": "smg"}, "project": "other"}) == "smg"
    assert resolve_project({"project": "smg"}) == "smg"


def test_neither_scope_nor_domain_is_in_the_chain():
    """A domain is a SECTION of a project and cannot stand in for the whole;
    `scope` is access control and is never topical."""
    assert "domain" not in PROJECT_SQL
    assert "scope" not in PROJECT_SQL
    assert resolve_project({"domain": "crypto"}) is None
    assert resolve_project({"scope": "team-a"}) is None
    assert resolve_project({"domain": "crypto", "scope": "team-a"}) is None


def test_unresolvable_is_none_not_a_bucket():
    """None, so a caller can tell "no project" from any particular name. PR 2
    turns that into fold-ineligibility rather than a shared bucket."""
    assert resolve_project({}) is None
    assert resolve_project(None) is None
    assert resolve_project("not-a-dict") is None
    assert resolve_project({"project": ""}) is None
    assert resolve_project({"project": "   "}) is None
    assert resolve_project({"project": 42}) is None
    assert resolve_project({"decision": "not-a-dict", "project": "smg"}) == "smg"


def test_sentinel_is_reserved_and_distinct():
    assert SENTINEL == "general_discussion"
    assert resolve_project({"project": SENTINEL}) == SENTINEL


# ── P1 has teeth only if no reader keeps a private copy ──────────────────────

# A hand-rolled resolution is a COALESCE that CHAINS metadata paths — that is
# what "resolving" means here, and every historical violation had that shape:
# falling back from 'project' to 'domain' or 'scope', or re-spelling
# PROJECT_SQL's own decision-blob chain.
#
# ⚠ It must NOT fire on `COALESCE(metadata->>'project', '')`, which is a
# different operation: keying a community_summaries ROW on its own stored axis,
# where the empty-string default is load-bearing for the unique index (a
# domain-level summary has no entity, and NULL in a unique index makes
# duplicates legal — migration 029). Requiring TWO metadata paths is what
# separates "resolve a record's project" from "key a summary on its own".
_OWN_COPY = re.compile(
    r"COALESCE\(\s*metadata->[^)]*'project'[^)]*metadata->[^)]*\)"
    r"|COALESCE\(\s*metadata->(?![^)]*'project'[^)]*'')[^)]*metadata->[^)]*'project'",
    re.IGNORECASE,
)

# Python implicit string concatenation splits a query across source lines, so
# collapse `" ... "  f" ... "` before looking for the pattern.
_JOIN = re.compile(r'"\s*f?"')


def _scripts():
    for name in sorted(os.listdir(_SCRIPTS)):
        if name.endswith(".py") and name != "project_axis.py":
            yield name, open(os.path.join(_SCRIPTS, name), encoding="utf-8").read()


def test_no_reader_carries_its_own_project_resolution():
    """The regression guard for P1: a ninth copy appearing anywhere fails here.

    project_axis.py is the sole exemption — it is where the resolution lives.
    """
    offenders = [
        name for name, src in _scripts()
        if _OWN_COPY.search(_JOIN.sub("", src))
    ]
    assert offenders == [], (
        "these modules hand-roll a project resolution instead of importing "
        f"project_axis.PROJECT_SQL: {offenders}"
    )


def test_the_p1_guard_discriminates_resolution_from_a_summary_key():
    """The guard above is only worth having if it still catches what it was
    written for. PR 7 narrowed it — a summary's own axis key
    (`COALESCE(metadata->>'project','')`, load-bearing for migration 029's
    unique index) is not a resolution — so the narrowing is pinned here rather
    than left to be re-derived. Every form below is a violation this project
    actually had (see project_axis.py's docstring: two readers fell back to
    `domain`, one to `scope`, and five re-spelled PROJECT_SQL)."""
    must_fire = [
        "COALESCE(metadata->>'project', metadata->>'domain')",
        "COALESCE(metadata->>'project', metadata->>'scope')",
        "COALESCE(metadata->'decision'->>'project', metadata->>'project')",
        "COALESCE(metadata->>'domain', metadata->>'project')",
    ]
    must_not_fire = [
        "COALESCE(metadata->>'project', '')",
        "COALESCE(metadata->>'entity', '')",
        "COALESCE(metadata->>'domain', '')",
    ]
    for src in must_fire:
        assert _OWN_COPY.search(src), f"P1 guard no longer catches: {src}"
    for src in must_not_fire:
        assert not _OWN_COPY.search(src), f"P1 guard false-positives on: {src}"


def test_every_reader_actually_imports_the_module():
    """The mirror of the above: absence of a copy could also mean the reader
    stopped resolving a project at all."""
    expected = {
        "coordinator.py", "consolidation_loop.py", "rem_loop.py",
        "migrate_retro_edges.py", "normalize_projects.py",
    }
    importers = {name for name, src in _scripts() if "from project_axis import" in src}
    assert expected <= importers, f"no longer importing the axis: {expected - importers}"


def test_the_migration_tool_matches_both_fields_not_the_resolution():
    """normalize_projects rewrites legacy spellings, so it must find a row whose
    OLD name is shadowed by a newer top-level one. COALESCE would skip it."""
    predicate = PROJECT_MATCH_SQL.format(p="%s")
    assert "metadata->>'project' = %s" in predicate
    assert "metadata->'decision'->>'project' = %s" in predicate
    assert "COALESCE" not in predicate
    assert " OR " in predicate


# ── decision_cycles now counts v2 GATING GROUPS (Dreaming Cycle Plan to v2,
#    §2.2; C2) — insight_cluster_cypher and its ≥2-projects rule are gone
#    (see test_project_identity.py's retired-P22 note). `_nrem_cycle_counts`
#    now walks each fact-gating (project, domain) group via
#    `insight_gate.walk_group_reached_set` and checks
#    `insight_gate.passes_insight_gate` (G2 + G3) — the SAME predicate
#    `consolidation_loop._find_fresh_insight_clusters` folds on. ─────────────

_FACT_ROWS_MARKER = "RETURN f.pg_id AS pg_id, project, domain"
_THREE_FACT_ROWS = [
    {"pg_id": 101, "project": "proj", "domain": "dom"},
    {"pg_id": 102, "project": "proj", "domain": "dom"},
    {"pg_id": 103, "project": "proj", "domain": "dom"},
]


@pytest.mark.asyncio
async def test_decision_cycles_reflects_the_walk_not_a_partition():
    """A gating (project, domain) group whose walk reaches NO judgement at all
    (empty neighbour response) must count as 0 — G2/G3 vacuously fail. The old
    chain collected decisions flat and bucketed by Postgres project, so two
    decisions in one project counted as a cycle even with nothing connecting
    them; this proves the gauge asks the graph, not Postgres."""
    from coordinator import MemoryCoordinator

    coord = MemoryCoordinator()
    captured = []

    async def fake_run(query, **params):
        captured.append((query, params))
        result = MagicMock()
        if _FACT_ROWS_MARKER in query:
            result.data = AsyncMock(return_value=_THREE_FACT_ROWS)
        else:
            result.data = AsyncMock(return_value=[])  # walk step — nothing reached
        return result

    session = MagicMock()
    session.run = fake_run
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    coord._neo4j = MagicMock()
    coord._neo4j.session = MagicMock(return_value=session)

    counts = await coord._nrem_cycle_counts()

    assert counts["decision_cycles"] == 0
    # It must have ASKED the graph the walk question (I3's one-hop query) —
    # a partition of Postgres project values would never issue this.
    assert any("UNWIND $ids AS pid" in q for q, _ in captured)


@pytest.mark.asyncio
async def test_decision_cycles_counts_a_group_whose_walk_reaches_a_retrospective():
    """The counterpart: the gating group's walk reaches one fresh Decision and
    one fresh Retrospective (G2 + G3 both satisfied) — one gating group, one
    cycle."""
    from coordinator import MemoryCoordinator

    coord = MemoryCoordinator()

    async def fake_run(query, **params):
        result = MagicMock()
        if _FACT_ROWS_MARKER in query:
            result.data = AsyncMock(return_value=_THREE_FACT_ROWS)
        else:
            ids = set(params.get("ids") or [])
            if ids == {101, 102, 103}:
                result.data = AsyncMock(return_value=[
                    {"src": 101, "dst": 201, "dst_label": "Decision",
                     "dst_consolidated": False},
                    {"src": 102, "dst": 202, "dst_label": "Retrospective",
                     "dst_consolidated": False},
                ])
            else:
                result.data = AsyncMock(return_value=[])  # fixpoint — nothing new
        return result

    session = MagicMock()
    session.run = fake_run
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    coord._neo4j = MagicMock()
    coord._neo4j.session = MagicMock(return_value=session)

    counts = await coord._nrem_cycle_counts()
    assert counts["decision_cycles"] == 1
    assert counts["total_cycles"] == counts["fact_cycles"] + 1


def test_decision_threshold_no_longer_a_duplicate_tunable():
    """v2: G2/G3 are 'at least one' conditions, not a tunable decision COUNT —
    the pre-v2 hardcoded twin of insight_threshold that used to sit beside it
    in coordinator.py is gone outright, not renamed."""
    import coordinator

    assert not hasattr(coordinator, "INSIGHT_THRESHOLD")
    assert not hasattr(coordinator, "NREM_DECISION_THRESHOLD")


# ── P2/P3 — an unresolvable project folds nothing (v0.8.32) ──────────────────

def test_fold_eligible_rejects_every_shape_of_absence():
    from project_axis import fold_eligible

    assert fold_eligible("shared-memory-GitHub") is True
    assert fold_eligible(None) is False
    assert fold_eligible("") is False
    assert fold_eligible("   ") is False
    assert fold_eligible(0) is False
    assert fold_eligible(["smg"]) is False


def test_both_partitioners_use_the_same_eligibility_predicate():
    """The gauge and the fold agreeing is not a coincidence to be maintained by
    hand — they call one function."""
    import consolidation_loop
    import coordinator

    assert consolidation_loop.fold_eligible is coordinator.fold_eligible


def test_the_fold_key_query_no_longer_invents_a_bucket():
    """The SQL must let NULL through so the partitioner can exclude it. A
    COALESCE here would rebuild the `general` bucket underneath the guard,
    and every unit test above would still pass."""
    src = open(os.path.join(_SCRIPTS, "consolidation_loop.py"), encoding="utf-8").read()
    assert "f\"SELECT id, {PROJECT_SQL},\"" in src
    assert "COALESCE({PROJECT_SQL}, 'general')" not in src

    # ⚠ The alias is `AS project`, never `AS domain` — the naming trap PR 7
    # exists to untangle, and a test asserting the old spelling would pin the
    # confusion in place. v2 (C1b): coordinator._nrem_cycle_counts no longer
    # resolves project via Postgres PROJECT_SQL at all — it reads
    # `proj.name AS project` straight off the graph's PROJECT_OF edge (the
    # SAME chain the fold walks), so the guard moves to the Cypher form.
    coord = open(os.path.join(_SCRIPTS, "coordinator.py"), encoding="utf-8").read()
    assert "proj.name AS project" in coord
    assert "proj.name AS domain" not in coord
    assert "dom.name AS project" not in coord
    assert "{PROJECT_SQL} AS domain" not in coord
    assert "DEFAULT_DOMAIN" not in coord.split("# NREM dream-cycle backlog gauge")[0]


def test_project_node_is_minted_from_the_resolved_project():
    """P3 — never from a section, never from a chain."""
    coord = open(os.path.join(_SCRIPTS, "coordinator.py"), encoding="utf-8").read()
    assert '"project": project_for_graph(metadata),' in coord
    assert '"project": metadata.get("project"),' not in coord
    assert '"project": resolve_project(metadata),' not in coord


# ── The PROJECT_OF backfill row (v0.8.32) ────────────────────────────────────

class _Recorder:
    """Captures the Cypher a handler runs, plus the SQL it issues."""

    def __init__(self):
        self.cypher = []
        self.sql = []

    def coordinator(self, project_id=None):
        """``project_id`` is what the registry lookup returns (migration 027).

        Default None — the pre-027 shape, and also what a deployment whose
        registry does not hold the name gets. Pass an int to exercise the
        identified path.
        """
        from coordinator import MemoryCoordinator
        c = MemoryCoordinator()

        async def run(query, **params):
            self.cypher.append((query, params))
            r = MagicMock()
            r.data = AsyncMock(return_value=[])
            return r

        session = MagicMock()
        session.run = run
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        c._neo4j = MagicMock()
        c._neo4j.session = MagicMock(return_value=session)

        conn = MagicMock()
        conn.execute = AsyncMock(side_effect=lambda *a: self.sql.append(a))
        conn.fetchval = AsyncMock(return_value=project_id)
        acq = MagicMock()
        acq.__aenter__ = AsyncMock(return_value=conn)
        acq.__aexit__ = AsyncMock(return_value=False)
        c._acquire = MagicMock(return_value=acq)
        return c


@pytest.mark.asyncio
async def test_backfill_row_writes_only_the_project_edge():
    """THE safety property. Re-enqueuing an ordinary fact row would re-run its
    `UNWIND $entities MERGE MENTIONS`, resurrecting every enrichment edge a
    later sweep deliberately deleted. The repair row must touch nothing else."""
    rec = _Recorder()
    coord = rec.coordinator()
    await coord._apply_project_of_outbox_row(7, 42, {"type": "project_of",
                                                     "project": "smg"})
    assert len(rec.cypher) == 1
    query, params = rec.cypher[0]
    assert "PROJECT_OF" in query
    assert "MENTIONS" not in query
    assert "$entities" not in query
    assert "f.content" not in query
    assert params == {"pg_id": 42, "project": "smg", "project_id": None}


@pytest.mark.asyncio
async def test_backfill_matches_the_record_and_never_mints_one():
    """A backfill mints no records. MERGE on the target would conjure a phantom
    node whose only property is a pg_id — the exact defect the supersede
    handler documents.

    Asserted as MATCH-not-MERGE on the target rather than as an exact opening
    string: the row now matches the whole SPINE (a promotion can target a
    judgement, and matching :Fact alone would silently drop those rows) and
    deletes the stale edge first (P19). Pinning the literal prefix made this
    test fail for a change that strengthened the very property it protects.
    """
    rec = _Recorder()
    coord = rec.coordinator()
    await coord._apply_project_of_outbox_row(7, 42, {"type": "project_of",
                                                     "project": "smg"})
    query = rec.cypher[0][0]
    assert query.startswith("MATCH ("), "the target is matched, never merged"
    # The ONLY node this row may create is the :Project it points at.
    merged_labels = re.findall(r"MERGE \(\w+:(\w+)", query)
    assert merged_labels == ["Project"], merged_labels


@pytest.mark.asyncio
async def test_backfill_row_is_deleted_not_left_as_backlog():
    """One-shot, like the supersede row: it carries no dream lifecycle, so
    leaving it applied would inflate the outbox working set forever."""
    rec = _Recorder()
    coord = rec.coordinator()
    await coord._apply_project_of_outbox_row(7, 42, {"type": "project_of",
                                                     "project": "smg"})
    assert rec.sql == [("DELETE FROM neo4j_outbox WHERE id=$1", 7)]


@pytest.mark.asyncio
async def test_backfill_row_with_no_project_writes_nothing():
    """P3 — never mint a :Project from an absent value."""
    rec = _Recorder()
    coord = rec.coordinator()
    await coord._apply_project_of_outbox_row(7, 42, {"type": "project_of",
                                                     "project": "  "})
    assert rec.cypher == []
    assert rec.sql == [("DELETE FROM neo4j_outbox WHERE id=$1", 7)]


def test_backfill_never_writes_neo4j_directly():
    """The script's whole contract: it enqueues, the worker applies."""
    src = open(os.path.join(_SCRIPTS, "backfill_project_of.py"), encoding="utf-8").read()
    assert "INSERT INTO neo4j_outbox" in src
    assert "'pending'" in src          # the status the worker actually polls
    assert "MERGE" not in src          # no Cypher writes of its own


def test_backfill_refuses_against_a_gateway_that_cannot_handle_the_row():
    """The ordering hazard, made unarmable — and asserted on BEHAVIOUR, not on
    source text: a guard whose condition is disabled leaves its own message in
    the file, so a text assertion passes while the guard is dead.

    A worker predating this row type treats it as an ordinary fact row and runs
    `SET f.content = $content` with content the row does not carry, blanking the
    content of every fact it touches. Silent graph-side data loss.
    """
    import importlib
    bf = importlib.import_module("backfill_project_of")

    assert bf.gateway_handles_project_of((0, 8, 32)) is True
    assert bf.gateway_handles_project_of((0, 9, 0)) is True
    assert bf.gateway_handles_project_of((1, 0, 0)) is True
    assert bf.gateway_handles_project_of((0, 8, 31)) is False
    assert bf.gateway_handles_project_of((0, 7, 9)) is False


def test_backfill_fails_closed_when_the_version_is_unknowable():
    """'Cannot tell' is not 'safe to write'."""
    import importlib
    bf = importlib.import_module("backfill_project_of")

    assert bf.gateway_handles_project_of(None) is False

    old = bf.GATEWAY_URL
    try:
        bf.GATEWAY_URL = "http://127.0.0.1:1"   # nothing listens here
        raw, parsed = bf.gateway_version()
        assert parsed is None
        assert bf.gateway_handles_project_of(parsed) is False
    finally:
        bf.GATEWAY_URL = old


def test_backfill_consults_the_guard_before_inserting():
    """Placement matters as much as the predicate: the check must gate the
    INSERT, not merely exist somewhere in the file."""
    src = open(os.path.join(_SCRIPTS, "backfill_project_of.py"), encoding="utf-8").read()
    apply_path = src.split("Dry run — nothing enqueued")[1]
    assert apply_path.index("gateway_handles_project_of") < apply_path.index(
        "INSERT INTO neo4j_outbox"
    )
