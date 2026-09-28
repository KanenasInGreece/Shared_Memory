import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
import consolidation_loop as cl
from consolidation_loop import render_thematic_fold


def test_thematic_fold_order_deterministic_and_ascending_by_pg_id():
    """decision:1242 (skip an unchanged fold) needs deterministic text: render_thematic_fold orders members by
    ascending pg_id so line order is deterministic regardless of Neo4j scan order.
    Asserts both equality between permutations and the ascending pg_id values."""
    record_map = {
        3: {"rtype": "fact", "kind": "observation", "recorded": "2026-09-01"},
        7: {"rtype": "fact", "kind": "tested", "recorded": "2026-09-02"},
        12: {"rtype": "fact", "kind": "measured", "recorded": "2026-09-03"},
    }

    # Order 1: [12, 3, 7]
    contents_1 = ["Content twelve", "Content three", "Content seven"]
    ids_1 = [12, 3, 7]

    # Order 2: [7, 12, 3]
    contents_2 = ["Content seven", "Content twelve", "Content three"]
    ids_2 = [7, 12, 3]

    summary_1, sorted_ids_1 = render_thematic_fold(contents_1, ids_1, record_map)
    summary_2, sorted_ids_2 = render_thematic_fold(contents_2, ids_2, record_map)

    # 1. Permutation equality: identical output across differing input orders
    assert summary_1 == summary_2
    assert sorted_ids_1 == sorted_ids_2 == [3, 7, 12]

    # 2. Value assertion (fact:1309): verify lines are strictly ascending by pg_id
    lines = summary_1.splitlines()
    assert len(lines) == 3
    assert "pg_id=3]" in lines[0] and "Content three" in lines[0]
    assert "pg_id=7]" in lines[1] and "Content seven" in lines[1]
    assert "pg_id=12]" in lines[2] and "Content twelve" in lines[2]
