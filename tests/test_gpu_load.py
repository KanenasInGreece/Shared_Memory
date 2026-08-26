"""Unit tests for the cross-architecture GPU-load probe (gpu_load.py).

Covers the pure decision function over an `nvtop -s` snapshot, the fail-open
branches of the async probe, AND the timeout-reap / self-disable path
(fact:1645) — the latter DOES spawn a real subprocess (a small fake-nvtop
script written to tmp_path), because the thing under test is whether that
subprocess gets reaped, not just whether the probe fails open.

The probe is platform-agnostic: it gates on raw GPU utilisation, with no
assumption about which process drives the GPU. Any consumer — the local LLM, a
direct chat, an unrelated app — counts as busy, because dreaming should yield to
all of them.
"""
import asyncio
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
import gpu_load
from gpu_load import parse_busy, inference_gpu_busy


@pytest.fixture(autouse=True)
def _reset_probe_state():
    """Every timeout/disable test mutates module-level state; reset it before
    AND after each test so tests never leak into each other regardless of
    order or a failure mid-test."""
    def _reset():
        gpu_load._warned = False
        gpu_load._consecutive_hangs = 0
        gpu_load._leaked_children = 0
        gpu_load._disabled_reason = None
        gpu_load._env_parse_warned = set()
    _reset()
    yield
    _reset()


def _write_fake_nvtop(tmp_path, body: str, name: str = "fake_nvtop"):
    """A minimal executable script standing in for nvtop. `exec`-ing the sleep
    (rather than a plain `sleep 5`) matters: without exec, killing the shell
    leaves the sleep behind as a stray orphan child when the shell dies, which
    is exactly the kind of leak this module exists to prevent in nvtop itself
    — so the test fixture must not introduce its own version of the bug."""
    script = tmp_path / name
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _gpu(util, cmdlines=()):
    return {
        "device_name": "Test GPU",
        "gpu_util": util,
        "processes": [{"cmdline": c, "gpu_usage": "0%"} for c in cmdlines],
    }


# ── parse_busy: utilisation gating (default = every GPU) ─────────────────────

def test_gpu_above_threshold_is_busy():
    snap = [_gpu("80%", ["/home/x/.lmstudio/.internal/utils/llmworker.js"])]
    assert parse_busy(snap, threshold=50) is True


def test_gpu_below_threshold_is_idle():
    snap = [_gpu("12%", ["lmstudio llmworker.js"])]
    assert parse_busy(snap, threshold=50) is False


def test_unrecognised_process_still_counts_as_busy():
    # A GPU pegged by a direct chat / desktop app / any non-LLM workload MUST
    # count as busy — dreaming yields to all GPU consumers, not just known servers.
    snap = [_gpu("100%", ["/opt/google/chrome/chrome --type=gpu-process"])]
    assert parse_busy(snap, threshold=50) is True


def test_any_busy_gpu_counts():
    # Multi-GPU box: GPU 0 idle, GPU 1 pegged by some workload → busy.
    snap = [
        _gpu("5%", ["chrome"]),
        _gpu("88%", ["llama-server -m model.gguf"]),
    ]
    assert parse_busy(snap, threshold=50) is True


def test_all_idle_is_not_busy():
    snap = [_gpu("3%", ["chrome"]), _gpu("10%", ["llmworker"])]
    assert parse_busy(snap, threshold=50) is False


# ── parse_busy: explicit GPU indices override ────────────────────────────────

def test_explicit_indices_gate_only_those_gpus():
    snap = [_gpu("90%", ["chrome"]), _gpu("10%", ["llmworker"])]
    # Gate on GPU 0 only → busy; gate on GPU 1 only → idle.
    assert parse_busy(snap, gpu_indices=[0], threshold=50) is True
    assert parse_busy(snap, gpu_indices=[1], threshold=50) is False


def test_out_of_range_index_is_skipped():
    snap = [_gpu("90%", ["llmworker"])]
    assert parse_busy(snap, gpu_indices=[5], threshold=50) is False


# ── parse_busy: malformed / edge input ───────────────────────────────────────

def test_non_list_snapshot_is_not_busy():
    assert parse_busy({"oops": 1}, threshold=50) is False


def test_empty_snapshot_is_not_busy():
    assert parse_busy([], threshold=50) is False


def test_threshold_boundary_is_inclusive():
    snap = [_gpu("50%", ["llmworker"])]
    assert parse_busy(snap, threshold=50) is True


@pytest.mark.parametrize("raw,expected", [
    ("52%", 52), ("100%", 100), ("0%", 0), (None, 0), ("", 0), ("  7 % ", 7), ("weird", 0),
])
def test_pct_parsing(raw, expected):
    assert gpu_load._pct(raw) == expected


# ── inference_gpu_busy: fail-open branches ───────────────────────────────────

@pytest.mark.asyncio
async def test_slot_aware_disabled_returns_not_busy(monkeypatch):
    monkeypatch.setenv("SLOT_AWARE", "0")
    assert await inference_gpu_busy() is False


@pytest.mark.asyncio
async def test_missing_nvtop_fails_open(monkeypatch):
    monkeypatch.setenv("SLOT_AWARE", "1")
    monkeypatch.setenv("NVTOP_BIN", "definitely-not-a-real-binary-xyz")
    # _reset_probe_state (autouse) already zeroed _warned so the branch fires.
    assert await inference_gpu_busy() is False


# ── gpu_probe_available + inference_busy_state: tri-state for telemetry ───────
# The read-only "busy" surface MUST distinguish "cannot tell" from "idle" so the
# monitor never renders a false "idle" when nvtop is absent or gating is off.

from gpu_load import gpu_probe_available, inference_busy_state


def test_probe_unavailable_when_slot_aware_off(monkeypatch):
    monkeypatch.setenv("SLOT_AWARE", "0")
    assert gpu_probe_available() is False


def test_probe_unavailable_when_nvtop_missing(monkeypatch):
    monkeypatch.setenv("SLOT_AWARE", "1")
    monkeypatch.setenv("NVTOP_BIN", "definitely-not-a-real-binary-xyz")
    assert gpu_probe_available() is False


@pytest.mark.asyncio
async def test_state_unknown_when_nvtop_missing(monkeypatch):
    # The whole point: nvtop absent => "unknown", NEVER "idle". A fail-open False
    # from inference_gpu_busy() must not be reported to the monitor as idle.
    monkeypatch.setenv("SLOT_AWARE", "1")
    monkeypatch.setenv("NVTOP_BIN", "definitely-not-a-real-binary-xyz")
    assert await inference_busy_state() == "unknown"


@pytest.mark.asyncio
async def test_state_unknown_when_slot_aware_off(monkeypatch):
    monkeypatch.setenv("SLOT_AWARE", "0")
    assert await inference_busy_state() == "unknown"


@pytest.mark.asyncio
async def test_state_busy_and_idle_when_probe_available(monkeypatch):
    # Probe available → delegate to the boolean gate, mapping True→busy, False→idle.
    monkeypatch.setattr(gpu_load, "gpu_probe_available", lambda: True)

    async def _busy():
        return True

    async def _idle():
        return False

    monkeypatch.setattr(gpu_load, "inference_gpu_busy", _busy)
    assert await inference_busy_state() == "busy"
    monkeypatch.setattr(gpu_load, "inference_gpu_busy", _idle)
    assert await inference_busy_state() == "idle"


# ── inference_gpu_busy: timeout reaps the child (fact:1645) ──────────────────
# On a host where nvtop blocks past NVTOP_TIMEOUT_SEC (observed cause: a
# GPU-fence D-state wait) the OLD code returned False without touching the
# child -- 926 leaked D-state processes, 2.98GB RSS, over 17h. These tests
# exercise the reap path against a REAL subprocess (a fake-nvtop script),
# because "was it actually killed and awaited" cannot be answered by mocking
# asyncio away.

def _capture_create_subprocess_exec(monkeypatch):
    """Wrap asyncio.create_subprocess_exec so a test can inspect/count the
    real Process objects gpu_load spawns, without changing its behaviour."""
    real_exec = asyncio.create_subprocess_exec
    calls = []

    async def _wrapped(*args, **kwargs):
        proc = await real_exec(*args, **kwargs)
        calls.append(proc)
        return proc

    monkeypatch.setattr(gpu_load.asyncio, "create_subprocess_exec", _wrapped)
    return calls


@pytest.mark.asyncio
async def test_timeout_kills_and_reaps_child(monkeypatch, tmp_path):
    """(a) MUTATION: remove `proc.kill()` from _reap_after_timeout -> this
    test dies (the child is never signalled, so os.kill(pid, 0) keeps
    succeeding instead of raising ProcessLookupError). Verified on a scratch
    copy -- see HANDOFF.md."""
    fake_nvtop = _write_fake_nvtop(tmp_path, "exec sleep 5")
    monkeypatch.setenv("SLOT_AWARE", "1")
    monkeypatch.setenv("NVTOP_BIN", str(fake_nvtop))
    monkeypatch.setenv("NVTOP_TIMEOUT_SEC", "0.2")
    monkeypatch.setenv("NVTOP_KILL_WAIT_SEC", "3.0")
    calls = _capture_create_subprocess_exec(monkeypatch)

    result = await inference_gpu_busy()

    assert result is False
    assert len(calls) == 1
    proc = calls[0]
    # A killed-but-unreaped zombie still answers os.kill(pid, 0) (so can pid
    # reuse) -- returncode is the assertion that actually distinguishes
    # "reaped" from "leaked".
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)
    assert proc.returncode == -9  # SIGKILL
    assert gpu_load._consecutive_hangs == 1
    assert gpu_load._leaked_children == 0


@pytest.mark.asyncio
async def test_disables_after_max_consecutive_hangs(monkeypatch, tmp_path):
    """(b) MUTATION: remove the `if _disabled_reason is not None: return
    False` early exit in inference_gpu_busy -> this test dies (the 3rd call
    spawns a 3rd child instead of being refused). Verified on a scratch
    copy -- see HANDOFF.md."""
    fake_nvtop = _write_fake_nvtop(tmp_path, "exec sleep 5")
    monkeypatch.setenv("SLOT_AWARE", "1")
    monkeypatch.setenv("NVTOP_BIN", str(fake_nvtop))
    monkeypatch.setenv("NVTOP_TIMEOUT_SEC", "0.1")
    monkeypatch.setenv("NVTOP_KILL_WAIT_SEC", "3.0")
    monkeypatch.setenv("NVTOP_MAX_CONSECUTIVE_HANGS", "2")
    calls = _capture_create_subprocess_exec(monkeypatch)

    for _ in range(2):
        assert await inference_gpu_busy() is False
    assert len(calls) == 2

    status = gpu_load.probe_status()
    assert status["state"] == "disabled_after_hangs"
    assert status["consecutive_hangs"] == 2
    assert gpu_load.gpu_probe_available() is False
    assert await gpu_load.inference_busy_state() == "unknown"

    # The (N+1)th call must NOT spawn another child.
    assert await inference_gpu_busy() is False
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_success_between_timeouts_resets_consecutive_hangs(monkeypatch, tmp_path):
    """(c) A clean snapshot clears the hang streak -- only CONSECUTIVE
    timeouts count toward the disable cap."""
    hanging = _write_fake_nvtop(tmp_path, "exec sleep 5", name="hanging_nvtop")
    healthy = _write_fake_nvtop(tmp_path, "echo '[]'", name="healthy_nvtop")
    monkeypatch.setenv("SLOT_AWARE", "1")
    monkeypatch.setenv("NVTOP_TIMEOUT_SEC", "0.1")
    monkeypatch.setenv("NVTOP_KILL_WAIT_SEC", "3.0")

    monkeypatch.setenv("NVTOP_BIN", str(hanging))
    assert await inference_gpu_busy() is False
    assert gpu_load._consecutive_hangs == 1

    monkeypatch.setenv("NVTOP_BIN", str(healthy))
    assert await inference_gpu_busy() is False  # empty snapshot -> not busy
    assert gpu_load._consecutive_hangs == 0


# ── probe_status(): (d) additive telemetry, pure module state ────────────────

def test_probe_status_unavailable_under_slot_aware_off(monkeypatch):
    monkeypatch.setenv("SLOT_AWARE", "0")
    assert gpu_load.probe_status()["state"] == "unavailable"


def test_probe_status_unavailable_when_nvtop_missing(monkeypatch):
    monkeypatch.setenv("SLOT_AWARE", "1")
    monkeypatch.setenv("NVTOP_BIN", "definitely-not-a-real-binary-xyz")
    assert gpu_load.probe_status()["state"] == "unavailable"


def test_probe_status_ok_when_installed_and_armed(monkeypatch, tmp_path):
    fake_nvtop = _write_fake_nvtop(tmp_path, "echo '[]'")
    monkeypatch.setenv("SLOT_AWARE", "1")
    monkeypatch.setenv("NVTOP_BIN", str(fake_nvtop))
    assert gpu_load.probe_status()["state"] == "ok"


# ── (f) the timeout log line names the exception class, never an empty () ────

@pytest.mark.asyncio
async def test_timeout_log_names_the_exception_class(monkeypatch, tmp_path, caplog):
    """Regression for the bug this release fixes: the OLD handler logged
    `"nvtop snapshot failed ()"` on a timeout -- str(TimeoutError()) is empty,
    so the line named nothing. The new timeout-specific line must carry the
    exception class name."""
    import logging

    fake_nvtop = _write_fake_nvtop(tmp_path, "exec sleep 5")
    monkeypatch.setenv("SLOT_AWARE", "1")
    monkeypatch.setenv("NVTOP_BIN", str(fake_nvtop))
    monkeypatch.setenv("NVTOP_TIMEOUT_SEC", "0.1")
    monkeypatch.setenv("NVTOP_KILL_WAIT_SEC", "3.0")

    with caplog.at_level(logging.WARNING, logger="gpu_load"):
        await inference_gpu_busy()

    messages = [r.getMessage() for r in caplog.records]
    assert any("TimeoutError" in m for m in messages)
    assert not any(m.strip().endswith("()") for m in messages)


# ── Fix round (merger rulings on the two post-build reviews) ─────────────────

@pytest.mark.asyncio
async def test_create_subprocess_raising_timeout_directly_returns_false(monkeypatch):
    """F1 (MEDIUM): create_subprocess_exec itself can raise asyncio.TimeoutError
    on a genuine race, BEFORE `proc` is ever assigned. Before this fix `proc`
    was only ever bound inside the try, so a TimeoutError from
    create_subprocess_exec itself left it unbound -- an UnboundLocalError
    would escape inference_gpu_busy() and stale the whole /health snapshot in
    the refresher (that failure mode is exactly what fact:1645's original bug
    already showed the cost of: a probe that raises instead of returning
    False). The fix is two parts: `proc = None` before the try (so the name
    is always bound), and the `if proc is not None:` guard asserted here (so
    a None proc is never handed to _reap_after_timeout, which expects a real
    process object).

    A bare "does it raise" test cannot tell the guard apart from the fix's
    other half: with `proc = None` alone, calling
    _reap_after_timeout(None, ...) without the guard raises AttributeError
    inside that function's OWN outer `except Exception`, which swallows it --
    so the call still returns False without raising, even with the guard
    removed. This test instead spies on _reap_after_timeout to prove it is
    never invoked at all when proc never got assigned.

    Mutation: remove the `if proc is not None:` guard around the
    _reap_after_timeout call in inference_gpu_busy's TimeoutError except
    clause -- this test dies (the spy IS called, with proc=None). Verified on
    a scratch copy -- see HANDOFF.md."""
    monkeypatch.setenv("SLOT_AWARE", "1")
    monkeypatch.setenv("NVTOP_BIN", "nvtop")
    monkeypatch.setattr(gpu_load.shutil, "which", lambda _bin: "/usr/bin/nvtop")

    async def _raise_timeout(*args, **kwargs):
        raise asyncio.TimeoutError

    monkeypatch.setattr(gpu_load.asyncio, "create_subprocess_exec", _raise_timeout)

    reap_calls = []

    async def _spy_reap(proc, timeout, exc):
        reap_calls.append(proc)

    monkeypatch.setattr(gpu_load, "_reap_after_timeout", _spy_reap)

    result = await inference_gpu_busy()  # must not raise

    assert result is False
    assert reap_calls == []  # never called when proc was never assigned


@pytest.mark.asyncio
async def test_cancellation_mid_snapshot_best_effort_kills_child(monkeypatch, tmp_path):
    """F2 (MEDIUM): asyncio.CancelledError is a BaseException, so neither
    except clause in inference_gpu_busy catches it -- a task cancellation
    (e.g. gateway shutdown) mid create_subprocess_exec/communicate would
    otherwise leave the nvtop child un-killed. The trailing `finally`'s
    best-effort kill covers this. No hang-streak assertion here: a
    cancellation is not a hang and must not be counted as one.

    Mutation: remove the `finally` block's kill -- this test dies (the child
    is still alive after the cancellation, past the 1s polling window).
    Verified on a scratch copy -- see HANDOFF.md."""
    fake_nvtop = _write_fake_nvtop(tmp_path, "exec sleep 5")
    monkeypatch.setenv("SLOT_AWARE", "1")
    monkeypatch.setenv("NVTOP_BIN", str(fake_nvtop))
    # Long enough that only the cancellation (not the timeout) ends the call.
    monkeypatch.setenv("NVTOP_TIMEOUT_SEC", "5.0")
    calls = _capture_create_subprocess_exec(monkeypatch)

    task = asyncio.create_task(inference_gpu_busy())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(calls) == 1
    proc = calls[0]

    # SIGKILL is sent synchronously in the `finally`, but the kernel's own
    # bookkeeping (and pytest-asyncio's loop) is not synchronous with that
    # call returning -- allow a short polling wait for the OS to catch up.
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 1.0
    while loop.time() < deadline:
        try:
            os.kill(proc.pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("child pid still answers os.kill(pid, 0) after a 1s wait")


@pytest.mark.asyncio
async def test_bad_env_value_falls_back_to_default_and_still_reaps(monkeypatch, tmp_path):
    """F6 (Gemini): a non-numeric NVTOP_KILL_WAIT_SEC used to raise ValueError
    INSIDE _reap_after_timeout's own defensive try, get swallowed by its
    `except Exception`, and abort the reap BEFORE _consecutive_hangs was
    incremented -- silently defeating rule 2's self-disable cap for as long
    as the operator's typo stood. The new _env_float/_env_int helpers parse
    before that try and fall back to the default instead.

    Mutation: move the kill_wait/max_hangs parsing back inside the reap's
    try block (i.e. revert to raw float()/int() calls there) -- this test
    dies (_consecutive_hangs stays 0, the child is left un-reaped). Verified
    on a scratch copy -- see HANDOFF.md."""
    fake_nvtop = _write_fake_nvtop(tmp_path, "exec sleep 5")
    monkeypatch.setenv("SLOT_AWARE", "1")
    monkeypatch.setenv("NVTOP_BIN", str(fake_nvtop))
    monkeypatch.setenv("NVTOP_TIMEOUT_SEC", "0.1")
    monkeypatch.setenv("NVTOP_KILL_WAIT_SEC", "not-a-number")
    calls = _capture_create_subprocess_exec(monkeypatch)

    result = await inference_gpu_busy()

    assert result is False
    assert gpu_load._consecutive_hangs == 1  # the increment this bug used to skip
    proc = calls[0]
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)
    assert proc.returncode == -9
