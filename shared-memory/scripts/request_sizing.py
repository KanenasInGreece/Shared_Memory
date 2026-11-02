"""How large a reasoning request is, and whether a reply is a loop.

The gateway sets max_tokens per try from reply_room. A backend qualifies on
context room alone: n_ctx times (1 - FIT_MARGIN), minus the prompt estimate,
must be at least that estimate. Qualifying on the spend cap would strand
work when max_output_tokens is small. fact:3394 measured real prompts near
3.3–5 bytes per token, lowest record 2.71, so the ratio default is 2.7.
Every function reads the environment on each call. Each process validates
at import only the values it uses, so a malformed value stops that process
and no other.
"""
import os
import re
from collections import Counter


def nrem_insight_item_chars() -> int:
    """Caps each judgement body in the insight prompt."""
    return int(os.environ.get("NREM_INSIGHT_SLOT_INPUT_CHARS", "2000"))


def fit_margin() -> float:
    """Fraction of n_ctx held back. Default 0.10, so the usable window is 0.9."""
    return float(os.environ.get("FIT_MARGIN", "0.10"))


def _fit_ratio() -> float:
    # fact:3394: lowest measured bytes-per-token on a real prompt was 2.71.
    return float(os.environ.get("LLM_CHARS_PER_TOKEN_RATIO", "2.7"))


def estimate_prompt_tokens(n_bytes: int) -> float:
    """Estimate prompt tokens from request body byte length.

    A ratio that is not greater than 0 gives 0.0, so a bad ratio cannot
    divide and cannot invent a huge estimate.
    """
    ratio = _fit_ratio()
    return float(n_bytes) / ratio if ratio > 0.0 else 0.0


def context_room(n_ctx, est: float) -> float | None:
    """Tokens left in the context window after the prompt and the margin.

    None when n_ctx is missing or not a positive number: an undeclared
    context does not qualify.
    """
    try:
        n = float(n_ctx)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return n * (1.0 - fit_margin()) - float(est)


def reply_room(n_ctx, max_output, est: float) -> float | None:
    """Tokens this try may spend: min(max_output_tokens, context room).

    max_output_tokens is only the spending cap. None when either bound is
    missing, so the walk does not invent a budget.
    """
    room = context_room(n_ctx, est)
    if room is None:
        return None
    try:
        cap = float(max_output)
    except (TypeError, ValueError):
        return None
    if cap <= 0:
        return None
    return min(cap, room)


def qualifies(room, est: float) -> bool:
    """True when context room is at least the prompt estimate.

    A backend qualifies on context room, not on the capped reply. Callers
    pass context_room, not reply_room.
    """
    if room is None:
        return False
    try:
        return float(room) >= float(est)
    except (TypeError, ValueError):
        return False


# seq-rep-4 and the 0.5 line are measurements, not settings (Welleck et al.,
# arXiv 1908.04319). fact:3396: two loops scored 0.994 and 0.915, honest
# cut-offs at most 0.015, and corpus records at most 0.070.
_SEQ_REP_WORDS = 4
_SEQ_REP_LOOP_ABOVE = 0.5

# Flat `{...}` repeated, or one long quoted string repeated. The match is
# never parsed. A plain-text loop needs seq-rep-4 because this pattern misses it.
_FLAT_OBJECT_RE = re.compile(r"\{[^{}]*\}")
_LONG_STRING_RE = re.compile(r'"([^"]{30,})"')


def seq_rep_4(text: str) -> float:
    """1 minus unique 4-word runs over all 4-word runs. Fewer than 4 words scores 0."""
    words = (text or "").split()
    n = _SEQ_REP_WORDS
    if len(words) < n:
        return 0.0
    runs = [tuple(words[i:i + n]) for i in range(len(words) - n + 1)]
    if not runs:
        return 0.0
    return 1.0 - (len(set(runs)) / len(runs))


def _json_repetition(body: str) -> bool:
    """True when a flat object or a long quoted string repeats at least three times."""
    if not body or not body.strip():
        return False
    objects = _FLAT_OBJECT_RE.findall(body)
    if objects:
        normalised = (" ".join(o.split()) for o in objects)
        if max(Counter(normalised).values()) >= 3:
            return True
    strings = _LONG_STRING_RE.findall(body)
    if strings and max(Counter(strings).values()) >= 3:
        return True
    return False


def reply_is_loop(text: str) -> bool:
    """True when a cut-off reply is a loop: the JSON check, or seq-rep-4 above 0.5.

    Callers run this only on finish_reason length. A loop is not moved to
    another backend; a larger budget only feeds it.
    """
    if _json_repetition(text or ""):
        return True
    return seq_rep_4(text or "") > _SEQ_REP_LOOP_ABOVE


def validate_nrem() -> None:
    """Validate the insight body cap. Output bounds are the gateway's, not NREM's."""
    nrem_insight_item_chars()


def validate_fit() -> None:
    """Validate fit sizing environment variables at module import time."""
    _fit_ratio()
    fit_margin()
