"""Unit tests for the cross-architecture GPU-load probe (gpu_load.py).

Covers the pure decision function over an `nvtop -s` snapshot plus the fail-open
branches of the async probe — no GPU, no nvtop, and no subprocess required.

The probe is platform-agnostic: it gates on raw GPU utilisation, with no
assumption about which process drives the GPU. Any consumer — the local LLM, a
direct chat, an unrelated app — counts as busy, because dreaming should yield to
all of them.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
import gpu_load
from gpu_load import parse_busy, inference_gpu_busy


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
    gpu_load._warned = False  # reset rate-limit so the branch is exercised
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
