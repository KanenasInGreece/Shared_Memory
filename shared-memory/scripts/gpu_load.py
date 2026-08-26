"""Cross-architecture GPU-load probe, surfaced as inference_busy telemetry.

This module used to be a direct gate the sleep-cycle daemons (rem_loop.py,
consolidation_loop.py) checked before dreaming. That direct gate has since been
removed — neither daemon imports this module any more. The only remaining
caller is coordinator.py::_consolidation_health_refresher, which polls
inference_busy_state() every CONSOLIDATION_HEALTH_REFRESH_SEC (~60s) purely to
populate the "inference_busy" telemetry surfaced on /health and
/memory/telemetry (a "GPU is busy — a user chatting with the LLM, or any other
GPU workload" signal for the monitor's LLM tile, not a load-bearing gate on
anything today).

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

A timed-out nvtop child is always reaped (SIGKILL + a short wait) or, failing
that, counted as leaked on every NON-CANCELLATION path (fact:1645: a host
where nvtop blocks in a GPU-fence wait left 926 D-state children, 2.98GB RSS,
over 17h, because the old code returned False on timeout without touching the
child). On CANCELLATION (asyncio.CancelledError, e.g. gateway shutdown mid
snapshot) the child is best-effort killed only — no await, no counting; see
inference_gpu_busy's `finally`. After NVTOP_MAX_CONSECUTIVE_HANGS consecutive
snapshot timeouts (any completed snapshot, even a malformed one, resets the
count), the probe disables itself for the rest of the process lifetime rather
than keep spawning children that are likely to hang the same way — permanent
until the gateway restarts.

Runs on the gateway host (coordinator.py), which is where inference actually
gets served, including generation that remote clients route through the
gateway. nvtop is an infrastructure-host prerequisite, not a remote-client one.
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
#   SLOT_AWARE=0                    disable GPU-aware dreaming entirely
#   NVTOP_BIN=nvtop                 path/name of the nvtop binary
#   GPU_BUSY_PERCENT=50             a gated GPU at/above this util (%) counts as busy
#   GPU_INDICES=0,1                 gate only these GPU positions; default is every GPU
#   NVTOP_TIMEOUT_SEC=5.0           snapshot subprocess timeout
#   NVTOP_KILL_WAIT_SEC=1.0         how long to wait for a SIGKILLed nvtop child to
#                                   actually exit before counting it as leaked
#                                   (D-state, unreapable)
#   NVTOP_MAX_CONSECUTIVE_HANGS=3   consecutive snapshot timeouts (any completed
#                                   snapshot, even a malformed one, resets the
#                                   count) before the probe disables itself for
#                                   the process lifetime (UNMEASURED default
#                                   — see fact:1645)
DEFAULT_GPU_BUSY_PERCENT = 50

_warned = False  # rate-limit the "nvtop unavailable" warning to once per process
_consecutive_hangs = 0  # snapshot timeouts in a row; reset on any successful snapshot
_leaked_children = 0  # nvtop children still in D state after SIGKILL+wait; monotonic
                      # for the process lifetime (reset-on-restart, like the rerank
                      # counters) — never awaited again once counted here
_disabled_reason = None  # None while armed; "disabled_after_hangs" once tripped by
                         # rule 2 — permanent until the gateway restarts (no re-arm)
_env_parse_warned: set = set()  # var names already warned about a bad value, once each


def _env_float(name: str, default: float) -> float:
    """Read a float env var, falling back to `default` (with a once-per-name
    WARNING) on anything that doesn't parse. F6 (fix round, Gemini): the raw
    `float(os.environ.get(...))` calls this replaces used to live INSIDE
    _reap_after_timeout's own try/except -- a non-numeric operator value threw
    ValueError, the defensive except swallowed it, and the whole reap aborted
    BEFORE _consecutive_hangs was ever incremented, so the self-disable in
    rule 2 could never trip. Parsing here, before that try block, keeps a
    typo'd knob from silently defeating the cap it's supposed to bound."""
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


async def _reap_after_timeout(proc, timeout: float, exc: Exception) -> None:
    """Reap (or count as leaked) an nvtop child that blew NVTOP_TIMEOUT_SEC, and
    advance the hang-streak counter rule 2's self-disable keys on.

    Wrapped so NOTHING escapes this function: a probe that cannot be reaped must
    never blank the whole /health snapshot — inference_gpu_busy() always returns
    False on this path regardless of what happens in here.
    """
    global _leaked_children, _consecutive_hangs, _disabled_reason
    # F6 (fix round, Gemini): parsed OUTSIDE the try below, via helpers that
    # never raise. A raw float()/int() in here used to be able to throw on a
    # malformed operator value, get swallowed by the try's own defensive
    # except, and abort the reap BEFORE _consecutive_hangs was incremented —
    # silently defeating rule 2's cap.
    kill_wait = _env_float("NVTOP_KILL_WAIT_SEC", 1.0)
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
            # Still unresponsive after SIGKILL — D state (GPU-fence wait). This
            # is an OS-level fact, not something in-process retains: proc and
            # its transport are local objects that get garbage-collected
            # normally, and the kernel process table entry simply stays in D
            # state until the machine reboots. Count it and never await it
            # again -- rule 2's cap on the hang streak bounds how many are
            # ever spawned in the first place. Read-then-write, no await in
            # between: the capability-probe task shares this module.
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
                "timeouts (any completed snapshot, even a malformed one, resets "
                "the count) (NVTOP_TIMEOUT_SEC="
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
    """Async probe: True if a gated GPU is busy at/above GPU_BUSY_PERCENT.

    Fail-open: returns False when SLOT_AWARE is disabled, nvtop is unavailable,
    the snapshot times out, or the probe has disabled itself after repeated
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
        # Rule 2: no-recovery self-disable. Never spawn another child this
        # process once the hang streak has tripped the cap.
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
    proc = None  # F1 (fix round): must exist before the try -- create_subprocess_
                 # exec itself raising (a missing-binary race) left this name
                 # unbound, and an UnboundLocalError out of the except below used
                 # to escape to the refresher and stale the whole /health snapshot.
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
        # cause: a GPU-fence D-state wait, fact:1645). It must be reaped or
        # counted — never left to leak silently. proc can still be None here
        # (see F1 above) if create_subprocess_exec itself is what timed out;
        # there is nothing to reap in that case.
        if proc is not None:
            await _reap_after_timeout(proc, timeout, exc)
        return False
    except Exception as exc:  # missing binary race, malformed JSON, etc. — the
        # process has already exited on this path, so no kill/wait is needed.
        if not _warned:
            log.warning("nvtop snapshot failed (%s: %s) — GPU-aware dreaming disabled this cycle.",
                        type(exc).__name__, exc)
            _warned = True
        # OPERATOR RULING (decision:1656 follow-up): the child ran and exited
        # on this path (it just produced something the probe couldn't parse,
        # or lost a missing-binary race) — that's evidence against a hang, so
        # it resets the streak exactly like a successful snapshot does.
        _consecutive_hangs = 0
        return False
    finally:
        # F2 (fix round): asyncio.CancelledError is a BaseException, so it is
        # NOT caught by either except clause above -- a task cancellation
        # (e.g. gateway shutdown) mid create_subprocess_exec/communicate would
        # otherwise leave the child un-killed and silently propagate past this
        # function. Best-effort only: no `await` (an await here would itself
        # be immediately cancelled and re-raise before doing anything) and no
        # counting (a cancellation is not a hang -- inference_gpu_busy() never
        # gets to observe or log about it). proc.returncode is already set
        # once communicate() or _reap_after_timeout's own wait() succeeded, so
        # this is a no-op on every normal exit path (return or exception).
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    # A full successful cycle clears any hang streak — only CONSECUTIVE
    # snapshot timeouts count toward the self-disable in rule 2 (any completed
    # snapshot, even a malformed one, resets the count — the generic `except
    # Exception` branch above resets it the same way, and for the same reason:
    # OPERATOR RULING, decision:1656 follow-up).
    _consecutive_hangs = 0

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

      "busy"    — a gated GPU is at/above GPU_BUSY_PERCENT (same gate REM/NREM
                  used to defer on, so the monitor can show the LLM "Busy" truthfully)
      "idle"    — the probe ran and no gated GPU is busy
      "unknown" — the probe is unavailable (SLOT_AWARE=0, nvtop absent, or the
                  probe has disabled itself after repeated hangs — see
                  probe_status()), so the fail-open False from
                  inference_gpu_busy() must NOT read as "idle"

    This is the read-only surface for the busy signal; inference_gpu_busy() remains
    the boolean the daemons gate on. nvtop sees raw GPU utilisation, so this also
    reflects a user chatting directly with :5000 (which bypasses the gateway)."""
    if not gpu_probe_available():
        return "unknown"
    return "busy" if await inference_gpu_busy() else "idle"
