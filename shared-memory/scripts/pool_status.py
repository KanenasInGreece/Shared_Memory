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
import logging
import os

import httpx

POOL_STATUS_URL = os.environ.get("POOL_STATUS_URL", "http://localhost:8888/pool/status")

logger = logging.getLogger("pool_status")
_was_unreachable = False


def _classify(body) -> tuple[str, str | None]:
    """Return the word for a parsed 200 body, plus the reason code if unreachable."""
    if not isinstance(body, dict) or "free_slots" not in body:
        return "unreachable", ("not_a_mapping" if not isinstance(body, dict)
                               else "no_free_slots")
    free_slots = body["free_slots"]
    if type(free_slots) is not int or free_slots < 0:
        return "unreachable", "bad_free_slots"
    backends = body.get("backends")
    if not isinstance(backends, dict) or not backends:
        return "empty", None
    return ("free" if free_slots > 0 else "busy"), None


def _note(answer: str, reason: str | None) -> None:
    """Log once on entering "unreachable" and once on leaving it; never raise.

    The state is updated before logging. The WARNING carries only a reason code and the
    recovery INFO only the answer word; NEVER log the URL, the headers or an
    exception's text, because the request carries a bearer token and the URL may hold
    credentials.
    """
    global _was_unreachable
    entering = answer == "unreachable" and not _was_unreachable
    leaving = answer != "unreachable" and _was_unreachable
    _was_unreachable = answer == "unreachable"
    try:
        if entering:
            logger.warning(
                "pool status unreachable (%s); dream gates fail open", reason
            )
        elif leaving:
            logger.info("pool status answering again (%s)", answer)
    except Exception:
        pass


async def pool_roster_state(timeout: float = 3.0, headers: dict | None = None) -> str:
    """Return "unreachable", "empty", "busy" or "free" for the gateway's pool.

    Applied in this order: "unreachable" is any non-200, any error, a body that is not a
    mapping, or a `free_slots` that is missing or not a non-negative integer. An
    anonymous caller on an auth-configured gateway gets a body without `free_slots`, so
    it must never read as "empty"; `headers` lets the daemons send their token.
    "empty": `backends` is not a non-empty mapping.
    "busy": `free_slots` is 0.
    "free": `free_slots` is above 0; the gateway counts available backends in it.
    The helper logs one WARNING with a reason code when it enters "unreachable" and one
    INFO when it next answers anything else. `timeout` (default 3.0 s, unmeasured)
    bounds the connect, read, write and pool waits, so it only protects a daemon from
    a gateway that hangs instead of answering.
    """
    answer, reason = "unreachable", "error"
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            r = await client.get(POOL_STATUS_URL, headers=headers or {})
            if r.status_code != 200:
                reason = f"http_{r.status_code}"
            else:
                try:
                    body = r.json()
                except ValueError:
                    reason = "bad_json"
                else:
                    answer, reason = _classify(body)
    except httpx.TimeoutException:
        answer, reason = "unreachable", "timeout"
    except Exception:
        answer, reason = "unreachable", "error"
    _note(answer, reason)
    return answer
