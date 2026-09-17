"""Classify embed/rerank 400/413 as window mismatch vs slack-bounded overrun, and clamp embedding inputs per string (decision:2540).

Defines:
- classify_overflow(): detects window mismatch vs slack-bounded overrun on HTTP 400/413.
- clamp_encoder_payload(): clamps OpenAI-style embedding inputs per string element.
- OverflowResult: classification result with unpacked tuple compatibility.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from aiohttp import ClientTimeout

from dream_telemetry import (
    EMBED_CHARS_PER_TOKEN,
    EMBED_MAX_CHARS,
    EMBED_MAX_CONTEXT_TOKENS,
    EMBED_SPECIAL_TOKEN_RESERVE,
    RERANK_MAX_DOC_CHARS,
    embed_ceiling,
    prefix_rerank_doc,
    prefix_rerank_query,
    rerank_ceiling,
)

log = logging.getLogger("EncoderWindow")

OVERFLOW_TOKEN_SLACK = int(os.environ.get("OVERFLOW_TOKEN_SLACK", "16"))


def reserved_clamp_chars() -> int:
    """Char length that fits the required window after special-token reserve.

    Same arithmetic coordinator._embed uses as the snap target:
    (EMBED_MAX_CONTEXT_TOKENS - EMBED_SPECIAL_TOKEN_RESERVE) * EMBED_CHARS_PER_TOKEN.
    """
    return int(
        (EMBED_MAX_CONTEXT_TOKENS - EMBED_SPECIAL_TOKEN_RESERVE) * EMBED_CHARS_PER_TOKEN
    )

_window_cache: dict[str, dict] = {
    "embedder": {"advertised_tokens": None, "source": None, "full_payload_ok": None},
    "reranker": {"advertised_tokens": None, "source": None, "full_payload_ok": None},
}


def set_encoder_cache(encoder_name: str, val: dict) -> None:
    """Explicitly set cached state for an encoder (unit tests / mock injection)."""
    _window_cache[encoder_name] = dict(val)


def get_encoder_window_snapshot() -> dict:
    """Return top-level /health encoder_window block."""
    _default_probe = {"advertised_tokens": None, "source": None, "full_payload_ok": None}
    return {
        "required_tokens": EMBED_MAX_CONTEXT_TOKENS,
        "special_token_reserve": EMBED_SPECIAL_TOKEN_RESERVE,
        "embed_max_chars": EMBED_MAX_CHARS,
        "embedder": dict(_window_cache.get("embedder") or _default_probe),
        "reranker": dict(_window_cache.get("reranker") or _default_probe),
    }


def _upstream_url(base: str, rel: str) -> str:
    base = str(base).rstrip("/")
    rel = str(rel)
    if not rel.startswith("/"):
        rel = "/" + rel
    if base.endswith("/v1") and rel.startswith("/v1/"):
        rel = rel[len("/v1"):]
    return f"{base}{rel}"


async def probe_encoder(
    session: Any,
    base_url: str,
    route: str,
    model_id: str,
) -> dict:
    """Probe an encoder backend for advertised tokens and empirical full payload capability."""
    encoder_name = "embedder" if "embed" in route else "reranker"
    cached = dict(_window_cache.get(encoder_name, {}))

    adv: int | None = None
    src: str | None = None
    reachable = False

    # 1. Try GET /v1/models matching model_id
    models_url = _upstream_url(base_url, "/v1/models")
    try:
        timeout = ClientTimeout(total=5.0)
        async with session.get(models_url, timeout=timeout, allow_redirects=False) as r:
            reachable = True
            if r.status == 200:
                body = await r.json()
                data = body.get("data", [])
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            mid = str(item.get("id", ""))
                            if mid == model_id or mid.endswith("/" + model_id) or mid.endswith(model_id):
                                ctx = (
                                    item.get("max_model_len")
                                    or item.get("context_length")
                                    or item.get("n_ctx")
                                    or item.get("max_tokens")
                                )
                                if ctx is not None:
                                    try:
                                        adv = int(ctx)
                                        src = "v1_models"
                                        break
                                    except (ValueError, TypeError):
                                        pass
    except Exception:
        pass

    # 2. If not found, try /props (llama.cpp)
    if adv is None:
        props_base = str(base_url).rstrip("/").removesuffix("/v1")
        props_url = f"{props_base}/props"
        try:
            timeout = ClientTimeout(total=5.0)
            async with session.get(props_url, timeout=timeout, allow_redirects=False) as r:
                reachable = True
                if r.status == 200:
                    body = await r.json()
                    if isinstance(body, dict):
                        ctx = (
                            body.get("default_generation_settings", {}).get("n_ctx")
                            or body.get("n_ctx")
                        )
                        if ctx is not None:
                            try:
                                adv = int(ctx)
                                src = "props"
                            except (ValueError, TypeError):
                                pass
        except Exception:
            pass

    # Carry forward previous advertised and full_payload_ok across failing cycles
    if not reachable:
        if cached.get("advertised_tokens") is not None or cached.get("full_payload_ok") is not None:
            return cached
        return {
            "advertised_tokens": None,
            "source": None,
            "full_payload_ok": None,
        }

    # Re-run one-shot only if advertised changes or last result failed/missing and encoder is up
    if (
        adv == cached.get("advertised_tokens")
        and cached.get("full_payload_ok") is True
    ):
        return cached

    full_ok: bool | None = False
    if encoder_name == "embedder":
        post_url = _upstream_url(base_url, "/v1/embeddings")
        text = "x" * EMBED_MAX_CHARS
        payload = {"input": text, "model": model_id}
        ceiling = embed_ceiling(EMBED_MAX_CHARS)
        try:
            timeout = ClientTimeout(total=ceiling)
            async with session.post(post_url, json=payload, timeout=timeout, allow_redirects=False) as r:
                await r.read()
                full_ok = (r.status == 200)
        except Exception:
            full_ok = False
    else:
        full_ok = None
        post_url = _upstream_url(base_url, "/v1/reranking")
        raw_query = "encoder window probe"
        query = prefix_rerank_query(raw_query)
        doc = prefix_rerank_doc(query, "x" * RERANK_MAX_DOC_CHARS)
        payload = {"query": query, "documents": [doc], "model": model_id}
        ceiling = rerank_ceiling([doc])
        try:
            timeout = ClientTimeout(total=ceiling)
            async with session.post(post_url, json=payload, timeout=timeout, allow_redirects=False) as r:
                await r.read()
                if r.status == 200:
                    full_ok = True
                elif r.status == 400:
                    full_ok = False
                else:
                    full_ok = None
        except Exception:
            full_ok = None


    res = {
        "advertised_tokens": adv if adv is not None else cached.get("advertised_tokens"),
        "source": src if src is not None else cached.get("source"),
        "full_payload_ok": full_ok,
    }
    _window_cache[encoder_name] = res
    return res


async def probe_encoder_window(session: Any, embed_base: str, rerank_base: str) -> dict:
    """Probe both encoders and return the updated snapshot."""
    await probe_encoder(session, embed_base, "/v1/embeddings", "bge-m3")
    await probe_encoder(session, rerank_base, "/v1/reranking", "bge-reranker-v2-m3")
    return get_encoder_window_snapshot()

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


def reset_encoder_window_cache() -> None:
    """Reset the probe window cache to defaults (useful for unit tests)."""
    _window_cache["embedder"] = {"advertised_tokens": None, "source": None, "full_payload_ok": None}
    _window_cache["reranker"] = {"advertised_tokens": None, "source": None, "full_payload_ok": None}




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

    - status not in (400, 413) -> 'other' (proxy/backend may 413 on window overrun)
    - advertised < required_tokens -> 'mismatch'
    - advertised >= required_tokens and requested <= advertised + OVERFLOW_TOKEN_SLACK -> 'overrun'
    - larger-than-slack or unparseable -> 'other' with (advertised, requested) when parsed
    """
    if status not in (400, 413):
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

    # "you requested 0 output tokens" is not a window request (live vLLM
    # sentence, fact:2581). Drop it so value=N / input-token patterns win.
    if requested == 0:
        requested = None

    # Check for bare value=8193 when advertised wasn't explicitly captured
    if requested is None:
        m_val = re.search(r"value\s*[:=]\s*(\d+)", text, re.IGNORECASE)
        if m_val:
            val = int(m_val.group(1))
            if val >= required_tokens:
                requested = val
                if advertised is None:
                    advertised = required_tokens

    if advertised is None or requested is None:
        return OverflowResult("other", None, None)

    if advertised < required_tokens:
        return OverflowResult("mismatch", advertised, requested)

    if requested <= advertised + OVERFLOW_TOKEN_SLACK:
        return OverflowResult("overrun", advertised, requested)

    # Larger than slack with advertised >= required
    return OverflowResult("other", advertised, requested)


_OVERFLOW_KINDS = frozenset({"overrun", "mismatch", "other"})


def _coerce_overflow_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def overflow_fields(classification: OverflowResult) -> dict:
    """S8 encoder overflow object: enum + ints/null, never provider text."""
    kind = classification.kind if classification.kind in _OVERFLOW_KINDS else "other"
    return {
        "kind": kind,
        "advertised": _coerce_overflow_int(classification.advertised),
        "requested": _coerce_overflow_int(classification.requested),
    }


def overflow_from_response_json(obj: Any) -> OverflowResult | None:
    """Parse gateway overflow fields. None if missing or malformed (not a crash)."""
    if not isinstance(obj, dict):
        return None
    ov = obj.get("overflow")
    if not isinstance(ov, dict):
        return None
    kind = ov.get("kind")
    if kind not in _OVERFLOW_KINDS:
        return None
    return OverflowResult(
        kind,
        _coerce_overflow_int(ov.get("advertised")),
        _coerce_overflow_int(ov.get("requested")),
    )


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
