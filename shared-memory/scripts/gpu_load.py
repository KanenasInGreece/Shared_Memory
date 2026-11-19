"""Cross-architecture GPU-load probe, surfaced as `inference_busy` telemetry.

This is a DISPLAY signal for the monitor's LLM tile ("the GPU is busy": a user
chatting with the LLM, or any other GPU workload); nothing gates on it. Neither
rem_loop.py nor consolidation_loop.py imports this module. The only callers are
coordinator.py (`_consolidation_health_refresher` polls `inference_busy_state()`
and `probe_status()` every CONSOLIDATION_HEALTH_REFRESH_SEC, about 60 s, for
/health and /memory/telemetry) and hive_mind_proxy.py (`gpu_probe_installed()`,
for the hardware fingerprint).

The probe makes **no assumption about what is driving the GPU** (decision:228,
the ruling that the probe reads raw GPU utilisation, not process names): the server platform (LM Studio,
vLLM, llama-server, Ollama, a bare script, ...) is irrelevant, and any consumer,
including a direct chat or an unrelated GPU app, counts as busy.

Driver-specific paths (`nvidia-smi`, `rocm-smi`, i915/xe sysfs `gpu_busy_percent`)
each break on some hardware; Intel Arc, for one, exposes no `gpu_busy_percent`.
So the probe shells out to **nvtop --snapshot**, which abstracts Nvidia (NVML),
AMD and Intel behind one JSON output. nvtop is a prerequisite for this feature.

Fail-open by design, so a broken probe never reads as a busy GPU.
`inference_busy_state` answers "unknown" only when `gpu_probe_available()` is
false BEFORE the call (SLOT_AWARE=0, nvtop absent, or the hang cap already
tripped). A failure DURING the call (timeout, bad JSON, spawn error) makes
`inference_gpu_busy` return False, which `inference_busy_state` reports as "idle"
for that cycle.

A timed-out nvtop child is reaped (SIGKILL plus a short wait) or, failing that,
counted as leaked, on every NON-CANCELLATION path. fact:1645 (measured) is why:
a host where nvtop blocked in a GPU-fence wait left 926 D-state children
(2.98 GB RSS) over 17 h, because the old code returned False on timeout without
touching the child. On CANCELLATION (asyncio.CancelledError, e.g. gateway shutdown
mid snapshot) the child is only best-effort killed, with no await and no
counting; see the `finally` in inference_gpu_busy. A counted leaked child is
never awaited again.

After NVTOP_MAX_CONSECUTIVE_HANGS consecutive snapshot timeouts (any cycle that
ends in anything other than a snapshot timeout resets the count) the probe
disables itself rather than keep spawning children likely to hang the same way
(decision:1656: reap on timeout, then self-disable after N timeouts). That is
permanent until the gateway restarts; there is no re-arm.

The probe runs on the gateway host (coordinator.py), where inference is served,
including generation that remote clients route through the gateway. nvtop is an
infrastructure-host prerequisite, not a remote-client one.
"""
import asyncio
import json
import logging
import os
import shutil

log = logging.getLogger("gpu_load")

# The env knobs are re-read at call time, not import, so a late .env load is
# visible.
DEFAULT_GPU_BUSY_PERCENT = 50

_warned = False  # rate-limit the "nvtop unavailable" warning to once per process
_consecutive_hangs = 0  # snapshot timeouts in a row; reset by any cycle that is not a snapshot timeout
_leaked_children = 0  # nvtop children still in D state after SIGKILL+wait; only
                      # grows until the gateway restarts; never awaited again
_disabled_reason = None  # None while armed; "disabled_after_hangs" once the
                         # hang-cap self-disable trips, permanent until restart
_env_parse_warned: set = set()  # var names already warned about a bad value, once each


def _env_float(name: str, default: float) -> float:
    """Read a float env var, falling back to `default` (with a once-per-name
    WARNING) on anything that doesn't parse. Never raises, so a typo'd knob
    cannot stale the /health snapshot, and _reap_after_timeout calls it BEFORE
    its own try: a parse error inside that try would be swallowed and abort the
    reap before the hang count advanced, defeating the hang-cap self-disable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        if name not in _env_parse_warned:
            log.warning("%s=%r is not a valid number — using the default %s.", name, raw, default)
            _env_parse_warned.add(name)
        return default


def _env_int(name: str, default: int) -> int:
    """Integer counterpart to _env_float() -- same reasoning, same guarantee."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        if name not in _env_parse_warned:
            log.warning("%s=%r is not a valid integer — using the default %s.", name, raw, default)
            _env_parse_warned.add(name)
        return default


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
      * if ``gpu_indices`` is given, select those GPU positions;
      * otherwise select **every** GPU in the snapshot.

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


async def _reap_after_timeout(proc, timeout: float, exc: Exception) -> None:
    """Reap (or count as leaked) an nvtop child that blew NVTOP_TIMEOUT_SEC, and
    advance the hang-streak counter the hang-cap self-disable keys on.

    Wrapped so NOTHING escapes this function: a probe that cannot be reaped must
    never blank the whole /health snapshot — inference_gpu_busy() always returns
    False on this path regardless of what happens in here.
    """
    global _leaked_children, _consecutive_hangs, _disabled_reason
    # Parsed OUTSIDE the try below, by helpers that never raise: an exception in
    # here would be swallowed by the try's defensive except and abort the reap
    # before _consecutive_hangs advanced, silently defeating the hang cap.
    kill_wait = _env_float("NVTOP_KILL_WAIT_SEC", 1.0)
    # Default 3 is UNMEASURED (fact:1338: a parameter value is a measurement
    # claim); fact:1645 measured a 926-child nvtop leak, not this cap.
    max_hangs = _env_int("NVTOP_MAX_CONSECUTIVE_HANGS", 3)
    try:
        try:
            proc.kill()
        except ProcessLookupError:
            # asyncio raises this when the child already exited right at the
            # timeout boundary — the success case for this call.
            pass

        reaped = True
        try:
            await asyncio.wait_for(proc.wait(), timeout=kill_wait)
        except asyncio.TimeoutError:
            # Still in D state after SIGKILL; the kernel keeps it until reboot.
            # Count it and do not await it again. The hang cap does not bound an
            # alternating pattern (decision:1656 follow-up: a non-timeout failure
            # resets the streak).
            reaped = False
            _leaked_children += 1

        _consecutive_hangs += 1

        log.warning(
            "nvtop snapshot timed out (NVTOP_TIMEOUT_SEC=%.1fs, %s) — child %s "
            "(hang streak: %d/%d).",
            timeout, type(exc).__name__,
            "reaped" if reaped else "leaked (D-state, unreapable)",
            _consecutive_hangs, max_hangs,
        )

        if _consecutive_hangs >= max_hangs and _disabled_reason is None:
            _disabled_reason = "disabled_after_hangs"
            log.warning(
                "GPU-aware dreaming DISABLED after %d consecutive nvtop snapshot "
                "timeouts (any cycle that ends in anything other than a "
                "snapshot timeout resets the count) (NVTOP_TIMEOUT_SEC="
                "%.1fs, NVTOP_KILL_WAIT_SEC=%.1fs, NVTOP_MAX_CONSECUTIVE_HANGS="
                "%d, %d child(ren) leaked so far). This is PERMANENT until the "
                "gateway restarts — no automatic re-arm. Investigate the nvtop "
                "hang before restarting, or the same threshold will trip again.",
                _consecutive_hangs, timeout, kill_wait, max_hangs, _leaked_children,
            )
    except Exception as reap_exc:  # pragma: no cover - defensive, must not escape
        log.warning("nvtop timeout-reap itself failed (%s: %s) — some subprocess "
                     "state may be untracked.", type(reap_exc).__name__, reap_exc)


async def inference_gpu_busy() -> bool:
    """Async probe: True if a monitored GPU is busy at/above GPU_BUSY_PERCENT.

    Fail-open: returns False when SLOT_AWARE is disabled, nvtop is unavailable,
    the snapshot times out or fails, or the probe has disabled itself after repeated
    hangs (NVTOP_MAX_CONSECUTIVE_HANGS). Every subprocess this spawns is always
    either reaped or counted as leaked on every NON-CANCELLATION path — see
    _reap_after_timeout. On cancellation (asyncio.CancelledError, a BaseException
    neither except clause below catches) the child is best-effort killed only,
    via the trailing `finally` — no await, no counting.
    """
    global _warned, _consecutive_hangs
    if os.environ.get("SLOT_AWARE", "1") == "0":
        return False
    if _disabled_reason is not None:
        # The hang-cap self-disable (decision:1656: no re-arm). Never spawn another
        # child this process once the hang streak has tripped the cap.
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

    timeout = _env_float("NVTOP_TIMEOUT_SEC", 5.0)
    # proc must exist before the try: create_subprocess_exec itself can raise,
    # and an unbound name in the handlers would escape and stale /health.
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            nvtop_bin, "-s",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        snapshot = json.loads(out.decode("utf-8", "replace"))
    except asyncio.TimeoutError as exc:
        # The nvtop child is still running past NVTOP_TIMEOUT_SEC (observed
        # cause: a GPU-fence D-state wait, fact:1645, the measured 926-child
        # leak). It must be reaped or counted, never left to leak silently. proc
        # is None if create_subprocess_exec itself timed out; nothing to reap.
        if proc is not None:
            await _reap_after_timeout(proc, timeout, exc)
        return False
    except Exception as exc:  # missing binary race, malformed JSON, etc. — no
        # kill/wait is needed here: either the process already exited (a
        # malformed-output or decode-error cycle) or it never started at all
        # (create_subprocess_exec itself raised, e.g. FileNotFoundError).
        if not _warned:
            log.warning("nvtop snapshot failed (%s: %s) — GPU-aware dreaming disabled this cycle.",
                        type(exc).__name__, exc)
            _warned = True
        # A non-timeout failure is not evidence of a persistent hang, so it
        # resets the streak (operator ruling, decision:1656 follow-up: the cap
        # counts only consecutive timeouts).
        if _consecutive_hangs:
            log.debug("non-timeout nvtop failure cleared a hang streak of %d.", _consecutive_hangs)
        _consecutive_hangs = 0
        return False
    finally:
        # CancelledError is a BaseException, so the excepts above miss a shutdown
        # mid-spawn and leave the child. Do not await here; that await would be
        # cancelled too.
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    # A full successful cycle clears the hang streak: only CONSECUTIVE snapshot
    # timeouts count toward the hang-cap self-disable. The generic
    # `except Exception` branch above resets it the same way (operator ruling,
    # decision:1656 follow-up: a non-timeout cycle resets the streak).
    _consecutive_hangs = 0

    threshold = _env_int("GPU_BUSY_PERCENT", DEFAULT_GPU_BUSY_PERCENT)
    gpu_indices_env = os.environ.get("GPU_INDICES", "").strip()
    indices = (
        [int(x) for x in gpu_indices_env.split(",") if x.strip().isdigit()]
        if gpu_indices_env else None
    )
    busy = parse_busy(snapshot, threshold, indices)
    if busy:
        # Debug detail only: nothing defers on this signal, which feeds the
        # `inference_busy` telemetry.
        log.debug("GPU busy (>=%d%%).", threshold)
    return busy


def _probe_installed() -> bool:
    """SLOT_AWARE on AND nvtop on PATH — pure installation check, independent of
    any runtime self-disable. This is what a hardware fingerprint must read
    (via gpu_probe_installed()): a runtime hang-disable is a process-lifetime
    fact about THIS run, not about the hardware, and must never flip a
    PERSISTED capacity fingerprint (that would fire
    gateway_start_fingerprint_mismatch on the next restart)."""
    if os.environ.get("SLOT_AWARE", "1") == "0":
        return False
    return shutil.which(os.environ.get("NVTOP_BIN", "nvtop")) is not None


def gpu_probe_installed() -> bool:
    """Public wrapper around _probe_installed() for hive_mind_proxy's hardware
    fingerprint — installation only ("is nvtop present and enabled"), never
    "can currently answer" (that's gpu_probe_available(), which also reflects
    the runtime self-disable)."""
    return _probe_installed()


def gpu_probe_available() -> bool:
    """True iff the nvtop GPU probe can actually answer RIGHT NOW: installed
    (SLOT_AWARE on AND nvtop on PATH) AND not self-disabled after repeated
    hangs. When False, a "not busy" result means "cannot tell", not "idle" —
    callers surfacing a busy/idle state to telemetry must treat that case as
    unknown."""
    return _probe_installed() and _disabled_reason is None


def probe_status() -> dict:
    """Pure module-state read for /health + coordinator telemetry — never
    shells out, never awaits. Precedence: a runtime self-disable is checked
    FIRST, ahead of "unavailable" (a probe that was installed and later
    disabled itself is a different, more actionable state than one that was
    never installed at all).

    Returns {"state": "ok" | "unavailable" | "disabled_after_hangs",
             "consecutive_hangs": int, "leaked_children": int}.
    """
    if _disabled_reason is not None:
        state = _disabled_reason
    elif not _probe_installed():
        state = "unavailable"
    else:
        state = "ok"
    return {
        "state": state,
        "consecutive_hangs": _consecutive_hangs,
        "leaked_children": _leaked_children,
    }


async def inference_busy_state() -> str:
    """Tri-state view of the inference GPU for /health + /memory/telemetry:

      "busy"    — a monitored GPU is at/above GPU_BUSY_PERCENT (so the monitor can
                  show the LLM "Busy" truthfully)
      "idle"    — the probe ran and no monitored GPU is busy (also a failure
                  DURING the call, e.g. a timeout, for that cycle)
      "unknown" — the probe is unavailable (SLOT_AWARE=0, nvtop absent, or the
                  probe has disabled itself after repeated hangs — see
                  probe_status()), so the fail-open False from
                  inference_gpu_busy() must NOT read as "idle"

    This is the read-only surface for the busy signal; inference_gpu_busy() is the
    boolean probe it is built from. nvtop sees raw GPU utilisation, so this also
    reflects a user chatting directly with :5000 (which bypasses the gateway)."""
    if not gpu_probe_available():
        return "unknown"
    return "busy" if await inference_gpu_busy() else "idle"
