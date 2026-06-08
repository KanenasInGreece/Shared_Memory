"""Cross-architecture GPU-load probe for REM & NREM dreaming.

The sleep-cycle daemons (rem_loop.py, consolidation_loop.py) drive the LLM at
:5000 to enrich and synthesise memory. When the GPU is already busy — a user
chatting with the local LLM, or any other GPU workload — dreaming should yield
so it does not compete for the hardware.

We deliberately make **no assumption about what is driving the GPU**. The only
contract is that :5000 serves an OpenAI-compatible completions endpoint; the
server platform (LM Studio, vLLM, llama-server, Ollama, TGI, a bare script, …)
is irrelevant. So the gate is purely "is the GPU busy", not "is a process whose
name we recognise busy" — the latter both coupled us to specific platforms and
ignored direct chats and unrelated GPU apps, which are exactly what we want to
yield to.

Detecting GPU utilisation portably is the hard part: driver-specific paths
(`nvidia-smi`, `rocm-smi`, i915/xe sysfs `gpu_busy_percent`) each break on some
hardware — e.g. Intel Arc exposes no `gpu_busy_percent`. We therefore shell out
to **nvtop --snapshot**, which already abstracts Nvidia (NVML), AMD and Intel
behind one JSON output. nvtop is a prerequisite for this feature.

Fail-open by design: if nvtop is absent, times out, or returns unparseable
output, the probe reports *not busy* so dreaming is never permanently blocked —
the daemons still honour their WRITE_QUIESCE_SEC time-guard, and NREM's hard
backstop always fires regardless of GPU state.

Runs wherever REM/NREM run — the infrastructure host — so it measures the GPU
that actually serves inference, including generation that remote clients route
through the gateway. nvtop is an infrastructure-host prerequisite, not a
remote-client one.
"""
import asyncio
import json
import logging
import os
import shutil

log = logging.getLogger("gpu_load")

# Defaults for the tunable knobs. The live probe re-reads os.environ at CALL time
# (not import time) so daemons that load .env after importing this module — and
# tests that monkeypatch env — see the right values. Documented env vars:
#   SLOT_AWARE=0           disable GPU-aware dreaming entirely
#   NVTOP_BIN=nvtop        path/name of the nvtop binary
#   GPU_BUSY_PERCENT=50    a gated GPU at/above this util (%) counts as busy
#   GPU_INDICES=0,1        gate only these GPU positions; default is every GPU
#   NVTOP_TIMEOUT_SEC=5.0  snapshot subprocess timeout
DEFAULT_GPU_BUSY_PERCENT = 50

_warned = False  # rate-limit the "nvtop unavailable" warning to once per process


def _pct(value) -> int:
    """Parse nvtop's '52%' (or None) into an int percentage; 0 on anything odd."""
    if not value:
        return 0
    try:
        return int(str(value).replace("%", "").strip() or 0)
    except (ValueError, TypeError):
        return 0


def parse_busy(snapshot, threshold=DEFAULT_GPU_BUSY_PERCENT, gpu_indices=None) -> bool:
    """Pure decision function over a parsed `nvtop -s` snapshot (list of GPU dicts).

    GPU selection:
      * if ``gpu_indices`` is given, gate on those GPU positions;
      * otherwise gate on **every** GPU in the snapshot.

    Returns True iff a gated GPU's ``gpu_util`` is >= ``threshold``. The signal is
    platform-agnostic: any GPU consumer (the local LLM, a direct chat, an
    unrelated app) counts — we yield to all of them, not just recognised servers.
    """
    if not isinstance(snapshot, list):
        return False
    if gpu_indices:
        selected = [snapshot[i] for i in gpu_indices if 0 <= i < len(snapshot)]
    else:
        selected = snapshot
    return any(_pct(g.get("gpu_util")) >= threshold for g in selected)


async def inference_gpu_busy() -> bool:
    """Async probe: True if a gated GPU is busy at/above GPU_BUSY_PERCENT.

    Fail-open: returns False when SLOT_AWARE is disabled or nvtop is unavailable.
    """
    global _warned
    if os.environ.get("SLOT_AWARE", "1") == "0":
        return False
    nvtop_bin = os.environ.get("NVTOP_BIN", "nvtop")
    if shutil.which(nvtop_bin) is None:
        if not _warned:
            log.warning(
                "SLOT_AWARE is on but %r is not installed — GPU-aware dreaming "
                "disabled (install nvtop, or set SLOT_AWARE=0). Falling back to "
                "the WRITE_QUIESCE_SEC time-guard only.", nvtop_bin,
            )
            _warned = True
        return False

    timeout = float(os.environ.get("NVTOP_TIMEOUT_SEC", "5.0"))
    try:
        proc = await asyncio.create_subprocess_exec(
            nvtop_bin, "-s",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        snapshot = json.loads(out.decode("utf-8", "replace"))
    except Exception as exc:  # missing binary race, timeout, malformed JSON, etc.
        if not _warned:
            log.warning("nvtop snapshot failed (%s) — GPU-aware dreaming disabled this cycle.", exc)
            _warned = True
        return False

    threshold = int(os.environ.get("GPU_BUSY_PERCENT", str(DEFAULT_GPU_BUSY_PERCENT)))
    gpu_indices_env = os.environ.get("GPU_INDICES", "").strip()
    indices = (
        [int(x) for x in gpu_indices_env.split(",") if x.strip().isdigit()]
        if gpu_indices_env else None
    )
    busy = parse_busy(snapshot, threshold, indices)
    if busy:
        # Detail at debug; the deferral itself is logged at WARNING by the caller
        # (REM/NREM) so the warning names which dreaming cycle was deferred.
        log.debug("GPU busy (>=%d%%).", threshold)
    return busy
