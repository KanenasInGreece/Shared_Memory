"""The workspace, not the stored records, is the authority on which projects exist.

Restored from `tests/test_entity_vote.py` (deleted in the v0.9.73 docs residue sweep,
`entity_vote.py` itself being a test-only orphan with no runtime importer) — these
`classify`/`normalise_key`/`collisions` cases are the only behavioural coverage
`shared-memory/scripts/sync_project_registry.py` had. Moved into their own file rather
than dropped along with the orphan module they used to share a file with. Folder names
genericised to placeholders (fact:1195 — a fixture states the FORM, never the private
instance); the placeholders below preserve the exact match/variant/close/absent shape
the originals exercised.

A project name is the folder name; seeding a registry from records instead registers
the misspellings alongside the projects. No DB or Neo4j required.
"""
import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

from sync_project_registry import classify, normalise_key, collisions


FOLDERS = {"shared-memory-GitHub", "shared-memory-monitor", "project-consult", "proj-d"}


def test_a_folder_match_needs_no_adjudication():
    assert classify("shared-memory-GitHub", FOLDERS)[0] == "exact"


def test_a_sister_project_is_matched_on_its_own_name():
    """shared-memory-monitor is a sister project of the framework and must stay
    distinct — it has its own folder and must never resolve to the framework."""
    verdict, folder, _ = classify("shared-memory-monitor", FOLDERS)
    assert verdict == "exact"
    assert folder == "shared-memory-monitor"


def test_case_differences_resolve_to_the_folder_spelling():
    """A directory listing is the canonical casing; treating a case difference
    as a separate project splits a project against itself."""
    verdict, folder, _ = classify("Shared-Memory-GitHub", FOLDERS)
    assert verdict == "variant"
    assert folder == "shared-memory-GitHub"


def test_separator_style_is_never_a_real_difference():
    """`shared_memory_monitor` and `shared-memory-monitor` are one project
    written by two tools. Before the normalised key these fell through to fuzzy
    matching, where a near-miss is only ever REPORTED — so a pure spelling
    variant would have been queued for human adjudication forever."""
    for spelling in ("shared_memory_monitor", "shared.memory.monitor",
                     "shared memory monitor", "  Shared-Memory-Monitor  "):
        verdict, folder, _ = classify(spelling, FOLDERS)
        assert verdict == "variant", spelling
        assert folder == "shared-memory-monitor"


def test_the_key_folds_separators_and_case_but_never_words():
    assert normalise_key("Shared_Memory Monitor") == normalise_key("shared-memory-monitor")
    assert normalise_key("  a__b  ") == "a-b"
    # WORDS are meaning, not spelling: folding these would merge a sister project
    # into the one beside it, which is the loss this axis exists to prevent.
    assert normalise_key("proj-d") != normalise_key("proj-d-other")
    assert normalise_key("shared-memory") != normalise_key("shared-memory-monitor")
    assert normalise_key("Shared_Memory") != normalise_key("shared-memory-GitHub")


def test_two_folders_that_normalise_alike_are_reported_never_merged():
    """Two real directories competing for one registry row is a problem only the
    operator can settle."""
    assert collisions({"my-proj", "my_proj"}) == {"my-proj": ["my-proj", "my_proj"]}
    assert collisions(FOLDERS) == {}


def test_a_near_miss_is_surfaced_never_auto_resolved():
    """A near-miss spelling could be a rename, a typo, or two real projects.
    The tool reports; the operator decides."""
    verdict, folder, ratio = classify("project-consultant", FOLDERS)
    assert verdict == "close"
    assert folder == "project-consult"
    assert ratio < 1.0


def test_a_name_with_no_folder_is_absent_not_dead():
    """Work assisted by agents whose folders live on another machine has no local
    directory. Treating that as 'not a project' would quietly disown it."""
    verdict, folder, _ = classify("proj-d-telemetry", FOLDERS)
    assert verdict == "absent"
    assert folder is None
