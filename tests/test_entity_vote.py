"""The entity vote's policy, and the workspace as the authority on projects.

Two ideas are under test, and both exist because inferring the project axis from
the CORPUS was the original mistake:

* The vote proposes a parked record's project from the projects of the records
  sharing its entities. Validated by holding out 390 facts with known projects:
  96.9% at >=70% agreement with >=3 support, against a 66.2% base rate for
  always guessing the largest project. Real signal — but every error it made was
  a SISTER PROJECT absorbed into the larger one, one of them at 95% agreement
  across 20 supporting facts. Raising the bar does not fix that; only unanimity
  measured clean (100% on 57 holdouts), so unanimity is the only band written
  without an operator.

* The workspace directories, not the stored records, decide which projects
  EXIST. A project name is the folder name; seeding a registry from records
  instead registers the misspellings alongside the projects.

No DB or Neo4j required.
"""
import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

from entity_vote import (
    tally, vote_band, is_auto,
    BAND_AUTO, BAND_HIGH, BAND_REVIEW, BAND_NONE,
)
from sync_project_registry import classify, normalise_key, collisions


# ── Tallying ─────────────────────────────────────────────────────────────────

def test_tally_reports_winner_share_and_support():
    project, share, support = tally([{"proj": "smg", "n": 9}, {"proj": "mon", "n": 1}])
    assert project == "smg"
    assert share == 0.9
    assert support == 10


def test_a_tie_has_no_winner():
    """A tie IS an unresolved vote. Letting row order break it would make the
    outcome depend on something nobody chose."""
    project, _share, _support = tally([{"proj": "a", "n": 5}, {"proj": "b", "n": 5}])
    assert project is None


def test_no_neighbours_is_not_a_vote():
    assert tally([]) == (None, 0.0, 0)
    assert tally(None) == (None, 0.0, 0)


# ── The bands — the whole policy, in one predicate ───────────────────────────

def test_only_unanimity_is_written_without_an_operator():
    """The measured safety property. Every error the vote made was below 100%,
    and one of them was at 95% with 20 supporting facts — so 'high confidence'
    is NOT a substitute for unanimity."""
    assert vote_band("smg", 1.0, 10) == BAND_AUTO
    assert is_auto(vote_band("smg", 1.0, 10)) is True
    assert is_auto(vote_band("smg", 0.95, 20)) is False
    assert is_auto(vote_band("smg", 0.99, 99)) is False


def test_the_95_percent_error_would_not_be_auto_written():
    """Regression on a real misclassification: a shared-memory-monitor fact read
    as shared-memory-GitHub at 95% agreement across 20 supporting facts. A sister
    project must stay distinct, so this case has to reach a human."""
    band = vote_band("shared-memory-GitHub", 0.95, 20)
    assert band == BAND_HIGH
    assert not is_auto(band)


def test_the_close_review_band_is_surfaced_separately():
    assert vote_band("smg", 0.72, 18) == BAND_REVIEW
    assert vote_band("smg", 0.70, 5) == BAND_REVIEW
    assert vote_band("smg", 0.89, 5) == BAND_REVIEW


def test_below_the_floor_nothing_is_proposed():
    """Accuracy falls off a cliff below 70% — 92% at 60%, 85% at 50% — so those
    records go to the sentinel rather than to a person's judgement queue."""
    assert vote_band("smg", 0.69, 50) == BAND_NONE
    assert vote_band("smg", 0.50, 50) == BAND_NONE


def test_thin_support_is_not_unanimity():
    """A 100% share drawn from one neighbouring fact is a coincidence of sharing
    a single entity, not agreement."""
    assert vote_band("smg", 1.0, 1) == BAND_NONE
    assert vote_band("smg", 1.0, 2) == BAND_NONE
    assert vote_band("smg", 1.0, 3) == BAND_AUTO


def test_a_tie_never_reaches_a_band():
    assert vote_band(None, 0.5, 100) == BAND_NONE


# ── The workspace is the authority on which projects exist ───────────────────

FOLDERS = {"shared-memory-GitHub", "shared-memory-monitor", "cloe-consult", "tier3"}


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
    assert normalise_key("tier3") != normalise_key("tier3-cloe")
    assert normalise_key("shared-memory") != normalise_key("shared-memory-monitor")
    assert normalise_key("Shared_Memory") != normalise_key("shared-memory-GitHub")


def test_two_folders_that_normalise_alike_are_reported_never_merged():
    """Two real directories competing for one registry row is a problem only the
    operator can settle."""
    assert collisions({"my-proj", "my_proj"}) == {"my-proj": ["my-proj", "my_proj"]}
    assert collisions(FOLDERS) == {}


def test_a_near_miss_is_surfaced_never_auto_resolved():
    """'cloe-consultant' vs the folder 'cloe-consult' could be a rename, a typo,
    or two real projects. The tool reports; the operator decides."""
    verdict, folder, ratio = classify("cloe-consultant", FOLDERS)
    assert verdict == "close"
    assert folder == "cloe-consult"
    assert ratio < 1.0


def test_a_name_with_no_folder_is_absent_not_dead():
    """Work assisted by agents whose folders live on another machine has no local
    directory. Treating that as 'not a project' would quietly disown it."""
    verdict, folder, _ = classify("tier3-telemetry", FOLDERS)
    assert verdict == "absent"
    assert folder is None
