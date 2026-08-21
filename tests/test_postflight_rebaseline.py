"""W-P (postflight re-baseline mode) -- phrase-selection logic.

Only the deterministic phrase-selection function is testable in this mocked
suite. It is embedded in shared-memory/scripts/postflight.sh (between the
`# >>> SELECT_SUMMARY_PHRASE` / `# <<< SELECT_SUMMARY_PHRASE` markers) rather
than duplicated here: this file extracts that block VERBATIM and runs it
standalone via subprocess with fixture stdin, so the test exercises the
actual shipped source, never a hand-written reimplementation that could
silently drift from it.

Everything else the W-P change touches -- mode selection from a live
community_summaries count, the A4/A5/A6 gateway calls, the docker exec
summary-selection query -- requires a live gateway + Postgres + Neo4j stack
and is NOT exercised here (matches the rest of this suite's mocked-only
scope). Those paths are verified by running postflight.sh on this machine's
reference install (see the builder's handoff), which the corpus already
holds live summaries for.
"""
import re
import subprocess
from pathlib import Path

POSTFLIGHT = (Path(__file__).parent.parent / "shared-memory" / "scripts"
              / "postflight.sh")

BEGIN_MARKER = "# >>> SELECT_SUMMARY_PHRASE"
END_MARKER = "# <<< SELECT_SUMMARY_PHRASE"


def _extract_phrase_selector_source() -> str:
    text = POSTFLIGHT.read_text()
    pattern = re.escape(BEGIN_MARKER) + r".*?\n(.*?)\n" + re.escape(END_MARKER)
    m = re.search(pattern, text, re.S)
    assert m, (
        f"could not find a {BEGIN_MARKER} ... {END_MARKER} block in "
        f"{POSTFLIGHT} -- the extraction markers moved or were removed"
    )
    return m.group(1)


def run_select_phrase(content: str) -> subprocess.CompletedProcess:
    """Runs the extracted `select_summary_phrase` bash function standalone,
    feeding `content` on stdin exactly as postflight.sh does."""
    source = _extract_phrase_selector_source()
    return subprocess.run(
        ["bash", "-c", source + "\nselect_summary_phrase"],
        input=content, capture_output=True, text=True, timeout=15,
    )


def test_markers_present_exactly_once():
    text = POSTFLIGHT.read_text()
    assert text.count(BEGIN_MARKER) == 1
    assert text.count(END_MARKER) == 1


def test_extracted_block_defines_the_function():
    source = _extract_phrase_selector_source()
    assert "select_summary_phrase()" in source


# ── Determinism ──────────────────────────────────────────────────────────

def test_same_content_yields_the_same_phrase_every_run():
    content = "The gateway restarts cleanly after a fingerprint mismatch and re-derives capacity."
    first = run_select_phrase(content)
    second = run_select_phrase(content)
    assert first.returncode == 0
    assert first.stdout == second.stdout
    assert first.stdout.strip() != ""


def test_phrase_is_a_prefix_of_the_cleaned_content_first_eight_words():
    content = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    result = run_select_phrase(content)
    assert result.returncode == 0
    assert result.stdout.strip() == "alpha beta gamma delta epsilon zeta eta theta"


# ── Hostile content: [FACT ...] / [DECISION ...] prefix lines ─────────────

def test_strips_bare_fact_prefix_line():
    content = "[FACT] the reranker fails open when the probe times out repeatedly"
    result = run_select_phrase(content)
    assert result.returncode == 0
    phrase = result.stdout.strip()
    assert "[FACT]" not in phrase
    assert phrase == "the reranker fails open when the probe times"


def test_strips_decorated_fact_and_decision_prefix_lines_multiline():
    content = (
        '[FACT kind=observation from="glxvm" recorded=2026-08-21 pg_id=1402] '
        "first line of real content here indeed\n"
        "[DECISION kind=architecture pg_id=1403] second line follows after that"
    )
    result = run_select_phrase(content)
    assert result.returncode == 0
    phrase = result.stdout.strip()
    assert "[FACT" not in phrase
    assert "[DECISION" not in phrase
    assert phrase == "first line of real content here indeed second"


def test_falls_back_to_raw_content_when_every_line_is_prefix_only():
    # No prose survives stripping -- the function must still return
    # something rather than crash or emit nothing (never a false EMPTY).
    content = "[FACT]"
    result = run_select_phrase(content)
    assert result.returncode == 0
    assert result.stdout.strip() == "[FACT]"


# ── Unicode ─────────────────────────────────────────────────────────────

def test_unicode_content_is_preserved_and_split_on_unicode_whitespace():
    content = "[FACT] Θεσσαλονίκη είναι όμορφη πόλη με πολλή ιστορία και 🎉 σήμερα εδώ"
    result = run_select_phrase(content)
    assert result.returncode == 0
    phrase = result.stdout.strip()
    assert phrase == "Θεσσαλονίκη είναι όμορφη πόλη με πολλή ιστορία και"
    assert "Θεσσαλονίκη" in phrase


# ── Short content ───────────────────────────────────────────────────────

def test_single_word_content_returns_that_word():
    result = run_select_phrase("hi")
    assert result.returncode == 0
    assert result.stdout.strip() == "hi"


def test_two_word_content_returns_both_words_not_padded_or_truncated_wrong():
    result = run_select_phrase("[FACT] hi there")
    assert result.returncode == 0
    assert result.stdout.strip() == "hi there"


# ── Empty content: exit 1, no stdout (never a false phrase) ───────────────

def test_empty_content_exits_nonzero_with_no_stdout():
    result = run_select_phrase("")
    assert result.returncode == 1
    assert result.stdout.strip() == ""


def test_whitespace_only_content_exits_nonzero_with_no_stdout():
    result = run_select_phrase("   \n\t  \n  ")
    assert result.returncode == 1
    assert result.stdout.strip() == ""
