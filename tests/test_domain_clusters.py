"""Unit tests for domain-scoped NREM consolidation partitioning (migration 007).

Covers the pure partition rule that splits an entity's facts by domain and
re-gates density per (entity, domain) — no DB or Neo4j required.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
from consolidation_loop import eligible_domain_clusters, DEFAULT_DOMAIN


def test_single_domain_meets_threshold():
    contents = ["a", "b", "c"]
    pg_ids = [1, 2, 3]
    domain_map = {1: "homelab", 2: "homelab", 3: "homelab"}
    result = eligible_domain_clusters(contents, pg_ids, domain_map, threshold=3)
    assert result == [("homelab", ["a", "b", "c"], [1, 2, 3])]


def test_mixed_domains_only_qualifying_domain_returned():
    # 3 homelab facts (meets threshold) + 2 framework facts (below threshold)
    contents = ["h1", "h2", "f1", "h3", "f2"]
    pg_ids = [1, 2, 3, 4, 5]
    domain_map = {1: "homelab", 2: "homelab", 3: "framework", 4: "homelab", 5: "framework"}
    result = dict((dom, p) for dom, _c, p in
                  eligible_domain_clusters(contents, pg_ids, domain_map, threshold=3))
    assert "homelab" in result
    assert result["homelab"] == [1, 2, 4]
    assert "framework" not in result  # only 2 facts, below threshold


def test_facts_spread_thinly_yield_no_clusters():
    # Entity has 4 facts total (would pass an entity-level gate of 3) but they
    # are split 2/2 across domains — neither domain meets threshold 3.
    contents = ["a", "b", "c", "d"]
    pg_ids = [1, 2, 3, 4]
    domain_map = {1: "x", 2: "x", 3: "y", 4: "y"}
    assert eligible_domain_clusters(contents, pg_ids, domain_map, threshold=3) == []


def test_untagged_facts_collapse_to_default_domain():
    contents = ["a", "b"]
    pg_ids = [1, 2]
    domain_map = {}  # nothing tagged
    result = eligible_domain_clusters(contents, pg_ids, domain_map, threshold=2)
    assert result == [(DEFAULT_DOMAIN, ["a", "b"], [1, 2])]


def test_empty_domain_value_falls_back_to_default():
    contents = ["a", "b"]
    pg_ids = [1, 2]
    domain_map = {1: "", 2: None}  # falsy domains
    result = eligible_domain_clusters(contents, pg_ids, domain_map, threshold=2)
    assert result == [(DEFAULT_DOMAIN, ["a", "b"], [1, 2])]


def test_content_pgid_alignment_preserved_per_domain():
    contents = ["alpha", "beta", "gamma", "delta"]
    pg_ids = [10, 20, 30, 40]
    domain_map = {10: "d1", 20: "d2", 30: "d1", 40: "d2"}
    result = dict((dom, list(zip(p, c)))
                  for dom, c, p in
                  eligible_domain_clusters(contents, pg_ids, domain_map, threshold=2))
    assert result["d1"] == [(10, "alpha"), (30, "gamma")]
    assert result["d2"] == [(20, "beta"), (40, "delta")]


# ── _count_domain_cycles — telemetry NREM cycle gauge (coordinator) ───────────
# Mirrors eligible_domain_clusters' gating but returns a count, used by
# GET /memory/telemetry so a read-only client needs no DB join of its own.
from coordinator import _count_domain_cycles


def test_count_cycles_single_domain_meets_threshold():
    assert _count_domain_cycles([1, 2, 3], {1: "x", 2: "x", 3: "x"}, threshold=3) == 1


def test_count_cycles_below_threshold_is_zero():
    assert _count_domain_cycles([1, 2], {1: "x", 2: "x"}, threshold=3) == 0


def test_count_cycles_counts_each_qualifying_domain():
    # x has 3 (qualifies), y has 3 (qualifies), z has 2 (does not) → 2 cycles
    pg_ids = [1, 2, 3, 4, 5, 6, 7, 8]
    domain_map = {1: "x", 2: "x", 3: "x", 4: "y", 5: "y", 6: "y", 7: "z", 8: "z"}
    assert _count_domain_cycles(pg_ids, domain_map, threshold=3) == 2


def test_count_cycles_thinly_spread_yields_zero():
    # 4 ids split 2/2 — neither domain meets threshold 3
    assert _count_domain_cycles([1, 2, 3, 4], {1: "x", 2: "x", 3: "y", 4: "y"}, threshold=3) == 0


def test_count_cycles_untagged_collapse_to_default():
    # No domain tags → all collapse to one bucket
    assert _count_domain_cycles([1, 2], {}, threshold=2) == 1


def test_count_cycles_empty_domain_falls_back_to_default():
    assert _count_domain_cycles([1, 2], {1: "", 2: None}, threshold=2) == 1
