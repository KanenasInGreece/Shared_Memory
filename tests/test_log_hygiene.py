"""
Unit tests for scripts/log_hygiene.py — log permission + off-event-loop writing.

Rotation/gzip are logrotate(8)'s job and are NOT tested here (no in-process
rotation by design). Covered: 0600/0700 perms on create + tighten, append,
AsyncLineWriter draining off the loop, ordering (no interleave), and the
no-running-loop sync fallback.
"""

import json
import os
import stat
import sys

import pytest


def _load():
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import log_hygiene
    return log_hygiene


def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


# ── secure_path / append_secure ─────────────────────────────────────────────────

def test_secure_path_creates_with_safe_perms(tmp_path):
    lh = _load()
    target = tmp_path / "logs" / "gateway-audit.jsonl"
    lh.secure_path(str(target))
    assert target.exists()
    assert _mode(target) == 0o600
    assert _mode(target.parent) == 0o700


def test_secure_path_tightens_existing_world_readable(tmp_path):
    lh = _load()
    target = tmp_path / "a.jsonl"
    target.write_text("x\n")
    os.chmod(target, 0o644)
    lh.secure_path(str(target))
    assert _mode(target) == 0o600


def test_append_secure_writes_lines_and_keeps_perms(tmp_path):
    lh = _load()
    target = tmp_path / "logs" / "rem-audit.jsonl"
    lh.append_secure(str(target), '{"a":1}')
    lh.append_secure(str(target), '{"a":2}')
    lines = target.read_text().splitlines()
    assert lines == ['{"a":1}', '{"a":2}']
    assert _mode(target) == 0o600


# ── AsyncLineWriter ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_async_writer_drains_in_order(tmp_path):
    lh = _load()
    target = tmp_path / "audit.jsonl"
    w = lh.AsyncLineWriter(str(target))
    for i in range(50):
        w.write(json.dumps({"n": i}))
    await w.flush()
    try:
        lines = target.read_text().splitlines()
        assert len(lines) == 50
        # ordered + every line is intact JSON (no interleave/torn writes)
        assert [json.loads(x)["n"] for x in lines] == list(range(50))
        assert _mode(target) == 0o600
    finally:
        await w.aclose()


@pytest.mark.asyncio
async def test_async_writer_flush_is_idempotent_when_empty(tmp_path):
    lh = _load()
    w = lh.AsyncLineWriter(str(tmp_path / "audit.jsonl"))
    await w.flush()        # nothing enqueued — must return immediately, not hang
    await w.aclose()


def test_async_writer_sync_fallback_no_loop(tmp_path):
    """With no running event loop, write() must append inline (not silently drop)."""
    lh = _load()
    target = tmp_path / "audit.jsonl"
    w = lh.AsyncLineWriter(str(target))
    w.write('{"sync":true}')          # no asyncio loop running here
    assert target.read_text().strip() == '{"sync":true}'
    assert _mode(target) == 0o600


# ── R-5: per-line drop policy ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_drop_newest_when_full_never_enqueues_and_never_evicts(tmp_path):
    """MUTATION TARGET: with drop_newest_when_full=True, a full queue must
    discard the NEW line — the queue's existing (older) entries are
    untouched, unlike the default drop-oldest policy."""
    lh = _load()
    target = tmp_path / "audit.jsonl"
    w = lh.AsyncLineWriter(str(target), maxsize=3)
    # Fill the queue without a drain task running yet is racy in practice
    # (the drain task starts on first write and may consume immediately),
    # so instead assert the CONTRACT directly against a queue we control.
    for i in range(3):
        w._q.put_nowait(f'{{"n":{i}}}')
    assert w._q.full()
    w.write('{"n":"newest"}', drop_newest_when_full=True)
    assert w.dropped == 1
    # The queue's contents are exactly what was there before — nothing evicted.
    remaining = []
    while not w._q.empty():
        remaining.append(w._q.get_nowait())
    assert remaining == ['{"n":0}', '{"n":1}', '{"n":2}']


@pytest.mark.asyncio
async def test_write_default_policy_still_drops_oldest_when_full(tmp_path):
    """Sanity: the default (no drop_newest_when_full) policy is unchanged —
    this is the regression guard for R-5's OTHER event types."""
    lh = _load()
    target = tmp_path / "audit.jsonl"
    w = lh.AsyncLineWriter(str(target), maxsize=3)
    for i in range(3):
        w._q.put_nowait(f'{{"n":{i}}}')
    w.write('{"n":"newest"}')  # default: drop_newest_when_full=False
    assert w.dropped == 1
    remaining = []
    while not w._q.empty():
        remaining.append(w._q.get_nowait())
    assert remaining == ['{"n":1}', '{"n":2}', '{"n":"newest"}']  # oldest (n:0) evicted


# ── last_dropped_ts: WHEN the trail went incomplete ──────────────────────────
# `dropped` says audit lines were lost; it cannot say whether that is still
# happening. The count resets with the process, so a consumer diffing polls
# reads a restart as "never dropped" — the stamp has to be taken at the drop
# site. Both policies drop, so both must stamp.

def _parse_iso_utc(value):
    from datetime import datetime, timezone
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None, f"last_dropped_ts must be tz-aware, got {value!r}"
    return parsed.astimezone(timezone.utc)


@pytest.mark.asyncio
async def test_last_dropped_ts_is_none_until_a_line_is_actually_dropped(tmp_path):
    lh = _load()
    w = lh.AsyncLineWriter(str(tmp_path / "audit.jsonl"), maxsize=3)
    assert w.dropped == 0
    assert w.last_dropped_ts is None
    w._q.put_nowait('{"n":0}')          # queued, not dropped
    assert w.last_dropped_ts is None


@pytest.mark.asyncio
async def test_drop_newest_policy_stamps_last_dropped_ts_with_the_drop_time(tmp_path):
    from datetime import datetime, timezone
    lh = _load()
    w = lh.AsyncLineWriter(str(tmp_path / "audit.jsonl"), maxsize=3)
    for i in range(3):
        w._q.put_nowait(f'{{"n":{i}}}')
    before = datetime.now(timezone.utc)
    w.write('{"n":"newest"}', drop_newest_when_full=True)
    after = datetime.now(timezone.utc)
    assert w.dropped == 1
    assert before <= _parse_iso_utc(w.last_dropped_ts) <= after


@pytest.mark.asyncio
async def test_drop_oldest_policy_stamps_last_dropped_ts_with_the_drop_time(tmp_path):
    from datetime import datetime, timezone
    lh = _load()
    w = lh.AsyncLineWriter(str(tmp_path / "audit.jsonl"), maxsize=3)
    for i in range(3):
        w._q.put_nowait(f'{{"n":{i}}}')
    before = datetime.now(timezone.utc)
    w.write('{"n":"newest"}')            # default policy: drop oldest
    after = datetime.now(timezone.utc)
    assert w.dropped == 1
    assert before <= _parse_iso_utc(w.last_dropped_ts) <= after


@pytest.mark.asyncio
async def test_last_dropped_ts_advances_to_the_most_recent_drop(tmp_path):
    lh = _load()
    w = lh.AsyncLineWriter(str(tmp_path / "audit.jsonl"), maxsize=3)
    for i in range(3):
        w._q.put_nowait(f'{{"n":{i}}}')
    w.write('{"n":"first-drop"}', drop_newest_when_full=True)
    first = _parse_iso_utc(w.last_dropped_ts)
    w.write('{"n":"second-drop"}', drop_newest_when_full=True)
    second = _parse_iso_utc(w.last_dropped_ts)
    assert w.dropped == 2
    assert second >= first


# ── O-4: symlink refusal + fd-based chmod + ancestor chain ──────────────────

def test_secure_path_refuses_a_preexisting_symlink_rather_than_following_it(tmp_path):
    """⚑ Security review O-4: a different-uid attacker who can pre-plant a
    symlink at a relocated CREDENTIAL_AUDIT_LOG_PATH must not get this
    process to append-and-0600 a file of their own choosing. MUTATION
    TARGET: drop O_NOFOLLOW from the open() flags and this fails (the
    symlink target gets silently opened/tightened instead of refused)."""
    lh = _load()
    real_target = tmp_path / "attacker_owned_file"
    real_target.write_text("not the gateway's data")
    os.chmod(real_target, 0o644)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    link = logs_dir / "credential-audit.jsonl"
    link.symlink_to(real_target)

    with pytest.raises(OSError):
        lh.secure_path(str(link))

    # The attacker's file is untouched — neither written to nor tightened.
    assert real_target.read_text() == "not the gateway's data"
    assert _mode(real_target) == 0o644


def test_secure_path_chmods_every_ancestor_it_creates(tmp_path):
    """MUTATION TARGET: chmod only the immediate parent (the pre-fix
    behaviour) and the middle levels of a multi-segment relocation stay at
    the process umask (typically 0755, world-traversable)."""
    lh = _load()
    target = tmp_path / "a" / "b" / "c" / "audit.jsonl"
    lh.secure_path(str(target))
    assert _mode(tmp_path / "a") == 0o700
    assert _mode(tmp_path / "a" / "b") == 0o700
    assert _mode(tmp_path / "a" / "b" / "c") == 0o700
    assert _mode(target) == 0o600


def test_secure_path_does_not_chmod_a_preexisting_ancestor_it_did_not_create(tmp_path):
    """Only ancestors THIS call creates get tightened — an operator's own
    pre-existing directory structure (which may deliberately have wider
    perms for other reasons) is not silently narrowed."""
    lh = _load()
    preexisting = tmp_path / "shared_dir"
    preexisting.mkdir(mode=0o755)
    os.chmod(preexisting, 0o755)  # mkdir(mode=) is subject to umask; be exact
    target = preexisting / "logs" / "audit.jsonl"
    lh.secure_path(str(target))
    assert _mode(preexisting) == 0o755          # untouched
    assert _mode(preexisting / "logs") == 0o700  # created by this call


# ── C (SEC round, R3-4 + IPv6): scheme-generic, case/bracket-preserving,
#    idempotent scrub_url_credentials ─────────────────────────────────────
# Prove-failing-first evidence (run against unmodified log_hygiene.py,
# pre-SEC-C): the old implementation matched only `r"https?://\S+"` (so a
# `postgresql://`/`bolt://`/`redis://` DSN and any uppercase scheme passed
# through VERBATIM, credentials included) and rebuilt the netloc from
# `parsed.hostname` (which lowercases the host and strips IPv6 brackets), so
# `http://MyHost:8000` -> `http://myhost:8000` and `http://[::1]:8000` ->
# `http://::1:8000` on the FIRST pass — confirmed by running exactly these
# assertions against that code before the fix landed.

def test_dsn_schemes_scrub_credentials(tmp_path):
    lh = _load()
    cases = [
        "postgresql://user:s3cret@host:5432/db",
        "bolt://neo4j:s3cret@host:7687",
        "redis://user:s3cret@host:6379/0",
    ]
    for c in cases:
        out = lh.scrub_url_credentials(c)
        assert "s3cret" not in out, f"{c!r} leaked its credential: {out!r}"
        assert "host" in out


def test_uppercase_scheme_scrubs_credentials():
    lh = _load()
    out = lh.scrub_url_credentials("HTTP://u:p@h:8000")
    assert "u:p" not in out
    assert "p" not in out.split("://", 1)[1].split(":")[0]  # no leaked userinfo before host
    assert "h:8000" in out


def test_netloc_less_dsn_preserves_double_slash():
    lh = _load()
    assert lh.scrub_url_credentials("postgresql:///agent_data") == "postgresql:///agent_data"


def test_mixed_case_host_byte_identical_first_pass():
    lh = _load()
    assert lh.scrub_url_credentials("http://MyHost:8000") == "http://MyHost:8000"


def test_ipv6_bracketed_host_byte_identical_first_pass():
    lh = _load()
    assert lh.scrub_url_credentials("http://[::1]:8000") == "http://[::1]:8000"


def test_ipv6_with_userinfo_scrubs_to_bracketed_form_intact():
    lh = _load()
    out = lh.scrub_url_credentials("http://u:p@[fd00::1]:9/x")
    assert out == "http://[fd00::1]:9/x"


@pytest.mark.parametrize("text", [
    "http://u:p@[fd00::1]:9/x",             # IPv6 + userinfo
    "http://[fd00::1]:9/x",                 # IPv6 + port, no userinfo
    "http://u:p@10.0.0.1:8000",             # IPv4 + userinfo
    "http://u:p@myhost:8000",               # hostname + userinfo
    "http://MyHost:8000",                   # mixed-case, no userinfo
    "HTTP://MyHost:8000",                   # mixed-case host + uppercase scheme
    "postgresql://postgres:ab/cd@localhost:5432/agent_data",  # F2: slash-in-password DSN
    "http://user:http://nested@host/path",                    # F2: nested-scheme userinfo
    "plain log text with no url in it at all",
])
def test_scrub_is_idempotent(text):
    lh = _load()
    once = lh.scrub_url_credentials(text)
    twice = lh.scrub_url_credentials(once)
    assert once == twice


# ── Fix round F8 (QA MED-3): scheme CASE is part of byte-identity too ──────
# `parsed.scheme` lowercases (urlsplit's own contract) — the old
# reconstruction used it directly, so a credential-free `HTTP://MyHost:8000`
# rendered `http://MyHost:8000`: host/port/brackets preserved, scheme
# silently lowercased. Prove-failing-first: this assertion fails against the
# unmodified (parsed.scheme-based) reconstruction.

def test_uppercase_scheme_byte_identical_when_credential_free():
    lh = _load()
    assert lh.scrub_url_credentials("HTTP://MyHost:8000") == "HTTP://MyHost:8000"


def test_uppercase_scheme_with_userinfo_preserves_scheme_case_after_scrub():
    lh = _load()
    out = lh.scrub_url_credentials("HTTP://u:p@MyHost:8000")
    assert out == "HTTP://MyHost:8000"


# ── Fix round F2 (SEC2 HIGH-1): fail CLOSED, not open, when a malformed
#    authority hides userinfo inside the PATH (a "/" in the password makes
#    urlsplit end the netloc early, so the old netloc-based strip left the
#    "@"-bearing tail — credential included — completely untouched: both
#    fixtures below passed through byte-identical on unmodified code,
#    confirmed by running these two assertions against it before this fix). ─

def test_dsn_with_slash_in_password_fails_closed_not_leaked():
    lh = _load()
    raw = "postgresql://postgres:ab/cd@localhost:5432/agent_data"
    out = lh.scrub_url_credentials(raw)
    assert out == "<url-redacted>"
    assert "ab/cd" not in out
    assert out != raw


def test_nested_scheme_shaped_userinfo_fails_closed_not_leaked():
    lh = _load()
    raw = "http://user:http://nested@host/path"
    out = lh.scrub_url_credentials(raw)
    assert out == "<url-redacted>"
    assert "nested" not in out
    assert out != raw


def test_clean_url_with_at_sign_only_in_query_still_scrubs_query_and_keeps_no_at():
    """A query-string "@" (dropped anyway, R-2) must never trip the F2 fail-
    closed guard — only an "@" in the authority/path portion is ambiguous."""
    lh = _load()
    out = lh.scrub_url_credentials("http://host:8000/path?token=abc@def")
    assert out == "http://host:8000/path"


def test_secure_path_uses_fchmod_not_a_separate_chmod_by_path(tmp_path):
    """MUTATION TARGET: revert to os.chmod(p, FILE_MODE) after os.open() and
    this test's spy no longer distinguishes the two — but more importantly,
    a TOCTOU window reopens. This spies on os.chmod to confirm it is never
    called for the FILE itself (only os.fchmod is used for that; os.chmod is
    still legitimately used for directory ancestors)."""
    import unittest.mock as mock
    lh = _load()
    target = tmp_path / "audit.jsonl"
    with mock.patch.object(lh.os, "chmod") as spy_chmod:
        lh.secure_path(str(target))
    for call in spy_chmod.call_args_list:
        assert str(call.args[0]) != str(target), (
            "os.chmod must never be called on the FILE path — os.fchmod on "
            "the open fd is what makes this TOCTOU-free"
        )
    assert _mode(target) == 0o600  # still tightened, just via fchmod
