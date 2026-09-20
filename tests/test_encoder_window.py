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


# --------------------------------------------------------------------------- #
# handle_encoder wire-cap Prove-It (F-GW-08 leftover B)
# Fakes speak StreamReader.read(n) from a queued chunk list. Bytes `content`
# is not a StreamReader. Tests go through handle_encoder, not the helper.
# --------------------------------------------------------------------------- #


class _QueuedStream:
    """aiohttp StreamReader stand-in: queued chunks, pull counter, leftover."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.pulls = 0
        self.ns = []

    @property
    def remaining(self):
        return list(self._chunks)

    async def read(self, n: int) -> bytes:
        self.pulls += 1
        self.ns.append(n)
        if n < 0:
            data = b"".join(self._chunks)
            self._chunks.clear()
            return data
        if not self._chunks:
            return b""
        chunk = self._chunks[0]
        if len(chunk) <= n:
            self._chunks.pop(0)
            return chunk
        self._chunks[0] = chunk[n:]
        return chunk[:n]


class _UpstreamBody:
    def __init__(self, body: bytes):
        self._body = body

    def iter_any(self):
        return self._agen()

    async def _agen(self):
        if self._body:
            yield self._body

    async def read(self, n=-1):
        if n < 0:
            return self._body
        return self._body[:n]


class _FakeUpstream:
    status = 200
    headers = {}

    def __init__(self):
        self.content = _UpstreamBody(b'{"data": [{"embedding": [0.1]}]}')

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class _CaptureSession:
    def __init__(self):
        self.calls = []
        self.sent_data = None

    def request(self, method, url, headers=None, data=None, allow_redirects=False):
        self.calls.append({"method": method, "url": url, "data": data})
        self.sent_data = data
        return _FakeUpstream()


def _encoder_req(path, content, content_length, *, raw_for_request_read=None):
    from unittest.mock import AsyncMock
    from yarl import URL

    class _Req:
        method = "POST"
        headers = {}
        can_read_body = True
        keep_alive = False
        read_calls = 0

        def __init__(self):
            self.path = path
            self.rel_url = URL(path, encoded=True)
            self.content = content
            self.content_length = content_length
            self._payload_writer = AsyncMock()
            self._raw_for_request_read = raw_for_request_read

        async def read(self):
            self.read_calls += 1
            if self._raw_for_request_read is not None:
                return self._raw_for_request_read
            return b'{"input":"tiny","model":"bge-m3"}'

    return _Req()


def _spy_json_loads(monkeypatch, module):
    calls = []
    real = module.json.loads

    def _wrapped(*args, **kwargs):
        calls.append(args[0] if args else None)
        return real(*args, **kwargs)

    monkeypatch.setattr(module.json, "loads", _wrapped)
    return calls


def test_handle_encoder_clamps_array_per_element():
    import asyncio
    import hive_mind_proxy as g

    raw = json.dumps({"input": ["x" * 50000], "model": "bge-m3"}).encode("utf-8")
    stream = _QueuedStream([raw])
    req = _encoder_req(
        "/v1/embeddings", stream, len(raw), raw_for_request_read=raw,
    )
    session = _CaptureSession()
    proxy = g.AsyncHiveMindProxy()
    proxy.session = session
    resp = asyncio.run(proxy.handle_encoder(req))
    assert resp.status == 200
    assert session.sent_data is not None
    forwarded = json.loads(session.sent_data)
    assert len(forwarded["input"]) == 1
    assert len(forwarded["input"][0]) == dream_telemetry.EMBED_MAX_CHARS


def test_handle_encoder_declared_cl_over_cap_413s_without_payload_pull(monkeypatch):
    """CL > CAP → 413, content.read pull count 0, no parse, no upstream."""
    import asyncio
    import hive_mind_proxy as g

    loads_calls = _spy_json_loads(monkeypatch, g)
    stream = _QueuedStream([b"x" * 100])
    req = _encoder_req(
        "/v1/embeddings", stream, g.EMBED_RERANK_BUFFER_CAP + 1,
    )
    session = _CaptureSession()
    proxy = g.AsyncHiveMindProxy()
    proxy.session = session
    resp = asyncio.run(proxy.handle_encoder(req))
    assert resp.status == 413
    assert resp.headers.get("X-SM-Fault-Origin") == "gateway"
    assert stream.pulls == 0
    assert req.read_calls == 0
    assert session.calls == []
    assert loads_calls == []
    body = json.loads(resp.body)
    assert "overflow" not in body
    assert str(g.EMBED_RERANK_BUFFER_CAP) in body["error"]


def test_handle_encoder_chunked_under_cap_clamps_embeddings_input():
    """Chunked embeddings under CAP go through clamp_encoder_payload."""
    import asyncio
    import hive_mind_proxy as g

    raw = json.dumps({"input": ["x" * 50000], "model": "bge-m3"}).encode("utf-8")
    assert len(raw) < 50 * 1024
    stream = _QueuedStream([raw])
    req = _encoder_req("/v1/embeddings", stream, None)
    session = _CaptureSession()
    proxy = g.AsyncHiveMindProxy()
    proxy.session = session
    resp = asyncio.run(proxy.handle_encoder(req))
    assert resp.status == 200
    expected = encoder_window.clamp_encoder_payload(raw)
    assert session.sent_data == expected
    assert session.sent_data is not req.content
    forwarded = json.loads(session.sent_data)
    assert len(forwarded["input"]) == 1
    assert len(forwarded["input"][0]) == dream_telemetry.EMBED_MAX_CHARS


def test_handle_encoder_chunked_over_cap_stops_after_first_oversize_pull(monkeypatch):
    """First chunk already CAP+1: do not pull chunk 2, 413, no parse, no forward."""
    import asyncio
    import hive_mind_proxy as g

    loads_calls = _spy_json_loads(monkeypatch, g)
    cap = g.EMBED_RERANK_BUFFER_CAP
    first = b"a" * (cap + 1)
    second = b"b" * 64
    stream = _QueuedStream([first, second])
    req = _encoder_req("/v1/embeddings", stream, None)
    session = _CaptureSession()
    proxy = g.AsyncHiveMindProxy()
    proxy.session = session
    resp = asyncio.run(proxy.handle_encoder(req))
    assert resp.status == 413
    assert resp.headers.get("X-SM-Fault-Origin") == "gateway"
    assert stream.pulls == 1
    assert stream.remaining == [second]
    assert req.read_calls == 0
    assert session.calls == []
    assert loads_calls == []
    body = json.loads(resp.body)
    assert "overflow" not in body


def test_handle_encoder_lying_cl_still_capped_by_helper(monkeypatch):
    """CL==CAP but read(n) yields CAP+1 → 413. request.read() is the tiny lie."""
    import asyncio
    import hive_mind_proxy as g

    loads_calls = _spy_json_loads(monkeypatch, g)
    cap = g.EMBED_RERANK_BUFFER_CAP
    oversize = b"x" * (cap + 1)
    stream = _QueuedStream([oversize])
    tiny = b'{"input":"tiny","model":"bge-m3"}'
    req = _encoder_req(
        "/v1/embeddings", stream, cap, raw_for_request_read=tiny,
    )
    session = _CaptureSession()
    proxy = g.AsyncHiveMindProxy()
    proxy.session = session
    resp = asyncio.run(proxy.handle_encoder(req))
    assert resp.status == 413
    assert resp.headers.get("X-SM-Fault-Origin") == "gateway"
    assert stream.pulls == 1
    assert req.read_calls == 0
    assert session.calls == []
    assert loads_calls == []
    body = json.loads(resp.body)
    assert "overflow" not in body


def test_handle_encoder_chunked_under_cap_prefixes_rerank():
    """Chunked rerank under CAP is prefix_rerank_*, not clamp_encoder_payload."""
    import asyncio
    import hive_mind_proxy as g
    from dream_telemetry import prefix_rerank_doc, prefix_rerank_query

    query = "q" * 30000
    doc = "d" * 30000
    raw = json.dumps({
        "query": query,
        "documents": [doc],
        "model": "bge-reranker-v2-m3",
    }).encode("utf-8")
    assert len(raw) < g.EMBED_RERANK_BUFFER_CAP
    stream = _QueuedStream([raw])
    req = _encoder_req("/v1/reranking", stream, None)
    session = _CaptureSession()
    proxy = g.AsyncHiveMindProxy()
    proxy.session = session
    resp = asyncio.run(proxy.handle_encoder(req))
    assert resp.status == 200
    assert session.sent_data != raw
    assert session.sent_data != encoder_window.clamp_encoder_payload(raw)
    forwarded = json.loads(session.sent_data)
    assert forwarded["query"] == prefix_rerank_query(query)
    assert forwarded["documents"] == [prefix_rerank_doc(query, doc)]


def test_handle_encoder_empty_chunked_sets_llm_body_empty_bytes():
    """Zero-chunk encoder body still sets llm_body=b'' so content is not streamed."""
    import asyncio
    import hive_mind_proxy as g

    stream = _QueuedStream([])
    req = _encoder_req("/v1/embeddings", stream, None)
    session = _CaptureSession()
    proxy = g.AsyncHiveMindProxy()
    proxy.session = session
    resp = asyncio.run(proxy.handle_encoder(req))
    assert resp.status == 200
    assert session.sent_data == b""
    assert session.sent_data is not req.content


def test_handle_proxy_does_not_413_on_encoder_buffer_cap(monkeypatch):
    """LLM path keeps request.read(); encoder CAP must not 413 handle_proxy."""
    import asyncio
    import importlib
    from aiohttp import web
    from yarl import URL

    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv(
        "LLM_BACKENDS_JSON",
        json.dumps([{"url": "http://a:5000", "private_ok": True}]),
    )
    import hive_mind_proxy as g
    importlib.reload(g)

    chat_body = b'{"messages":[{"role":"user","content":"hi"}],"model":"local-model"}'

    class _ChatReq:
        method = "POST"
        path = "/v1/chat/completions"
        rel_url = URL("/v1/chat/completions", encoded=True)
        headers = {}
        can_read_body = True
        content_length = g.EMBED_RERANK_BUFFER_CAP + 1

        async def read(self):
            return chat_body

    forwarded = {}

    async def _spy(request, **kwargs):
        forwarded["called"] = True
        forwarded["llm_body"] = kwargs.get("llm_body")
        return web.json_response({"ok": True}, status=200)

    proxy = g.AsyncHiveMindProxy()
    proxy._forward_upstream = _spy
    resp = asyncio.run(proxy.handle_proxy(_ChatReq()))
    assert resp.status != 413
    assert forwarded.get("called") is True
    assert forwarded.get("llm_body") is not None


def test_main_keeps_50_mib_client_max_size():
    import inspect
    import hive_mind_proxy as g

    src = inspect.getsource(g.main)
    assert "client_max_size=50 * 1024 * 1024" in src

