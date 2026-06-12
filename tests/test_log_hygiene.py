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
