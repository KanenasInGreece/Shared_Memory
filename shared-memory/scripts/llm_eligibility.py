"""Pure backlog eligibility. No database driver.

The gateway imports this module. llm_backlog re-exports the same functions
and is the only place that opens Postgres.
"""
import os

POOL_REASONS = frozenset({"loop", "unparseable", "unrecognised"})
BOOT_REASONS = frozenset({"connection_lost", "write_failed", "rewrite_failed"})
KNOWN_REASONS = frozenset({"no_context", "cut_off"}) | POOL_REASONS | BOOT_REASONS


def identity() -> dict:
    """Spawn values the gateway put on this process."""
    def _int(name: str) -> int:
        try:
            return int(os.environ.get(name) or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "pool_id": os.environ.get("SM_LLM_POOL_ID") or "",
        "largest_ctx": _int("SM_LLM_LARGEST_CTX"),
        "largest_reply": _int("SM_LLM_LARGEST_REPLY"),
        "boot_id": os.environ.get("SM_GATEWAY_BOOT_ID") or "",
    }


def eligible(reason: str, stored_ctx, stored_reply, stored_pool, stored_boot,
             ident: dict | None = None) -> bool:
    """True when this stored row may be tried again under ident.

    An unknown reason is not eligible. A caller that cannot read the table
    must not turn that into an empty blocked set.
    """
    ident = ident or identity()
    if reason == "no_context":
        return ident["largest_ctx"] > (stored_ctx or 0)
    if reason == "cut_off":
        return ident["largest_reply"] > (stored_reply or 0)
    if reason in POOL_REASONS:
        return ident["pool_id"] != (stored_pool or "")
    if reason in BOOT_REASONS:
        return ident["boot_id"] != (stored_boot or "")
    return False
