"""
Retrieval must honour record LIFECYCLE — supersession on every path, and
lifecycle edges visible in graph context.

Two defects this pins, both measured live before the fix:

  * Three retrieval paths dropped the `NOT superseded` guard — the keyword
    fallback (used whenever embedding is unavailable), the Tier-1 vector
    fallback and the Tier-3 thematic fallback. Proven: the keyword query
    returned a superseded fact beside the fact that superseded it, and the
    unguarded Tier-3 query returned a superseded summary. 39 superseded records
    were reachable this way.

  * graph_context ranked edges by `asserted_by` FIRST, and inheritance stamps
    MENTIONS with asserted_by='inherited'. A decision with 31 such edges buried
    its own HAD_OUTCOME below the cap, so a reader could not see it had been
    judged, and a retrospective did not surface the decision it judges.

⚠ These assert the QUERY TEXT. All SQL/Cypher here is stubbed, so a green suite
proves nothing about execution — every one of these is additionally verified
against the running system before release (see the release fact).
"""
import importlib.util, os, sys
import pytest


def _coordinator():
    d = os.path.normpath(os.path.join(os.path.dirname(__file__), "..",
                                      "shared-memory", "scripts"))
    if d not in sys.path:
        sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location("coordinator",
                                                  os.path.join(d, "coordinator.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["coordinator"] = mod
    spec.loader.exec_module(mod)
    return mod


coordinator_mod = _coordinator()
SRC = open(os.path.join(os.path.dirname(__file__), "..", "shared-memory",
                        "scripts", "coordinator.py")).read()


# ── supersession holds on EVERY retrieval path, not just the happy one ──────

def test_keyword_fallback_filters_superseded():
    """The keyword/ILIKE fallback answers whenever embedding is unavailable —
    it is reachable in normal operation, not an edge case. It must not serve
    records that were retired for being wrong."""
    i = SRC.index("SELECT id, content, metadata FROM technical_docs")
    frag = SRC[i:i + 320]
    assert "ILIKE" in frag, "located the wrong query"
    assert "NOT superseded" in frag, (
        "the keyword fallback must exclude superseded records"
    )


def test_tier1_vector_fallback_filters_superseded():
    """The pre-migration fallback may drop `created_at`; it may never drop the
    supersession guard. A search that returns nothing is visibly broken, one
    that serves retired records is invisibly wrong."""
    i = SRC.index("except asyncpg.UndefinedColumnError:")
    frag = SRC[i:i + 900]
    assert "NOT superseded" in frag


def test_tier1_fallback_catches_only_a_missing_column():
    """A bare `except Exception` caught every error and dropped the guard with
    it, so ANY transient fault served superseded rows. The fallback exists for
    one schema shape and must catch only that."""
    assert "except asyncpg.UndefinedColumnError:" in SRC
    i = SRC.index("ORDER BY embedding <=> $1::vector LIMIT $2")
    assert "except Exception:" not in SRC[i:i + 400], (
        "the Tier-1 vector fallback must not catch arbitrary exceptions"
    )


def test_tier3_thematic_fallback_filters_superseded():
    """Supersession is how a Tier-3 narrative is retired — the mechanism for
    summary lifecycle is scheduled, not built, so the filter is all there is."""
    n = SRC.count("FROM community_summaries")
    assert n >= 3
    for idx, _ in enumerate(range(n)):
        pass
    # every community_summaries read on the search path carries the guard
    start = SRC.index("vis_t3, vis_t3_params = _visibility_filter")
    end = SRC.index("# Tier 1 — vector search", start)
    block = SRC[start:end]
    assert block.count("FROM community_summaries") == 3
    assert block.count("NOT superseded") == 3, (
        "all three Tier-3 reads (insight, thematic, thematic-fallback) must "
        f"filter supersession; found {block.count('NOT superseded')}"
    )


# ── lifecycle edges must survive the graph-context cap ─────────────────────

def test_graph_context_ranks_lifecycle_edges_by_type():
    """Ranked by TYPE, never by the provenance stamp: inheritance writes
    MENTIONS with asserted_by set, so a stamp-first sort promotes topical edges
    above the verdict edge that says whether a decision still stands.

    BOTH expansion queries must carry it — the single-anchor one and the
    batched one. (An earlier version of this test matched the batched query
    twice, because its marker is a substring of the indented form, and so
    silently pinned nothing in the single-anchor query.)"""
    marker = "ORDER BY CASE WHEN type(r) IN ["
    assert SRC.count(marker) == 2, (
        f"both expansion queries must rank by type; found {SRC.count(marker)}"
    )
    pos = -1
    for _ in range(2):
        pos = SRC.index(marker, pos + 1)
        frag = SRC[pos:pos + 320]
        for rel in ("had_outcome", "supersedes", "grounded_in", "informed_by"):
            assert f"ONT.{rel}" in frag, f"{rel} missing from a ranking clause"
        assert frag.index("type(r) IN [") < frag.index("r.asserted_by IS NOT NULL"), (
            "edge type must be the FIRST sort key in every expansion query"
        )


def test_asserted_by_still_breaks_ties_below_type():
    """The stamp is not discarded — it remains the second key, so a stamped
    edge still outranks an unstamped one WITHIN the same tier."""
    pos = SRC.index("ORDER BY CASE WHEN type(r) IN [")
    frag = SRC[pos:pos + 320]
    assert "r.asserted_by IS NOT NULL THEN 1 ELSE 2 END" in frag
