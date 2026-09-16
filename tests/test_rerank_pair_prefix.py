"""Tests for reranker query-aware pair prefixing (Task 2 / decision:2557)."""
import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock
from yarl import URL

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import dream_telemetry
from dream_telemetry import (
    RERANK_MAX_DOC_CHARS,
    as_text,
    pair_budget,
    prefix_rerank_doc,
    prefix_rerank_query,
    special_reserve_chars,
)
import hive_mind_proxy as g


def test_pair_budget_and_reserve_invariant():
    """len(prefixed_q) + len(prefixed_doc) + special_reserve_chars <= RERANK_MAX_DOC_CHARS (I4)."""
    for q in ("", "short", "x" * 50000):
        prefixed_q = prefix_rerank_query(q)
        prefixed_d = prefix_rerank_doc(q, "x" * 100000)
        assert len(prefixed_q) + len(prefixed_d) + special_reserve_chars <= RERANK_MAX_DOC_CHARS
        assert len(prefixed_q) <= pair_budget
        assert len(prefixed_d) <= max(0, pair_budget - len(prefixed_q))


def test_prefix_rerank_type_safety():
    """as_text never 500s: None and non-str -> empty string, no len() error."""
    assert prefix_rerank_query(123) == ""
    assert prefix_rerank_query(None) == ""
    assert prefix_rerank_query(["list"]) == ""
    assert prefix_rerank_query(False) == ""

    assert prefix_rerank_doc(None, None) == ""
    assert prefix_rerank_doc(123, 456) == ""
    assert prefix_rerank_doc("query", None) == ""
    assert prefix_rerank_doc("query", 123) == ""
    assert prefix_rerank_doc(None, "doc") == "doc"[:pair_budget]


def test_oversize_query_prefixed_and_leaves_no_budget_for_doc():
    """A query larger than pair_budget is itself prefixed, leaving 0 for the doc."""
    huge_query = "q" * (pair_budget + 1000)
    prefixed_q = prefix_rerank_query(huge_query)
    assert len(prefixed_q) == pair_budget
    prefixed_d = prefix_rerank_doc(huge_query, "some doc content")
    assert prefixed_d == ""


def test_rerank_window_defaults_to_embedding_window_invariant():
    """I6: RERANK_MAX_DOC_CHARS == EMBED_MAX_CHARS at defaults."""
    assert dream_telemetry.RERANK_MAX_DOC_CHARS == dream_telemetry.EMBED_MAX_CHARS


def test_handle_encoder_rerank_prefixes_query_and_documents():
    """handle_encoder on /v1/reranking prefixes query and documents per string element."""
    sent_data = None

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
            self.content = _OneShotAsyncIter(b'{"results": []}')

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class _CaptureSession:
        def request(self, method, url, headers=None, data=None, allow_redirects=False):
            nonlocal sent_data
            sent_data = data
            return _FakeUpstream()

    class _RerankReq:
        method = "POST"
        path = "/v1/reranking"
        rel_url = URL("/v1/reranking", encoded=True)
        headers = {}
        can_read_body = True

        def __init__(self):
            self._payload_writer = AsyncMock()
            self._raw = json.dumps({
                "query": "search query",
                "documents": ["x" * 50000, 123, "short doc"],
                "model": "bge-reranker-v2-m3",
            }).encode("utf-8")
            self.content_length = len(self._raw)

        async def read(self):
            return self._raw

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _CaptureSession()
    resp = asyncio.run(proxy.handle_encoder(_RerankReq()))
    assert resp.status == 200
    assert sent_data is not None
    forwarded = json.loads(sent_data)

    # query is prefixed
    assert forwarded["query"] == prefix_rerank_query("search query")
    # documents: array length unchanged (3 elements)
    assert len(forwarded["documents"]) == 3
    # first document prefixed to remaining budget
    expected_d0 = prefix_rerank_doc("search query", "x" * 50000)
    assert forwarded["documents"][0] == expected_d0
    # non-string document entry left unchanged
    assert forwarded["documents"][1] == 123
    # short doc unchanged
    assert forwarded["documents"][2] == "short doc"


def test_handle_encoder_rerank_invalid_json_forwarded_unchanged():
    """handle_encoder on /v1/reranking forwards invalid JSON unchanged without raising."""
    sent_data = None

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
            self.content = _OneShotAsyncIter(b'{"results": []}')

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class _CaptureSession:
        def request(self, method, url, headers=None, data=None, allow_redirects=False):
            nonlocal sent_data
            sent_data = data
            return _FakeUpstream()

    raw_invalid = b"not valid json {"

    class _InvalidReq:
        method = "POST"
        path = "/v1/reranking"
        rel_url = URL("/v1/reranking", encoded=True)
        headers = {}
        can_read_body = True

        def __init__(self):
            self._payload_writer = AsyncMock()
            self._raw = raw_invalid
            self.content_length = len(self._raw)

        async def read(self):
            return self._raw

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _CaptureSession()
    resp = asyncio.run(proxy.handle_encoder(_InvalidReq()))
    assert resp.status == 200
    assert sent_data == raw_invalid


@pytest.mark.parametrize("non_str_query", [123, True, ["list"]])
def test_handle_encoder_rerank_coerces_non_str_query_to_empty(non_str_query):
    """ADV-R1: POST/handle_encoder coerces non-str query to empty string on the wire without 500."""
    sent_data = None

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
            self.content = _OneShotAsyncIter(b'{"results": []}')

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class _CaptureSession:
        def request(self, method, url, headers=None, data=None, allow_redirects=False):
            nonlocal sent_data
            sent_data = data
            return _FakeUpstream()

    class _RerankReq:
        method = "POST"
        path = "/v1/reranking"
        rel_url = URL("/v1/reranking", encoded=True)
        headers = {}
        can_read_body = True

        def __init__(self):
            self._payload_writer = AsyncMock()
            self._raw = json.dumps({
                "query": non_str_query,
                "documents": ["x" * 1000],
                "model": "bge-reranker-v2-m3",
            }).encode("utf-8")
            self.content_length = len(self._raw)

        async def read(self):
            return self._raw

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _CaptureSession()
    resp = asyncio.run(proxy.handle_encoder(_RerankReq()))
    assert resp.status == 200
    assert sent_data is not None
    forwarded = json.loads(sent_data)

    assert forwarded["query"] == ""
    assert forwarded["documents"][0] == prefix_rerank_doc("", "x" * 1000)


def test_handle_encoder_rerank_oversize_query_pins_pair_budget():
    """ADV-R2: query larger than pair_budget is prefixed to pair_budget and doc gets remaining 0."""
    sent_data = None

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
            self.content = _OneShotAsyncIter(b'{"results": []}')

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class _CaptureSession:
        def request(self, method, url, headers=None, data=None, allow_redirects=False):
            nonlocal sent_data
            sent_data = data
            return _FakeUpstream()

    oversize_query = "q" * (pair_budget + 1000)

    class _RerankReq:
        method = "POST"
        path = "/v1/reranking"
        rel_url = URL("/v1/reranking", encoded=True)
        headers = {}
        can_read_body = True

        def __init__(self):
            self._payload_writer = AsyncMock()
            self._raw = json.dumps({
                "query": oversize_query,
                "documents": ["x" * 1000],
                "model": "bge-reranker-v2-m3",
            }).encode("utf-8")
            self.content_length = len(self._raw)

        async def read(self):
            return self._raw

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _CaptureSession()
    resp = asyncio.run(proxy.handle_encoder(_RerankReq()))
    assert resp.status == 200
    assert sent_data is not None
    forwarded = json.loads(sent_data)

    assert len(forwarded["query"]) == pair_budget
    assert forwarded["query"] == "q" * pair_budget
    assert forwarded["documents"][0] == ""

