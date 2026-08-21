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


# ── Fix-round probe-6 (decision:1435, grounded in fact:1434): the twelve
# tests above only prove select_summary_phrase computes correctly in
# isolation -- none of them pin that A5 actually CALLS it. Without this
# guard, deleting the call site from A5 (e.g. hardcoding a phrase, or
# silently regressing to something else) would leave every other test in
# this file green. This test greps postflight.sh's own A5 SECTION for a
# real invocation -- piped into the function, not merely the string
# "select_summary_phrase" appearing anywhere (which the function's own
# definition and its doc comments already satisfy trivially) --
# capture-surface-test style (grep-style assertion against the shipped
# source, per tests/test_capture_surface_documented.py's pattern).

A5_SECTION_START = "# ── A5 — read path, honestly graded"
A5_SECTION_END = "# ── A6 — baseline emission"


def _extract_a5_section() -> str:
    text = POSTFLIGHT.read_text()
    start = text.find(A5_SECTION_START)
    end = text.find(A5_SECTION_END)
    assert start != -1, (
        f"could not find the A5 section header {A5_SECTION_START!r} in "
        f"{POSTFLIGHT} -- section markers moved or were renamed"
    )
    assert end != -1 and end > start, (
        f"could not find the A6 section header {A5_SECTION_END!r} after "
        f"the A5 header in {POSTFLIGHT} -- section markers moved or were "
        f"renamed"
    )
    return text[start:end]


def test_a5_section_actually_invokes_the_phrase_selector():
    a5 = _extract_a5_section()
    # A genuine call site: piped INTO the function, inside a command
    # substitution, assigned to `phrase`. Not merely a substring match --
    # that would also match a comment or the function's own name in prose.
    assert '| select_summary_phrase)"' in a5, (
        "A5's re-baseline branch no longer pipes content into "
        "select_summary_phrase() -- the phrase-selection call site was "
        "deleted, renamed, or replaced with something else. This is "
        "exactly the gap the fix-round review (fact:1434, probe 6) named: "
        "no other test in this file would catch that."
    )
    assert 'phrase="$(printf' in a5, (
        "expected the call site to assign the selector's output to "
        "`phrase` via a printf | select_summary_phrase command "
        "substitution, matching every other content-reading call in this "
        "script (json_get/json_keys use the same idiom)"
    )


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
        '[FACT kind=measured from="shared-memory/scripts/coordinator.py" '
        "recorded=2026-01-01 pg_id=123] "
        "first line of real content here indeed\n"
        "[DECISION kind=architecture pg_id=124] second line follows after that"
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


# ── Control characters (SEC-01, decision:1439 -- correcting fact:1437's
# CRITICAL "terminal escape sequence injection" claim to REQUIRED: the real
# impact is operator-visible output spoofing / log poisoning via whatever
# terminal renders postflight's output, not code execution -- printf '%s'
# never interprets escapes in its argument). C0 (0x00-0x1F, ESC 0x1B
# included) and C1 (0x80-0x9F) control characters must be stripped from the
# FINAL phrase, whether they arrive embedded in a plain word or inside a
# fabricated ANSI-looking escape sequence. ─────────────────────────────────

def test_strips_esc_and_other_c0_control_characters_embedded_in_words():
    content = "[FACT] hello\x1bworld control\x07char test here now"
    result = run_select_phrase(content)
    assert result.returncode == 0
    phrase = result.stdout.strip()
    assert "\x1b" not in phrase
    assert "\x07" not in phrase
    assert phrase == "helloworld controlchar test here now"


def test_strips_esc_from_a_fabricated_ansi_escape_sequence_but_keeps_printable_bytes():
    # A realistic attack shape: ESC [ 31 m ... ESC [ 0 m (an ANSI color
    # sequence) around fake alert text. SEC-01 strips only the control
    # BYTES (the two ESC characters) -- the surrounding printable digits
    # and brackets are ordinary content, not control characters, and are
    # left alone. This is the literal ruling's scope, not a full ANSI
    # sequence stripper.
    content = "\x1b[31mALERT\x1b[0m fake error message here for testing"
    result = run_select_phrase(content)
    assert result.returncode == 0
    phrase = result.stdout.strip()
    assert "\x1b" not in phrase
    assert phrase == "[31mALERT[0m fake error message here for testing"


def test_strips_c1_control_characters():
    # \x9c (STRING TERMINATOR) / \x9d (OPERATING SYSTEM COMMAND) -- real C1
    # control codes used in 8-bit terminal escape tricks. Written as
    # explicit \x escapes, not literal bytes, so the source stays readable
    # -- the two literal ones a prior draft of this test embedded directly
    # were invisible on screen, which is itself the class SEC-01 exists
    # to catch.
    content = "[FACT] test \x9cinjected\x9d more words here indeed"
    result = run_select_phrase(content)
    assert result.returncode == 0
    phrase = result.stdout.strip()
    assert "\x9c" not in phrase
    assert "\x9d" not in phrase
    assert phrase == "test injected more words here indeed"


def test_content_of_only_control_characters_exits_nonzero_with_no_stdout():
    # Every "word" is entirely control bytes -- after stripping, the phrase
    # is empty, which must be treated exactly like no-words-at-all (exit 1,
    # no stdout), never an empty string printed as if it were a phrase.
    content = "\x01\x02\x03\x04\x05"
    result = run_select_phrase(content)
    assert result.returncode == 1
    assert result.stdout.strip() == ""


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
