"""Tests for encoder_window.py — window contract, overflow classification, and proxy clamping."""
import json
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import dream_telemetry
import encoder_window


def test_classify_overflow_vllm_8193_overrun():
    body = {
        "message": (
            "This model's maximum context length is 8192 tokens. "
            "However, you requested 8193 tokens in the messages, "
            "please reduce the length of the messages."
        ),
        "type": "invalid_request_error",
        "param": "messages",
        "code": 400,
    }
    res = encoder_window.classify_overflow(400, body, required_tokens=8192)
    assert res.kind == "overrun"
    assert res.advertised == 8192
    assert res.requested == 8193
    # Tuple unpacking support
    kind, (adv, req) = res
    assert kind == "overrun" and adv == 8192 and req == 8193


def test_classify_overflow_value_8193_body():
    body = '{"error": {"message": "Invalid request: value=8193 exceeds max_model_len 8192"}}'
    res = encoder_window.classify_overflow(400, body, required_tokens=8192)
    assert res.kind == "overrun"
    assert res.advertised == 8192
    assert res.requested == 8193


def test_classify_overflow_value_8193_bare():
    body = 'value=8193'
    res = encoder_window.classify_overflow(400, body, required_tokens=8192)
    assert res.kind == "overrun"
    assert res.requested == 8193


LIVE_VLLM_INPUT_TOKENS_SENTENCE = (
    "This model's maximum context length is 8192 tokens. However, you requested "
    "0 output tokens and your prompt contains at least 8193 input tokens, for a "
    "total of at least 8193 tokens. Please reduce the length of the input prompt "
    "or the number of requested output tokens. (parameter=input_tokens, value=8193)"
)


def test_classify_overflow_live_vllm_input_tokens_not_zero_output():
    """Workstation vLLM 400 (fact:2581): 'requested 0 output tokens' must not win."""
    res = encoder_window.classify_overflow(
        400, LIVE_VLLM_INPUT_TOKENS_SENTENCE, required_tokens=8192
    )
    assert res.kind == "overrun"
    assert res.advertised == 8192
    assert res.requested == 8193


def test_overflow_from_response_json_requires_dict_and_enum():
    assert encoder_window.overflow_from_response_json("nope") is None
    assert encoder_window.overflow_from_response_json({"overflow": "nope"}) is None
    assert encoder_window.overflow_from_response_json(
        {"overflow": {"kind": "garbage", "advertised": 8192, "requested": 8193}}
    ) is None
    res = encoder_window.overflow_from_response_json(
        {"error": "upstream_fault", "overflow": {
            "kind": "overrun", "advertised": "8192", "requested": 8193,
        }}
    )
    assert res is not None
    assert res.kind == "overrun"
    assert res.advertised == 8192
    assert res.requested == 8193


def test_overflow_fields_coerces_int_or_null():
    fields = encoder_window.overflow_fields(
        encoder_window.OverflowResult("overrun", 8192, 8193)
    )
    assert fields == {"kind": "overrun", "advertised": 8192, "requested": 8193}
    fields_none = encoder_window.overflow_fields(
        encoder_window.OverflowResult("other", None, None)
    )
    assert fields_none == {"kind": "other", "advertised": None, "requested": None}


def test_classify_overflow_mismatch_short_window():
    body = {
        "message": (
            "This model's maximum context length is 512 tokens. "
            "However, you requested 600 tokens in the messages."
        )
    }
    res = encoder_window.classify_overflow(400, body, required_tokens=8192)
    assert res.kind == "mismatch"
    assert res.advertised == 512
    assert res.requested == 600


def test_classify_overflow_larger_than_slack():
    # Gap > OVERFLOW_TOKEN_SLACK (16) with advertised >= required
    body = {
        "message": (
            "This model's maximum context length is 8192 tokens. "
            "However, you requested 8250 tokens in the messages."
        )
    }
    res = encoder_window.classify_overflow(400, body, required_tokens=8192)
    assert res.kind == "other"
    assert res.advertised == 8192
    assert res.requested == 8250


def test_classify_overflow_unparseable_400():
    res = encoder_window.classify_overflow(400, "Bad request: invalid JSON format", required_tokens=8192)
    assert res.kind == "other"
    assert res.advertised is None
    assert res.requested is None


def test_classify_overflow_non_400():
    res = encoder_window.classify_overflow(500, "Internal server error", required_tokens=8192)
    assert res.kind == "other"


def test_classify_overflow_413_overrun():
    """Proxy/backend may 413 on window overrun — same classification as 400."""
    body = {
        "message": (
            "This model's maximum context length is 8192 tokens. "
            "However, you requested 8193 tokens in the messages."
        )
    }
    res = encoder_window.classify_overflow(413, body, required_tokens=8192)
    assert res.kind == "overrun"
    assert res.advertised == 8192
    assert res.requested == 8193


def test_reserved_clamp_chars_matches_special_token_formula():
    expected = int(
        (dream_telemetry.EMBED_MAX_CONTEXT_TOKENS
         - dream_telemetry.EMBED_SPECIAL_TOKEN_RESERVE)
        * dream_telemetry.EMBED_CHARS_PER_TOKEN
    )
    assert encoder_window.reserved_clamp_chars() == expected
    assert expected == 24570


def test_clamp_encoder_payload_string():
    raw = json.dumps({"input": "x" * 50000, "model": "bge-m3"}).encode("utf-8")
    clamped_bytes = encoder_window.clamp_encoder_payload(raw)
    data = json.loads(clamped_bytes)
    assert len(data["input"]) == dream_telemetry.EMBED_MAX_CHARS
    assert data["model"] == "bge-m3"


def test_clamp_encoder_payload_list_of_strings():
    raw = json.dumps({
        "input": ["a" * 50000, "b" * 30000, "c" * 10],
        "model": "bge-m3"
    }).encode("utf-8")
    clamped_bytes = encoder_window.clamp_encoder_payload(raw)
    data = json.loads(clamped_bytes)
    assert len(data["input"]) == 3
    assert len(data["input"][0]) == dream_telemetry.EMBED_MAX_CHARS
    assert len(data["input"][1]) == dream_telemetry.EMBED_MAX_CHARS
    assert data["input"][2] == "c" * 10


def test_clamp_encoder_payload_token_ids_untouched():
    raw = json.dumps({"input": [101, 2054, 102], "model": "bge-m3"}).encode("utf-8")
    clamped_bytes = encoder_window.clamp_encoder_payload(raw)
    data = json.loads(clamped_bytes)
    assert data["input"] == [101, 2054, 102]


def test_handle_encoder_clamps_array_per_element():
    import asyncio
    from yarl import URL
    import hive_mind_proxy as g

    sent_data = None

    from unittest.mock import AsyncMock

    class _OneShotAsyncIter:
        def __init__(self, body: bytes):
            self._body = body

        def iter_any(self):
            return self._agen()

        async def _agen(self):
            if self._body:
                yield self._body

        async def read(self, n=-1):
            return self._body

    class _FakeUpstream:
        status = 200
        headers = {}

        def __init__(self):
            self.content = _OneShotAsyncIter(b'{"data": [{"embedding": [0.1]}]}')

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class _CaptureSession:
        def request(self, method, url, headers=None, data=None, allow_redirects=False):
            nonlocal sent_data
            sent_data = data
            return _FakeUpstream()

    class _EmbedReq:
        method = "POST"
        path = "/v1/embeddings"
        rel_url = URL("/v1/embeddings", encoded=True)
        headers = {}
        can_read_body = True

        def __init__(self):
            self._payload_writer = AsyncMock()
            self._raw = json.dumps({"input": ["x" * 50000], "model": "bge-m3"}).encode("utf-8")
            self.content_length = len(self._raw)

        async def read(self):
            return self._raw

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _CaptureSession()
    resp = asyncio.run(proxy.handle_encoder(_EmbedReq()))
    assert resp.status == 200
    assert sent_data is not None
    forwarded = json.loads(sent_data)
    # The array must have 1 element, clamped to EMBED_MAX_CHARS (never array slicing)
    assert len(forwarded["input"]) == 1
    assert len(forwarded["input"][0]) == dream_telemetry.EMBED_MAX_CHARS


def test_handle_encoder_chunked_does_not_buffer_for_clamp():
    import asyncio
    from yarl import URL
    import hive_mind_proxy as g
    from unittest.mock import AsyncMock

    sent_data = None

    class _CaptureSession:
        def request(self, method, url, headers=None, data=None, allow_redirects=False):
            nonlocal sent_data
            sent_data = data
            class _FakeUpstream:
                status = 200
                headers = {}
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    pass
            return _FakeUpstream()

    class _ChunkedEmbedReq:
        method = "POST"
        path = "/v1/embeddings"
        rel_url = URL("/v1/embeddings", encoded=True)
        headers = {}
        can_read_body = True
        content_length = None  # Chunked / streaming (unknown content length)
        content = b"streamed-chunked-content"

        def __init__(self):
            self._payload_writer = AsyncMock()

        async def read(self):
            return b'{"input": ["not-clamped"], "model": "bge-m3"}'

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _CaptureSession()
    resp = asyncio.run(proxy.handle_encoder(_ChunkedEmbedReq()))
    assert resp.status == 200
    # llm_body was NOT buffered; request.content was passed directly to upstream
    assert sent_data == b"streamed-chunked-content"


def test_handle_encoder_body_exceeding_cap_returns_413():
    import asyncio
    from yarl import URL
    import hive_mind_proxy as g
    from unittest.mock import AsyncMock

    class _ExceedingReq:
        method = "POST"
        path = "/v1/embeddings"
        rel_url = URL("/v1/embeddings", encoded=True)
        headers = {}
        can_read_body = True
        content_length = g.EMBED_RERANK_BUFFER_CAP

        def __init__(self):
            self._payload_writer = AsyncMock()

        async def read(self):
            return b"x" * (g.EMBED_RERANK_BUFFER_CAP + 1)

    proxy = g.AsyncHiveMindProxy()
    resp = asyncio.run(proxy.handle_encoder(_ExceedingReq()))
    assert resp.status == 413
    assert resp.headers.get("X-SM-Fault-Origin") == "gateway"

