"""Unit tests for the cross-architecture GPU/inference load probe (gpu_load.py).

Covers the pure decision function over an `nvtop -s` snapshot plus the fail-open
branches of the async probe — no GPU, no nvtop, and no subprocess required.
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


# ── parse_busy: process auto-detection ───────────────────────────────────────

def test_inference_gpu_above_threshold_is_busy():
    snap = [_gpu("80%", ["/home/x/.lmstudio/.internal/utils/llmworker.js"])]
    assert parse_busy(snap, proc_match="lmstudio|llmworker", threshold=50) is True


def test_inference_gpu_below_threshold_is_idle():
    snap = [_gpu("12%", ["lmstudio llmworker.js"])]
    assert parse_busy(snap, proc_match="lmstudio|llmworker", threshold=50) is False


def test_busy_gpu_without_inference_process_is_ignored():
    # A GPU pegged at 100% by the desktop/browser must NOT count as inference busy.
    snap = [_gpu("100%", ["/opt/google/chrome/chrome --type=gpu-process"])]
    assert parse_busy(snap, proc_match="lmstudio|llmworker|llama", threshold=50) is False


def test_only_inference_hosting_gpu_is_gated():
    # GPU 0 runs the desktop hot; GPU 1 runs LM Studio but is idle → not busy.
    snap = [
        _gpu("95%", ["chrome --type=gpu-process"]),
        _gpu("5%", ["llmworker.js"]),
    ]
    assert parse_busy(snap, proc_match="llmworker", threshold=50) is False


def test_inference_busy_on_second_gpu():
    snap = [
        _gpu("0%", ["chrome"]),
        _gpu("88%", ["llama-server -m model.gguf"]),
    ]
    assert parse_busy(snap, proc_match="lmstudio|llmworker|llama", threshold=50) is True


# ── parse_busy: explicit GPU indices override ────────────────────────────────

def test_explicit_indices_override_process_match():
    snap = [_gpu("90%", ["chrome"]), _gpu("10%", ["llmworker"])]
    # Gate on GPU 0 regardless of which GPU runs inference.
    assert parse_busy(snap, gpu_indices=[0], threshold=50) is True
    assert parse_busy(snap, gpu_indices=[1], threshold=50) is False


def test_out_of_range_index_is_skipped():
    snap = [_gpu("90%", ["llmworker"])]
    assert parse_busy(snap, gpu_indices=[5], threshold=50) is False


# ── parse_busy: malformed / edge input ───────────────────────────────────────

def test_non_list_snapshot_is_not_busy():
    assert parse_busy({"oops": 1}, proc_match="llama", threshold=50) is False


def test_threshold_boundary_is_inclusive():
    snap = [_gpu("50%", ["llmworker"])]
    assert parse_busy(snap, proc_match="llmworker", threshold=50) is True


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
