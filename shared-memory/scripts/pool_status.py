"""Client helper: does the gateway's LLM pool have a free backend?

The REM and NREM dreaming daemons ask this before a dream cycle instead of a global
GPU-busy check, which deferred to their own dream calls and ignored a free card (one
measured run: 482 NREM + 487 REM defers in 45 min). The daemons reach their model
through the gateway, so its pool signal counts their traffic. A backend is available
when its in-flight count is below its declared maximum, it is not cooling down after
failures, and it is not reserved. An empty roster is not busy. Desktop GPU use is
ignored.

Fail-OPEN: when the roster cannot be learned the answer is "unreachable", and callers
go on, so dreaming is never blocked by a failed probe.
"""
import os

import httpx

POOL_STATUS_URL = os.environ.get("POOL_STATUS_URL", "http://localhost:8888/pool/status")


async def pool_roster_state(timeout: float = 3.0, headers: dict | None = None) -> str:
    """Return "unreachable", "empty", "busy" or "free" for the gateway's pool.

    "unreachable": any non-200, any error, or a body that is not a mapping or has no
    `free_slots`. An anonymous caller on an auth-configured gateway gets such a body,
    so it must never read as "empty"; `headers` lets the daemons send their token.
    "empty": `free_slots` is present but `backends` is not a non-empty mapping.
    "busy": `backends` is a non-empty mapping and `free_slots` is not above 0.
    "free": `backends` is a non-empty mapping and `free_slots` is above 0; the gateway
    counts available backends in it.
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
