"""Central reasoning-request sizing for the gateway and dream daemons.

This is the ONE place that decides how large a reasoning request is (output bound,
the insight judgement body input cut, and the fit estimate). Full record text,
decision titles, reversal lines, and the previous insight are still assembled
whole and will move here when sizing becomes backend-aware (fact:2983). A
backend's extra_body may still override max_tokens after the fit check —
hive_mind_proxy._apply_backend_body_overrides. Today every value is a global
default read from the environment; the backend keyword is accepted for future
backend-specific sizing. Every function reads the environment on each call. Each
process validates at import only the values it uses, so a malformed value stops
that process and no other, as before.
"""
import os


def rem_solo_max_tokens(*, backend: str | None = None) -> int:
    """Return maximum tokens for a solo REM enrichment request."""
    return int(os.environ.get("REM_MAX_TOKENS_SOLO", "1500"))


def _rem_retry_factor() -> float:
    return float(os.environ.get("REM_TRUNCATION_RETRY_FACTOR", "2.0"))


def rem_retry_bound(first: int, *, degenerate: bool, backend: str | None = None) -> int:
    """Return token bound for a REM retry.

    An honest truncation widens the bound once; a repetition loop retries at
    the same bound, because a larger budget only feeds the loop (fact:1329/1330).
    """
    if degenerate:
        return first
    return int(first * _rem_retry_factor())


def rem_batch_max_tokens(n_facts: int, *, backend: str | None = None) -> int:
    """Return token budget for a REM batch scaled by number of facts sent."""
    per_fact = int(os.environ.get("REM_MAX_TOKENS_PER_FACT", "400"))
    per_summary = int(os.environ.get("REM_MAX_TOKENS_PER_SUMMARY", "250"))
    return (per_fact + per_summary) * n_facts


def _nrem_insight_base() -> int:
    return int(os.environ.get("NREM_MAX_TOKENS_INSIGHT", "8192"))


def _nrem_retry_factor() -> float:
    return float(os.environ.get("NREM_TRUNCATION_RETRY_FACTOR", "2.0"))


def nrem_insight_bounds(*, backend: str | None = None) -> list[int]:
    """Return initial and widened token bounds for an NREM insight fold call.

    Widen a truncated bound once before failing the fold, so a longer draft is
    not dead-lettered by the default; 8192 stays above the growing
    previous-insight floor after 2048 stalled the busiest cluster (decision:1205).
    """
    base = _nrem_insight_base()
    factor = _nrem_retry_factor()
    return [base, int(base * factor)]


def nrem_insight_item_chars(*, backend: str | None = None) -> int:
    """Caps each judgement body (decision text after the title line, or the
    full retrospective) before the insight prompt (decision:1205).
    """
    return int(os.environ.get("NREM_INSIGHT_SLOT_INPUT_CHARS", "2000"))


def _fit_ratio() -> float:
    return float(os.environ.get("LLM_CHARS_PER_TOKEN_RATIO", "1.2"))


def estimate_prompt_tokens(n_bytes: int, *, backend: str | None = None) -> float:
    """Estimate prompt tokens from request body byte length.

    The ratio is a measured floor of 1.2 chars per token, chosen so the
    estimate never under-counts prompt tokens; a ratio that is not > 0 gives 0.0.
    """
    ratio = _fit_ratio()
    return float(n_bytes) / ratio if ratio > 0.0 else 0.0


def _fit_default_output() -> int:
    return int(os.environ.get("FIT_DEFAULT_OUTPUT_TOKENS", "2048"))


def effective_max_tokens(body_obj, *, backend: str | None = None) -> float:
    """Return effective max_tokens from request payload or estimator default.

    The default (FIT_DEFAULT_OUTPUT_TOKENS, 2048) is an unmeasured output budget
    used only when the caller sends no max_tokens (fact:1338). REM_MAX_TOKENS_*
    is the measured ceiling for dream traffic (decision:1330); this is only the
    estimator fallback.
    """
    default_out = float(_fit_default_output())
    if isinstance(body_obj, dict):
        mt = body_obj.get("max_tokens")
        if isinstance(mt, (int, float)) and mt > 0:
            return float(mt)
    return default_out


def _fit_margin() -> float:
    return float(os.environ.get("FIT_MARGIN", "0.10"))


def fits(n_ctx, est_prompt_tokens: float, effective_max_tokens: float, *, backend: str | None = None) -> bool:
    """Check whether a request fits within backend context window.

    Eligible iff est_prompt_tokens + effective_max_tokens <= n_ctx * (1 - FIT_MARGIN);
    an undeclared n_ctx always fits.
    """
    if not n_ctx:
        return True
    margin = _fit_margin()
    return (est_prompt_tokens + effective_max_tokens) <= n_ctx * (1.0 - margin)


def validate_rem() -> None:
    """Validate REM sizing environment variables at module import time."""
    rem_solo_max_tokens()
    rem_batch_max_tokens(0)
    _rem_retry_factor()


def validate_nrem() -> None:
    """Validate NREM sizing environment variables at module import time."""
    _nrem_insight_base()
    _nrem_retry_factor()
    nrem_insight_item_chars()


def validate_fit() -> None:
    """Validate fit sizing environment variables at module import time."""
    _fit_ratio()
    _fit_margin()
    _fit_default_output()
