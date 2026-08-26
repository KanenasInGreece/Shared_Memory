"""
Unit tests for L0 REM anti-degeneration (Local_Documentation/L0_Design_2026-08-18.md,
pre-build review fact:1346; diagnosis fact:1329/1330).

Covers: the pure truncation_is_degenerate classifier (OBJECT + LONG-STRING
rules), the solo retry policy split (degenerate → same-bound retry; honest →
existing widened ladder, F4 pinned), bounded specimen logging (N1), and the
additive metrics stamping (N4 — ok is never flipped by classification).

⛔ Fixtures state the FORM of the defect, never our private instances —
generic entity names only (public names allowed: shared-memory,
shared-memory-GitHub, shared-memory-monitor).

All Neo4j/Postgres/LLM I/O is mocked; no live infrastructure required.
"""

import os
import sys

import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
sys.path.insert(0, os.path.dirname(__file__))   # tests/ itself, for test_rem_loop

# Reuse the dynamic-import + daemon-construction helpers already established
# for rem_loop.py's own suite, rather than re-deriving them. Importing the
# SAME `rem_mod` instance test_rem_loop.py already loaded matters: calling
# load_rem_loop() a second time would exec a second, distinct module object,
# decoupled from the one _make_daemon()'s REMDaemon class actually closes
# over — patch.object(rem_mod, ...) here would then silently patch the wrong
# instance and never take effect.
from test_rem_loop import rem_mod, _make_daemon, _length_resp, _ok_resp

truncation_is_degenerate = rem_mod.truncation_is_degenerate
_truncation_specimen = rem_mod._truncation_specimen
REM_MAX_TOKENS_SOLO = rem_mod.REM_MAX_TOKENS_SOLO
REM_TRUNCATION_RETRY_FACTOR = rem_mod.REM_TRUNCATION_RETRY_FACTOR
REM_TRUNCATION_SPECIMEN_CHARS = rem_mod.REM_TRUNCATION_SPECIMEN_CHARS


# ── Fixture bodies (generic entity names only) ────────────────────────────────
# A repeated LONG string >=30 chars, used by the LONG-STRING-rule fixtures.
_REPEATED_SENTENCE = (
    "We considered switching vendors but decided against it due to cost"
)
assert len(_REPEATED_SENTENCE) >= 30


# ── 1. Classifier: truncation_is_degenerate ────────────────────────────────────

def test_object_rule_fires_on_three_exact_repeats():
    """OBJECT rule: the same flat {...} object >=3 times → degenerate."""
    body = (
        '[{"name": "WidgetCo", "rel_type": "MENTIONS"}, '
        '{"name": "WidgetCo", "rel_type": "MENTIONS"}, '
        '{"name": "WidgetCo", "rel_type": "MENTIONS"}]'
    )
    assert truncation_is_degenerate(body) is True


def test_long_string_rule_fires_on_three_exact_repeats():
    """LONG-STRING rule (F-6): Decision extras are STRING arrays, not
    objects — a >=30-char string repeated >=3 times must also fire, or an
    object-only detector calls a sentence loop honest."""
    body = '{"rejected": ["%s", "%s", "%s"' % (
        (_REPEATED_SENTENCE,) * 3
    )
    assert truncation_is_degenerate(body) is True


def test_honest_truncation_with_unique_elements_is_not_degenerate():
    """A record that legitimately ran out of room — every element distinct —
    must NOT be classified degenerate."""
    body = (
        '{"relationships": [{"name": "AlphaCorp", "rel_type": "MENTIONS"}, '
        '{"name": "BetaCorp", "rel_type": "PART_OF"}, '
        '{"name": "GammaCorp", "rel_type": "USES"}], '
        '"summary": "This record describes a distinct rollout across three vend'
    )
    assert truncation_is_degenerate(body) is False


def test_honest_output_with_repeated_short_tokens_is_not_degenerate():
    """rel_types are legitimately reused across DISTINCT triples — the
    length floor keeps the short schema token itself from ever counting, and
    the full objects differ (different `name`) so the OBJECT rule doesn't
    fire on them either."""
    body = (
        '[{"name": "AlphaCorp", "rel_type": "MENTIONS"}, '
        '{"name": "BetaCorp", "rel_type": "MENTIONS"}, '
        '{"name": "GammaCorp", "rel_type": "MENTIONS"}]'
    )
    assert truncation_is_degenerate(body) is False


def test_f7_prose_quoting_a_braced_snippet_thrice_is_accepted_residual_fp():
    """F-7 (pre-build review, accepted): prose that quotes the SAME braced
    snippet >=3 times fires the OBJECT rule even though it is not a
    relationships-array repetition loop. This is a documented, accepted
    residual false positive — its cost is one same-bound retry, never a lost
    record."""
    body = (
        'The proposed schema uses {"key": "value"} as an example. Later we '
        'again reference {"key": "value"} to show consistency. Finally '
        '{"key": "value"} appears a third time in the doc'
    )
    assert truncation_is_degenerate(body) is True


@pytest.mark.parametrize("body", ["", "   ", None,
                                   "no braces, no quotes, just prose words"])
def test_empty_garbage_non_json_fails_open(body):
    """Fail-open: empty/whitespace/None/no-JSON-shape text is never
    classified degenerate — the classifier only shortcuts an ALREADY-
    truncated call, it never blocks one on ambiguous input."""
    assert truncation_is_degenerate(body) is False


# ── 2. Policy: solo retry bound by class (N2, F4 pinned) ───────────────────────

@pytest.mark.asyncio
async def test_degenerate_truncation_retries_at_the_same_bound(monkeypatch):
    """A degenerate first truncation must retry at REM_MAX_TOKENS_SOLO again
    — never the widened (x2) bound."""
    daemon, _ = _make_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    degenerate_body = (
        '[{"name": "WidgetCo", "rel_type": "MENTIONS"}, '
        '{"name": "WidgetCo", "rel_type": "MENTIONS"}, '
        '{"name": "WidgetCo", "rel_type": "MENTIONS"}'
    )
    bounds = []

    async def _fake_post(self, url, **kwargs):
        bounds.append(kwargs.get("json", {})["max_tokens"])
        if len(bounds) == 1:
            return _length_resp(degenerate_body)
        return _ok_resp('{"summary":"complete","relationships":[]}')
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    with patch.object(rem_mod, "_parse_llm_json",
                      wraps=rem_mod._parse_llm_json) as parse:
        result, _model = await daemon._llm_process(
            "content", rem_mod.KIND_FACT, [], {}, pg_id=1)

    assert bounds == [REM_MAX_TOKENS_SOLO, REM_MAX_TOKENS_SOLO]
    assert result == {"summary": "complete", "relationships": []}
    # N3: only the final SUCCESSFUL body reaches the parser — the truncated,
    # degenerate first attempt's body never does.
    assert parse.call_count == 1


@pytest.mark.asyncio
async def test_honest_truncation_still_gets_the_widened_retry(monkeypatch):
    """F4 pinned: an honest (non-repeating) truncation must still get the
    x2-widened retry, unaffected by the degenerate branch."""
    daemon, _ = _make_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    honest_body = '{"summary":"cut","relationships":[{"name":"AlphaCorp","rel_type":"MENTIONS"}'
    bounds = []

    async def _fake_post(self, url, **kwargs):
        bounds.append(kwargs.get("json", {})["max_tokens"])
        if len(bounds) == 1:
            return _length_resp(honest_body)
        return _ok_resp('{"summary":"complete","relationships":[]}')
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    result, _model = await daemon._llm_process(
        "content", rem_mod.KIND_FACT, [], {}, pg_id=1)

    assert bounds == [REM_MAX_TOKENS_SOLO,
                      int(REM_MAX_TOKENS_SOLO * REM_TRUNCATION_RETRY_FACTOR)]
    assert result == {"summary": "complete", "relationships": []}


@pytest.mark.asyncio
async def test_second_truncation_of_either_class_fails_the_unit(monkeypatch):
    """Two truncations in a row — degenerate both times — still fail the
    unit exactly as an honest double-truncation does today."""
    daemon, _ = _make_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    degenerate_body = (
        '[{"name": "WidgetCo", "rel_type": "MENTIONS"}, '
        '{"name": "WidgetCo", "rel_type": "MENTIONS"}, '
        '{"name": "WidgetCo", "rel_type": "MENTIONS"}'
    )

    async def _fake_post(self, url, **kwargs):
        return _length_resp(degenerate_body)
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    with patch.object(rem_mod, "_parse_llm_json") as parse:
        result, _model = await daemon._llm_process(
            "content", rem_mod.KIND_FACT, [], {}, pg_id=1)

    assert result is None
    assert daemon._last_llm_failure == rem_mod.LLM_FAIL_TRUNCATED
    parse.assert_not_called()  # N3: never parsed, even on final failure


@pytest.mark.asyncio
async def test_honest_double_truncation_error_advises_retry_not_bump(monkeypatch, caplog):
    """v0.9.62 (fact:1609 + a live 2026-08-26 incident): an HONEST double
    truncation (not classified degenerate either time) used to end its ERROR
    line with "Raise REM_MAX_TOKENS_SOLO if this record is legitimately
    large" — exactly the bump decision:1330 measured and rejected (a
    repetition loop consumes ANY budget; the next pick-up completed the same
    record in far fewer tokens). The line must now say the unit retries on a
    later pick-up and must NOT advise the bump.

    Mutation check: restoring the old "Raise REM_MAX_TOKENS_SOLO if this
    record is legitimately large." wording on a scratch copy must fail this
    test."""
    daemon, _ = _make_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    honest_body = '{"summary":"cut","relationships":[{"name":"AlphaCorp","rel_type":"MENTIONS"}'

    async def _fake_post(self, url, **kwargs):
        return _length_resp(honest_body)
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    with caplog.at_level("ERROR"):
        result, _model = await daemon._llm_process(
            "content", rem_mod.KIND_FACT, [], {}, pg_id=1)

    assert result is None
    error_lines = [r.message for r in caplog.records if r.levelname == "ERROR"]
    joined = "\n".join(error_lines)
    assert "decision:1330" in joined
    assert "Raise REM_MAX_TOKENS_SOLO if" not in joined
    assert "retry" in joined.lower() and "rem_attempts" in joined


# ── 3. Logging: bounded specimen (N1) ───────────────────────────────────────────

def test_truncation_specimen_is_the_bounded_tail():
    """Pure specimen extraction: last REM_TRUNCATION_SPECIMEN_CHARS chars,
    single line (newlines collapsed). The length is pinned against a literal
    500 on one side, not only against the module constant — an equality
    between two expressions is half a guard (fact:1309): both sides moving
    with the same knob could drift together to a wrong value."""
    body = "a" * 1000 + "\nTAIL\nEND" + "b" * 10
    specimen = _truncation_specimen(body)
    assert REM_TRUNCATION_SPECIMEN_CHARS == 500   # the shipped default, literally
    assert len(specimen) <= 500
    assert "\n" not in specimen
    assert specimen.endswith("b" * 10)          # it IS the tail, not the head
    assert "a" * 1000 not in specimen            # never the full body


def test_specimen_zero_or_negative_chars_means_disabled_not_full_body(monkeypatch):
    """S-1 (fact:1347, Required): `body[-0:]` is the WHOLE body, so the value
    an operator picks to disable specimens must yield the EMPTY string — the
    exact inversion the security review executed (44,000-char body logged
    whole at CHARS=0)."""
    body = "x" * 44_000
    for n in (0, -1):
        monkeypatch.setattr(rem_mod, "REM_TRUNCATION_SPECIMEN_CHARS", n)
        assert rem_mod._truncation_specimen(body) == ""


def test_specimen_strips_terminal_control_characters(monkeypatch):
    """S-4 (fact:1347): ESC/CSI, NUL and BS must not reach journalctl —
    crafted model output cannot smuggle terminal control sequences."""
    monkeypatch.setattr(rem_mod, "REM_TRUNCATION_SPECIMEN_CHARS", 500)
    specimen = rem_mod._truncation_specimen('{"a": "b"}\x1b[31mFAKE\x00\x08')
    assert "\x1b" not in specimen and "\x00" not in specimen and "\x08" not in specimen
    assert "FAKE" in specimen                    # content survives, controls do not


def test_non_string_completion_content_classifies_as_empty():
    """S-2 (fact:1347): a non-string `content` in the envelope must coerce to
    "" (fail-open honest) rather than escape into the un-try'd solo path and
    abort the REM cycle."""
    for content in ({"t": 1}, 7, ["a"], None):
        resp = {"choices": [{"message": {"content": content},
                             "finish_reason": "length"}]}
        assert rem_mod._completion_text(resp) == ""


@pytest.mark.asyncio
async def test_truncated_call_warns_with_the_specimen(monkeypatch, caplog):
    """Integration: a truncated solo call leaves a WARN carrying the bounded
    specimen — never silently discarded (N1)."""
    daemon, _ = _make_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    long_unique_tail = "UNIQUE_MARKER_" + "x" * 50
    honest_body = '{"summary":"' + long_unique_tail

    async def _fake_post(self, url, **kwargs):
        return _length_resp(honest_body)
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    with caplog.at_level("WARNING"):
        await daemon._llm_process("content", rem_mod.KIND_FACT, [], {}, pg_id=99)

    specimen_lines = [r.message for r in caplog.records if "specimen(last" in r.message]
    assert specimen_lines, "expected a WARN carrying the bounded specimen"
    assert any(long_unique_tail in line for line in specimen_lines)


# ── 4. Metrics stamping (N4 — ok is never flipped) ──────────────────────────────

@pytest.mark.asyncio
async def test_degenerate_metrics_row_carries_note_and_specimen(monkeypatch):
    daemon, _ = _make_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    degenerate_body = (
        '[{"name": "WidgetCo", "rel_type": "MENTIONS"}, '
        '{"name": "WidgetCo", "rel_type": "MENTIONS"}, '
        '{"name": "WidgetCo", "rel_type": "MENTIONS"}'
    )

    async def _fake_post(self, url, **kwargs):
        return _length_resp(degenerate_body)
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    with patch.object(rem_mod, "record_llm_call") as rec:
        await daemon._llm_process("content", rem_mod.KIND_FACT, [], {}, pg_id=1)

    degenerate_calls = [c for c in rec.call_args_list
                        if c.kwargs.get("note") == "degenerate"]
    assert degenerate_calls, "expected at least one metrics row stamped note='degenerate'"
    for c in degenerate_calls:
        assert c.kwargs.get("specimen")
        assert len(c.kwargs["specimen"]) <= REM_TRUNCATION_SPECIMEN_CHARS
        # N4: ok is never flipped by classification — truncation is not a
        # transport/HTTP failure, so it stays at record_llm_call's default.
        assert c.kwargs.get("ok", True) is True


@pytest.mark.asyncio
async def test_honest_truncated_metrics_row_carries_note(monkeypatch):
    daemon, _ = _make_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    honest_body = '{"summary":"cut","relationships":[{"name":"AlphaCorp","rel_type":"MENTIONS"}'

    async def _fake_post(self, url, **kwargs):
        return _length_resp(honest_body)
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    with patch.object(rem_mod, "record_llm_call") as rec:
        await daemon._llm_process("content", rem_mod.KIND_FACT, [], {}, pg_id=1)

    honest_calls = [c for c in rec.call_args_list
                    if c.kwargs.get("note") == "truncated_honest"]
    assert honest_calls, "expected at least one metrics row stamped note='truncated_honest'"
    for c in honest_calls:
        assert c.kwargs.get("ok", True) is True
