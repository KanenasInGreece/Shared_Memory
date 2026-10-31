"""Client helper: is the reasoning-LLM pool available?

The dreaming daemons (REM/NREM) gate on this instead of a global nvtop GPU-busy
check. Rationale (measured): a GLOBAL "any GPU busy" gate SELF-DEFERS to the
daemons' own dream LLM calls and, being global, ignores a free card — on a
multi-backend pool that starves the dream cycle (measured 482 NREM + 487 REM
defers / 45 min). Because ALL LLM traffic (dream cycles AND user chats) flows
through the gateway. The chat route serves the framework's daemons; other
clients call a model directly. Per-backend in-flight is the usage signal: a
free backend can take a dream call, and a fleet that is entirely in flight or
cooling is busy. An empty roster is not busy. Desktop GPU use is ignored.

Fail-OPEN: if the gateway can't be reached, assume available so dreaming is never
permanently blocked (matches the old nvtop probe's fail-open contract).

SEC-A5-01 (PR A5 fix round): /pool/status gates its roster behind an agent
bearer token on an auth-configured install (same split as /health). An
anonymous body has no free_slots key. pool_roster_state treats that as
unreachable, the same as a non-200, so a daemon that omits its token does
not read an empty roster as free. `headers` lets rem_loop.py and
consolidation_loop.py attach the daemon's AGENT_TOKEN.
"""
import os

import httpx

POOL_STATUS_URL = os.environ.get("POOL_STATUS_URL", "http://localhost:8888/pool/status")


async def pool_roster_state(timeout: float = 3.0, headers: dict | None = None) -> str:
    """empty, busy, free, or unreachable. Unreachable fail-opens; empty is not busy.

    An anonymous body has no free_slots key and is unreachable, the same as a non-200. A 200 with no backends is an empty roster.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            r = await client.get(POOL_STATUS_URL, headers=headers or {})
            if r.status_code != 200:
                return "unreachable"
            body = r.json()
            if not isinstance(body, dict) or "free_slots" not in body:
                return "unreachable"
            backends = body.get("backends")
            if not isinstance(backends, dict) or not backends:
                return "empty"
            if int(body.get("free_slots") or 0) > 0:
                return "free"
            return "busy"
    except Exception:
        return "unreachable"
