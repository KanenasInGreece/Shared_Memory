"""F-GW-06 / F-GW-07: drain the S7 LLM probe with other_tasks, and treat
encoder health/capability 3xx as not-ok with allow_redirects=False.

aiohttp follows redirects by default; a 302 is also < 400, so omitting the
kwarg OR keeping `status < 400` both mark a redirect as healthy. These
tests default FakeSession allow_redirects to True so an omitted kwarg is
visible (same trick as test_probe_encoder_disallows_redirects).
"""
import asyncio
import inspect
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def _load(monkeypatch):
    import importlib
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    return g


# ── F-GW-06 ─────────────────────────────────────────────────────────────────

def test_main_drains_llm_probe_task(monkeypatch):
    """MUTATION: drop llm_probe_task from the drain other_tasks tuple.

    create_task alone is not enough — inspect the drain call so a leftover
    assignment in main() cannot satisfy this.
    """
    g = _load(monkeypatch)
    src = inspect.getsource(g.main)
    match = re.search(r"_drain_watchdogs_and_daemons\s*\((.*?)\)", src, re.DOTALL)
    assert match is not None, "main() must call _drain_watchdogs_and_daemons"
    drain_args = match.group(1)
    assert "llm_probe_task" in drain_args, (
        "llm_probe_task must be in the drain other_tasks tuple so the S7 "
        "probe is cancelled before proxy.cleanup() closes the session"
    )
    assert "capability_task" in drain_args
    assert "token_lifecycle_task" in drain_args


@pytest.mark.asyncio
async def test_drain_with_tokenless_other_task_does_not_crash(monkeypatch):
    """other_tasks are cancelled, never revoked per-task (agy KeyError was
    a contract misread — revoke is two named .pop(..., None) calls)."""
    g = _load(monkeypatch)

    async def _noop():
        return None

    t1 = asyncio.create_task(_noop())
    t2 = asyncio.create_task(_noop())
    extra = asyncio.create_task(_noop())
    await g._drain_watchdogs_and_daemons(t1, t2, (extra,))
    assert extra.done()


@pytest.mark.asyncio
async def test_drain_cancels_hanging_llm_probe_before_session_close(monkeypatch):
    """S7 probe uses proxy.session; drain must finish the task before close."""
    g = _load(monkeypatch)
    hang_entered = asyncio.Event()

    class _HangingCm:
        async def __aenter__(self):
            hang_entered.set()
            await asyncio.Event().wait()
            raise AssertionError("hanging get resumed without cancel")

        async def __aexit__(self, *a):
            return False

    class _Session:
        closed = False

        def get(self, *a, **k):
            return _HangingCm()

        async def close(self):
            self.closed = True

    class _Proxy:
        def __init__(self):
            self.session = _Session()

        async def cleanup(self):
            if self.session and not self.session.closed:
                await self.session.close()

    proxy = _Proxy()
    stop_event = asyncio.Event()
    llm_probe_task = asyncio.create_task(g._llm_probe_daemon(proxy, stop_event))
    await hang_entered.wait()
    stop_event.set()

    async def _noop():
        return None

    t1 = asyncio.create_task(_noop())
    t2 = asyncio.create_task(_noop())
    await asyncio.wait_for(
        g._drain_watchdogs_and_daemons(t1, t2, (llm_probe_task,)),
        timeout=5.0,
    )
    assert llm_probe_task.done(), "drain must cancel the hanging S7 probe"
    await proxy.cleanup()
    assert proxy.session.closed
    assert llm_probe_task.done()


# ── F-GW-07 ─────────────────────────────────────────────────────────────────

class _RedirectTrackingResp:
    def __init__(self, tracker, status=200):
        self._tracker = tracker
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self):
        self._tracker["read_calls"] += 1
        return b'{"results":[]}'

    async def json(self):
        self._tracker["json_calls"] += 1
        return {"results": [{"index": 0, "relevance_score": 1.0}],
                "data": [{"embedding": [0.0] * 8}]}


class _RedirectTrackingSession:
    """Default allow_redirects=True so omitting the kwarg is visible."""

    def __init__(self, status=200):
        self.calls = []
        self.tracker = {"read_calls": 0, "json_calls": 0}
        self._status = status

    def get(self, url, timeout=None, allow_redirects=True, **_kw):
        self.calls.append(("get", url, allow_redirects))
        return _RedirectTrackingResp(self.tracker, self._status)

    def post(self, url, json=None, timeout=None, allow_redirects=True, **_kw):
        self.calls.append(("post", url, allow_redirects))
        return _RedirectTrackingResp(self.tracker, self._status)


@pytest.mark.asyncio
async def test_health_and_capability_probes_disallow_redirects(monkeypatch):
    """MUTATION: drop allow_redirects=False on either health GET or
    capability POST — FakeSession defaults the kwarg to True."""
    g = _load(monkeypatch)
    session = _RedirectTrackingSession(status=200)

    class _Proxy:
        pass

    proxy = _Proxy()
    proxy.session = session
    await g._build_health_checks(proxy, None)
    health_calls = [c for c in session.calls if c[0] == "get"]
    assert health_calls, "encoder /health GETs must run"
    for method, url, allow_red in health_calls:
        assert allow_red is False, f"{method} {url} had allow_redirects={allow_red}"

    cap_session = _RedirectTrackingSession(status=200)
    await g._probe_capability(cap_session)
    post_calls = [c for c in cap_session.calls if c[0] == "post"]
    assert len(post_calls) == 2, "reranker and embedder POSTs must both run"
    for method, url, allow_red in post_calls:
        assert allow_red is False, f"{method} {url} had allow_redirects={allow_red}"


@pytest.mark.asyncio
async def test_health_encoder_302_is_not_ok(monkeypatch):
    """MUTATION: restore `status < 400` — a 302 is < 400 and would read as ok."""
    g = _load(monkeypatch)
    session = _RedirectTrackingSession(status=302)

    class _Proxy:
        pass

    proxy = _Proxy()
    proxy.session = session
    checks = await g._build_health_checks(proxy, None)
    assert checks["embedder"] == "http_302"
    assert checks["reranker"] == "http_302"
    assert checks["embedder"] != "ok"
    assert checks["reranker"] != "ok"


@pytest.mark.asyncio
async def test_capability_probe_302_is_not_ok_and_does_not_parse_json(monkeypatch):
    """A 302 body must not be parsed as JSON success, and must not count as serving."""
    g = _load(monkeypatch)
    session = _RedirectTrackingSession(status=302)
    out = await g._probe_capability(session)
    for backend in ("reranker", "embedder"):
        assert out[backend]["status"] == "failing"
        assert out[backend].get("serves_full_payload") is not True
    assert out["status"] == "degraded"
    assert session.tracker["json_calls"] == 0, (
        "capability must not .json() a 3xx body"
    )
