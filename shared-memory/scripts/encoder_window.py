"""Encoder window contract and overflow handling for shared-memory (decision:2540).

Defines:
- classify_overflow(): detects window mismatch vs slack-bounded overrun on HTTP 400.
- clamp_encoder_payload(): clamps OpenAI-style embedding inputs per string element.
- OverflowResult: classification result with unpacked tuple compatibility.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from dream_telemetry import (
    EMBED_CHARS_PER_TOKEN,
    EMBED_MAX_CHARS,
    EMBED_MAX_CONTEXT_TOKENS,
    EMBED_SPECIAL_TOKEN_RESERVE,
    RERANK_MAX_DOC_CHARS,
)

log = logging.getLogger("EncoderWindow")

OVERFLOW_TOKEN_SLACK = int(os.environ.get("OVERFLOW_TOKEN_SLACK", "16"))

_embed_window_overruns_total: int = 0
_embed_window_overruns_last_ts: str | None = None


def record_embed_window_overrun() -> None:
    """Record a slack-bounded overrun event and timestamp."""
    global _embed_window_overruns_total, _embed_window_overruns_last_ts
    import datetime
    _embed_window_overruns_total += 1
    _embed_window_overruns_last_ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_embed_window_overruns() -> tuple[int, str | None]:
    """Return (count, last_ts) for slack-bounded embed window overruns."""
    return _embed_window_overruns_total, _embed_window_overruns_last_ts


def reset_embed_window_overruns() -> None:
    """Reset overrun counters (useful for unit tests)."""
    global _embed_window_overruns_total, _embed_window_overruns_last_ts
    _embed_window_overruns_total = 0
    _embed_window_overruns_last_ts = None


class OverflowResult:
    """Result of classify_overflow, supporting both attribute access and tuple unpacking."""

    def __init__(self, kind: str, advertised: int | None = None, requested: int | None = None):
        self.kind = kind  # "mismatch" | "overrun" | "other"
        self.advertised = advertised
        self.requested = requested

    def __iter__(self):
        yield self.kind
        yield (self.advertised, self.requested)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, tuple):
            if len(other) == 2 and isinstance(other[1], tuple):
                return (self.kind, (self.advertised, self.requested)) == other
            if len(other) == 3:
                return (self.kind, self.advertised, self.requested) == other
        if isinstance(other, OverflowResult):
            return (
                self.kind == other.kind
                and self.advertised == other.advertised
                and self.requested == other.requested
            )
        return False

    def __repr__(self) -> str:
        return f"OverflowResult(kind={self.kind!r}, advertised={self.advertised}, requested={self.requested})"


# Regex patterns to extract advertised window and requested tokens from error bodies.
_PATTERNS = [
    # vLLM: "maximum context length is 8192 tokens. However, you requested 8193 tokens"
    re.compile(
        r"maximum context length is\s+(\d+)\s+tokens.*?requested\s+(\d+)\s+tokens",
        re.IGNORECASE | re.DOTALL,
    ),
    # Reversed: "requested 8193 tokens ... maximum context length is 8192"
    re.compile(
        r"requested\s+(\d+)\s+tokens.*?maximum context length is\s+(\d+)\s+tokens",
        re.IGNORECASE | re.DOTALL,
    ),
    # max_model_len ... value=8193
    re.compile(
        r"max_model_len\D+(\d+).*?value\D+(\d+)",
        re.IGNORECASE | re.DOTALL,
    ),
    # value=8193 ... max_model_len 8192
    re.compile(
        r"value\s*[:=]\s*(\d+).*?max_model_len\D+(\d+)",
        re.IGNORECASE | re.DOTALL,
    ),
    # llama.cpp / generic: tokens: 8193, context: 8192
    re.compile(
        r"(?:input tokens|tokens?|prompt)\D+(\d+)\D+(?:context window|context|max)\D+(\d+)",
        re.IGNORECASE,
    ),
    # context window: 8192, tokens: 8193
    re.compile(
        r"(?:context window|context|max)\D+(\d+)\D+(?:input tokens|tokens?|prompt)\D+(\d+)",
        re.IGNORECASE,
    ),
]


def _extract_body_text(body: Any) -> str:
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", errors="replace")
        except Exception:
            return ""
    if isinstance(body, dict):
        # Extract message/error string or serialize values
        parts = []
        for key in ("message", "detail", "error"):
            val = body.get(key)
            if isinstance(val, str):
                parts.append(val)
            elif isinstance(val, dict):
                parts.append(str(val.get("message", val)))
            elif isinstance(val, list):
                parts.append(str(val))
        if parts:
            return " ".join(parts)
        return json.dumps(body)
    if isinstance(body, str):
        # Attempt to inspect JSON dict
        text = body.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return _extract_body_text(parsed)
            except Exception:
                pass
        return body
    return str(body)


def classify_overflow(
    status: int, body: Any, required_tokens: int | None = None
) -> OverflowResult:
    """Classify an HTTP response as mismatch, overrun, or other.

    - status != 400 -> 'other'
    - advertised < required_tokens -> 'mismatch'
    - advertised >= required_tokens and requested <= advertised + OVERFLOW_TOKEN_SLACK -> 'overrun'
    - larger-than-slack or unparseable -> 'other' with (advertised, requested) when parsed
    """
    if status != 400:
        return OverflowResult("other", None, None)

    if required_tokens is None:
        required_tokens = EMBED_MAX_CONTEXT_TOKENS

    text = _extract_body_text(body)
    advertised: int | None = None
    requested: int | None = None

    for idx, pat in enumerate(_PATTERNS):
        m = pat.search(text)
        if m:
            if idx == 0:  # advertised, requested
                advertised, requested = int(m.group(1)), int(m.group(2))
            elif idx == 1:  # requested, advertised
                requested, advertised = int(m.group(1)), int(m.group(2))
            elif idx == 2:  # advertised, requested
                advertised, requested = int(m.group(1)), int(m.group(2))
            elif idx == 3:  # requested, advertised
                requested, advertised = int(m.group(1)), int(m.group(2))
            elif idx == 4:  # requested, advertised
                requested, advertised = int(m.group(1)), int(m.group(2))
            elif idx == 5:  # advertised, requested
                advertised, requested = int(m.group(1)), int(m.group(2))
            break

    # Check for bare value=8193 when advertised wasn't explicitly captured
    if requested is None:
        m_val = re.search(r"value\s*[:=]\s*(\d+)", text, re.IGNORECASE)
        if m_val:
            val = int(m_val.group(1))
            if val >= required_tokens:
                requested = val
                advertised = required_tokens

    if advertised is None or requested is None:
        return OverflowResult("other", None, None)

    if advertised < required_tokens:
        return OverflowResult("mismatch", advertised, requested)

    if requested <= advertised + OVERFLOW_TOKEN_SLACK:
        return OverflowResult("overrun", advertised, requested)

    # Larger than slack with advertised >= required
    return OverflowResult("other", advertised, requested)


def clamp_encoder_payload(raw_body: bytes) -> bytes:
    """Clamp OpenAI input string or array of strings to EMBED_MAX_CHARS per string element.

    Token-id arrays (list of ints or list of list of ints) are left untouched.
    """
    try:
        data = json.loads(raw_body)
    except Exception:
        return raw_body

    if not isinstance(data, dict):
        return raw_body

    inp = data.get("input")
    if isinstance(inp, str):
        if len(inp) > EMBED_MAX_CHARS:
            data["input"] = inp[:EMBED_MAX_CHARS]
            return json.dumps(data).encode("utf-8")
    elif isinstance(inp, list):
        # Clamp per string element without modifying list length
        modified = False
        new_list = []
        for item in inp:
            if isinstance(item, str):
                if len(item) > EMBED_MAX_CHARS:
                    new_list.append(item[:EMBED_MAX_CHARS])
                    modified = True
                else:
                    new_list.append(item)
            else:
                new_list.append(item)
        if modified:
            data["input"] = new_list
            return json.dumps(data).encode("utf-8")

    return raw_body
