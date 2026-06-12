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
from pathlib import Path

FILE_MODE = 0o600
DIR_MODE  = 0o700


def secure_path(path: str) -> str:
    """Ensure the parent dir (0700) and file (0600) exist with safe perms.

    Idempotent; tightens an existing world-readable file to 0600. Returns the
    user-expanded absolute-ish path so callers can open it directly.
    """
    p = Path(os.path.expanduser(path))
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p.parent, DIR_MODE)
    except OSError:
        pass
    if not p.exists():
        # O_CREAT honours the mode arg (subject to umask); chmod after to be exact.
        os.close(os.open(p, os.O_CREAT | os.O_APPEND | os.O_WRONLY, FILE_MODE))
    try:
        os.chmod(p, FILE_MODE)
    except OSError:
        pass
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
    loop. A bounded queue with drop-oldest means a slow disk can neither stall
    the loop nor grow memory without bound. One drain task serialises writes, so
    lines never interleave. With no running loop (sync callers / tests),
    ``write()`` falls back to a direct secured append.
    """

    def __init__(self, path: str, *, maxsize: int = 10000) -> None:
        self.path = path
        self._q: asyncio.Queue[str] = asyncio.Queue(maxsize=maxsize)
        self._task: asyncio.Task | None = None
        self.dropped = 0

    def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain(), name="audit-log-writer")

    def write(self, line: str) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            append_secure(self.path, line)   # no loop (sync caller/test) → inline
            return
        self._ensure_task()
        try:
            self._q.put_nowait(line)
        except asyncio.QueueFull:
            try:
                self._q.get_nowait()          # drop oldest, keep the newest
                self._q.put_nowait(line)
            except Exception:
                pass
            self.dropped += 1

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
