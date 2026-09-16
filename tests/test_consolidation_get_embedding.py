"""Prove-It for consolidation_loop.get_embedding overflow snap (decision:2569).

A slack-bounded HTTP 400/413 retries at the reserved clamp and returns a
1024-dim vector. A mismatch does not silent-prefix. Floor without 200
returns None — no 1-char garbage, no per-char HTTP spam. Vector only;
the caller's summary text is not rewritten.
"""
import asyncio
import inspect
import logging
import os
import sys
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import consolidation_loop as cl  # noqa: E402
from consolidation_loop import ConsolidationDaemon  # noqa: E402
from dream_telemetry import (  # noqa: E402
    EMBED_CHARS_PER_TOKEN,
    EMBED_MAX_CONTEXT_TOKENS,
    EMBED_SPECIAL_TOKEN_RESERVE,
)

RESERVED_CLAMP = int(
    (EMBED_MAX_CONTEXT_TOKENS - EMBED_SPECIAL_TOKEN_RESERVE) * EMBED_CHARS_PER_TOKEN
)

OVERRUN_BODY = (
    '{"message": "This model\'s maximum context length is 8192 tokens. '
    'However, you requested 8193 tokens in the messages, '
    'please reduce the length of the messages."}'
)
MISMATCH_BODY = (
    '{"message": "This model\'s maximum context length is 512 tokens. '
    'However, you requested 600 tokens in the messages."}'
)
VEC_1024 = [0.01] * 1024


class _FakeResp:
    def __init__(self, status_code, text="", embedding=None):
        self.status_code = status_code
        self.text = text
        self._embedding = embedding

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code} error", request=None, response=self,
            )

    def json(self):
        if self._embedding is None:
            raise AssertionError("must not parse a non-200 as an embedding")
        return {"data": [{"embedding": self._embedding}]}


class _RecordingClient:
    """Stand-in for httpx.AsyncClient; records POST bodies in `posts`."""

    def __init__(self, posts, handler, **_kwargs):
        self._posts = posts
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, url, headers=None, json=None, timeout=None):
        self._posts.append(json)
        return self._handler(json, len(self._posts))


def _run_embed(text, handler, monkeypatch, embed_max_chars=None):
    posts = []
    if embed_max_chars is not None:
        monkeypatch.setattr(cl, "EMBED_MAX_CHARS", embed_max_chars)
    monkeypatch.setattr(
        cl.httpx, "AsyncClient",
        lambda **kw: _RecordingClient(posts, handler, **kw),
    )
    result = asyncio.run(ConsolidationDaemon.get_embedding(None, text))
    return result, posts


def test_get_embedding_overrun_400_snaps_and_returns_1024(monkeypatch, caplog):
    """Slack-overrun 400 retries shorter (reserved clamp) and returns 1024-dim."""
    caplog.set_level(logging.ERROR)

    def handler(body, n):
        if n == 1:
            return _FakeResp(400, OVERRUN_BODY)
        return _FakeResp(200, embedding=VEC_1024)

    original = "x" * 24576
    result, posts = _run_embed(original, handler, monkeypatch, embed_max_chars=24576)

    assert result == VEC_1024
    assert len(result) == 1024
    assert len(posts) == 2
    assert len(posts[0]["input"]) == 24576
    assert len(posts[1]["input"]) == RESERVED_CLAMP
    assert len(posts[1]["input"]) < len(posts[0]["input"])
    # Caller's summary is not rewritten (vector only).
    assert len(original) == 24576
    assert all(len(p["input"]) > 1 for p in posts)


def test_get_embedding_overrun_413_snaps_and_returns_1024(monkeypatch):
    """Proxy/backend may 413 on window overrun — same snap as 400."""

    def handler(body, n):
        if n == 1:
            return _FakeResp(413, OVERRUN_BODY)
        return _FakeResp(200, embedding=VEC_1024)

    result, posts = _run_embed("x" * 24576, handler, monkeypatch, embed_max_chars=24576)

    assert result == VEC_1024
    assert len(result) == 1024
    assert len(posts) == 2
    assert len(posts[1]["input"]) == RESERVED_CLAMP


def test_get_embedding_mismatch_does_not_truncate(monkeypatch, caplog):
    """Short advertised window: named log, return None, no silent prefix retry."""
    caplog.set_level(logging.ERROR)

    def handler(body, n):
        return _FakeResp(400, MISMATCH_BODY)

    result, posts = _run_embed("x" * 2000, handler, monkeypatch)

    assert result is None
    assert len(posts) == 1
    assert len(posts[0]["input"]) == 2000
    joined = caplog.text.lower()
    assert "mismatch" in joined
    assert "none" in joined or "returning none" in joined or "will not" in joined


def test_get_embedding_floor_without_200_returns_none_not_one_char(monkeypatch, caplog):
    """Reserved clamp still 400 → None. No 1-char garbage, no per-char HTTP spam."""
    caplog.set_level(logging.ERROR)

    def handler(body, n):
        return _FakeResp(400, OVERRUN_BODY)

    result, posts = _run_embed("x" * 24576, handler, monkeypatch, embed_max_chars=24576)

    assert result is None
    assert 1 <= len(posts) <= 4
    posted = [len(p["input"]) for p in posts]
    assert min(posted) > 1
    assert 1 not in posted
    # At least one snap to the reserved clamp, never a walk down to 1 char.
    assert RESERVED_CLAMP in posted or posted[-1] <= RESERVED_CLAMP
    assert min(posted) >= RESERVED_CLAMP - 16


def test_get_embedding_other_4xx_stays_none_no_snap(monkeypatch):
    def handler(body, n):
        return _FakeResp(401, "unauthorized")

    result, posts = _run_embed("hello", handler, monkeypatch)
    assert result is None
    assert len(posts) == 1


def test_get_embedding_classifies_400_and_413_in_source():
    """Mutation: skip classify → 400 → None on overrun (this source pin fails)."""
    src = inspect.getsource(ConsolidationDaemon.get_embedding)
    assert "classify_overflow" in src
    assert "400" in src
    assert "413" in src
    assert "reserved" in src.lower() or "reserved_clamp" in src
