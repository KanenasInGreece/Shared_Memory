"""
Server-side log-file hygiene for the framework.

One place that enforces safe permissions (0600 file / 0700 parent dir) on every
framework-created log, and provides an off-event-loop line writer so async
callers (the gateway audit log) never block the loop on disk I/O.

Rotation + gzip are handled by system **logrotate(8)** (see
``ops/shared-memory.logrotate`` + the ``shared-memory-logrotate.timer`` user
unit) — this module deliberately does NOT rotate, so the two never fight over
the same file. The writers open-append-close per line (no persistent handle),
so logrotate ``create`` mode is clean: the next write reopens the fresh file.

Thin-client note: ``memory_bridge.py`` / ``vector-skill.py`` ship alone and do
NOT import this; they set 0600 inline and their per-tool logs are rotated by
``consolidation_loop.merge_logs()``. This module is server-side only
(coordinator, rem_loop).
"""
from __future__ import annotations

import asyncio
import os
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

FILE_MODE = 0o600
DIR_MODE  = 0o700


def _chmod_created_ancestors(dir_path: Path) -> None:
    """mkdir(parents=True) + DIR_MODE on every ancestor this call actually
    CREATES — not just the immediate parent. A relocation more than one
    level deep (e.g. CREDENTIAL_AUDIT_LOG_PATH under a fresh multi-segment
    path) previously left every level above the immediate parent at the
    process umask (typically 0755, world-traversable)."""
    created = []
    cur = dir_path
    while not cur.exists():
        created.append(cur)
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    dir_path.mkdir(parents=True, exist_ok=True)
    for d in created:
        try:
            os.chmod(d, DIR_MODE)
        except OSError:
            pass


def secure_path(path: str) -> str:
    """Ensure the parent dir chain (0700, every level this call creates) and
    the file (0600) exist with safe perms. Idempotent; tightens an existing
    world-readable file to 0600. Returns the user-expanded absolute-ish path
    so callers can open it directly.

    Refuses a symlink at the final path component (O_NOFOLLOW) rather than
    opening whatever it points at: a deployer who relocates the log via
    CREDENTIAL_AUDIT_LOG_PATH/GATEWAY_AUDIT_LOG_PATH into a shared directory
    (/tmp, /var/log) must not let a different-uid actor pre-plant a symlink
    and get this process to append-and-0600 a file of their own choosing.
    Raises OSError (ELOOP) in that case — the caller (AsyncLineWriter, or its
    sync fallback) already treats a write failure as best-effort and never
    lets it break a request.

    Permissions are set via the OPEN FILE DESCRIPTOR (os.fchmod), not a
    separate chmod-by-path call, so there is no TOCTOU window between
    "created/opened" and "permissions applied" for an attacker to race.
    """
    p = Path(os.path.expanduser(path))
    _chmod_created_ancestors(p.parent)
    flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW
    fd = os.open(p, flags, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)  # tightens a pre-existing world-readable file too
    finally:
        os.close(fd)
    return str(p)


def append_secure(path: str, line: str) -> None:
    """Append one line to a permission-secured log file.

    Synchronous and safe to run inside a thread executor. Rotation is
    logrotate's job — this only secures perms and writes.
    """
    real = secure_path(path)
    with open(real, "a", encoding="utf-8") as fh:
        fh.write(line if line.endswith("\n") else line + "\n")


class AsyncLineWriter:
    """Off-event-loop line writer for async callers (the gateway audit log).

    ``write()`` is non-blocking: it enqueues (O(1)) and a single drain task does
    the actual append in a thread executor, so disk I/O never runs on the event
    loop. A bounded queue means a slow disk can neither stall the loop nor grow
    memory without bound. One drain task serialises writes, so lines never
    interleave. With no running loop (sync callers / tests), ``write()`` falls
    back to a direct secured append.

    Eviction policy is PER LINE, not per writer (security review R-1 fix
    round on PR A3, finding R-5): the default is drop-OLDEST, which is right
    for a lifecycle event a caller can only produce by doing something real
    (a daemon boot, a genuine upstream rejection). For an event an
    UNAUTHENTICATED caller can trigger at will (a bad bearer token), drop-
    oldest lets a flood evict the genuine security events that were queued
    before the flood started — pass ``drop_newest_when_full=True`` for that
    class of line instead, so a flood can only ever evict itself.
    """

    def __init__(self, path: str, *, maxsize: int = 10000) -> None:
        self.path = path
        self._q: asyncio.Queue[str] = asyncio.Queue(maxsize=maxsize)
        self._task: asyncio.Task | None = None
        self.dropped = 0
        # When the most recent drop happened. `dropped` alone answers "have we
        # lost audit lines?" but not "is this still happening?" — and the count
        # resets with the process, so a consumer cannot recover the timing by
        # diffing polls (a restart makes the delta read as "never dropped").
        # Stamped at the drop site for the same reason the insight census pairs
        # its age with the row that produced it: an age derived somewhere other
        # than where the event happened goes stale silently.
        self.last_dropped_ts: str | None = None

    def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain(), name="audit-log-writer")

    def write(self, line: str, *, drop_newest_when_full: bool = False) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            append_secure(self.path, line)   # no loop (sync caller/test) → inline
            return
        self._ensure_task()
        try:
            self._q.put_nowait(line)
        except asyncio.QueueFull:
            if drop_newest_when_full:
                self.dropped += 1  # THIS line never enqueues — the queue is untouched
                self.last_dropped_ts = datetime.now(timezone.utc).isoformat()
                return
            try:
                self._q.get_nowait()          # drop oldest, keep the newest
                self._q.put_nowait(line)
            except Exception:
                pass
            self.dropped += 1
            self.last_dropped_ts = datetime.now(timezone.utc).isoformat()

    async def _drain(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            line = await self._q.get()
            try:
                await loop.run_in_executor(None, append_secure, self.path, line)
            except Exception:
                pass
            finally:
                self._q.task_done()

    async def flush(self) -> None:
        """Block until everything enqueued so far is written (tests + shutdown)."""
        await self._q.join()

    async def aclose(self) -> None:
        """Flush, then stop the drain task — call on graceful shutdown."""
        try:
            await self.flush()
        finally:
            if self._task is not None:
                self._task.cancel()
                try:
                    await self._task
                except BaseException:
                    pass
                self._task = None


_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")


def scrub_url_credentials(text: str) -> str:
    """Security review O-6 (widened R3-4): strip userinfo (user:pass@) and the
    query string from any URL found in `text` before it reaches a
    client-visible body or a log. An exception's own __str__ can render the
    full request URL (aiohttp's InvalidURL and httpx's HTTPStatusError both
    do), and a real provider pattern puts a credential in a URL — a
    `?key=...` query parameter, or userinfo — so echoing that text verbatim is
    a key-leakage path. Shared here because BOTH the gateway (LLM backends)
    and the coordinator (its own EMBEDDER_URL / RERANKER_URL calls) render
    such errors; the coordinator cannot import the gateway module.

    Scheme-generic (not http(s)-only): matches ANY `scheme://` prefix,
    including `postgresql://`, `bolt://`, `redis://` DSNs and mixed/upper
    -case schemes (`HTTP://…`). The netloc is rebuilt byte-preservingly —
    everything after the LAST "@" in the ORIGINAL netloc is kept verbatim
    (case, IPv6 brackets, port intact); only the userinfo before that "@" is
    dropped. This deliberately avoids `parsed.hostname` (which lowercases the
    host and strips IPv6 brackets) and `urlunsplit` (which drops one slash of
    a netloc-less DSN like `postgresql:///agent_data`) — reconstruction is
    `f"{scheme}://{netloc}{path}"` directly. Query and fragment are always
    dropped (R-2's render-scrub policy). Idempotent: a second pass over
    already-scrubbed output is a no-op. Only scheme/host/port/path survive;
    never raises (a "URL" urlsplit chokes on is replaced outright, not
    echoed)."""
    def _scrub(m: "re.Match") -> str:
        raw = m.group(0)
        try:
            parsed = urllib.parse.urlsplit(raw)
            netloc = parsed.netloc.rpartition("@")[2]
            return f"{parsed.scheme}://{netloc}{parsed.path}"
        except Exception:
            return "<url-redacted>"
    return _URL_RE.sub(_scrub, text)
