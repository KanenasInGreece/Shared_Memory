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
