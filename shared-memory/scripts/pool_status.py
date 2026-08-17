"""Client helper: is the reasoning-LLM pool available?

The dreaming daemons (REM/NREM) gate on this instead of a global nvtop GPU-busy
check. Rationale (measured): a GLOBAL "any GPU busy" gate SELF-DEFERS to the
daemons' own dream LLM calls and, being global, ignores a free card — on a
multi-backend pool that starves the dream cycle (measured 482 NREM + 487 REM
defers / 45 min). Because ALL LLM traffic (dream cycles AND user chats) flows
through the gateway, per-backend in-flight IS the LLM-usage signal: if a backend
is free the gateway can route a dream call to a card the user isn't LLM-loading,
so proceed; if every backend is in-flight/cooling, defer so dream work doesn't
queue behind (and delay) a user chat. Desktop/display GPU use is intentionally
ignored — memory is already reserved for it.

Fail-OPEN: if the gateway can't be reached, assume available so dreaming is never
permanently blocked (matches the old nvtop probe's fail-open contract).

SEC-A5-01 (PR A5 fix round): /pool/status now gates its roster/pool-state
detail behind an agent bearer token on an auth-configured install (same
split as /health). This function's own `free_slots` read still degrades
safely without one — an anonymous call gets `{}`, and `.get("free_slots", 1)`
fail-opens exactly like an unreachable gateway — but a daemon that always
relies on that default has silently traded real slot-awareness for a
constant "always free" answer, which is a worse defect than the disclosure
this gate closes. `headers` lets every real caller (rem_loop.py,
consolidation_loop.py) attach its own resolved AGENT_TOKEN so it always
gets the genuine, current signal instead.
"""
import os

import httpx

POOL_STATUS_URL = os.environ.get("POOL_STATUS_URL", "http://localhost:8888/pool/status")


async def pool_has_free_slot(timeout: float = 3.0, headers: dict | None = None) -> bool:
    """True if at least one reasoning-LLM backend is free (or on any error).
    `headers` should carry the caller's own Authorization (see module
    docstring) — omitted only by a standalone/debug caller with no token."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(POOL_STATUS_URL, headers=headers or {})
            if r.status_code != 200:
                return True
            return int(r.json().get("free_slots", 1)) > 0
    except Exception:
        return True
