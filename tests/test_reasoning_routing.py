"""Gateway walk and backlog retry rules.

Each test dies if its guard is removed: a cut-off stored, a loop moved, a
fault that should stay put, a second try of the same backend, a busy local
sent to a paid host, an extra_body spend cap, a too-small context, a retry
with the same pool or context, a move while LLM_MOVE_ON_DOWN is false, a
priority inversion, a missing log line, a key on the wrong backend, a key in
a specimen, or a seq-rep-4 score on the wrong side of 0.5.
"""
import asyncio
import importlib
import json
import os
import sys

import aiohttp
import pytest
from aiohttp.client_reqrep import ConnectionKey
from yarl import URL

pytestmark = pytest.mark.usefixtures("chat_caller_is_consolidation")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import request_sizing


def _fresh(monkeypatch, backends):
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(backends))
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    return g


def _backend(url, *, model, n_ctx, max_output, priority=1, token_env=None, extra=None,
             max_inflight=None):
    row = {
        "url": url,
        "model": model,
        "n_ctx": n_ctx,
        "max_output_tokens": max_output,
        "priority": priority,
        "private_ok": True,
    }
    if max_inflight is not None:
        row["max_inflight"] = max_inflight
    if token_env:
        row["token_env"] = token_env
    if extra:
        row["extra_body"] = extra
    return row


LOCAL = "http://127.0.0.1:9101"
PAID = "https://llm.example.test/v1"
MID = "http://127.0.0.1:9102"


class _Upstream:
    def __init__(self, status, body):
        self.status = status
        self.headers = {}
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.content = self

    async def read(self, n=-1):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class _Session:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append({"url": kwargs.get("url"), "headers": kwargs.get("headers"), "data": kwargs.get("data")})
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _req(text="hello"):
    raw = json.dumps({"messages": [{"role": "user", "content": text}], "model": "local-model"}).encode()

    class _R(dict):
        method = "POST"
        path = "/v1/chat/completions"
        rel_url = URL("/v1/chat/completions", encoded=True)
        headers = {}
        can_read_body = True

        async def read(self):
            return raw

    return _R()


def _stop(text="done"):
    return _Upstream(200, {"choices": [{"finish_reason": "stop", "message": {"content": text}}], "usage": {"completion_tokens": 2}})


def _length(text):
    return _Upstream(200, {"choices": [{"finish_reason": "length", "message": {"content": text}}], "usage": {"completion_tokens": 9}})


def _context():
    return _Upstream(400, {"error": {"type": "exceed_context_size_error", "message": "over"}})


def _status(code, type_="transient", usage=None):
    body = {"error": {"type": type_, "message": "no"}}
    if usage is not None:
        body["usage"] = usage
    return _Upstream(code, body)


def _down():
    key = ConnectionKey("127.0.0.1", 9, False, False, None, None, None)
    return aiohttp.ClientConnectorError(key, OSError(111, "refused"))


def _urls(session):
    return [str(c["url"]) for c in session.calls]


def _body(resp):
    return json.loads(resp.body.decode())


def test_seq_rep_4_states_the_measured_line():
    """fact:3396: a loop scores near 0.99 and is flagged; honest text near 0.01 is not."""
    loop = ("alpha beta gamma delta " * 200).split()
    loop_score = request_sizing.seq_rep_4(" ".join(loop))
    assert loop_score > 0.99
    assert request_sizing.reply_is_loop(" ".join(loop)) is True
    words = [f"w{i}" for i in range(103)] + ["w0", "w1", "w2", "w3"]
    honest = request_sizing.seq_rep_4(" ".join(words))
    assert 0.005 < honest < 0.02
    assert request_sizing.reply_is_loop(" ".join(words)) is False


def test_cut_off_does_not_reach_the_daemon(monkeypatch):
    g = _fresh(monkeypatch, [_backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=256)])
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_length("unique honest tail that is not a loop at all")])
    proxy.session = session
    proxy.llm_session = proxy.session
    resp = asyncio.run(proxy.handle_proxy(_req()))
    assert resp.status == 422
    body = _body(resp)
    assert body["error"] == "backlog" and body["reason"] == "cut_off"
    assert "choices" not in body


def test_loop_does_not_move(monkeypatch):
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=256),
        _backend(PAID, model="deepseek-flash", n_ctx=32768, max_output=4096, token_env="ROUTE_KEY_B"),
    ])
    monkeypatch.setenv("ROUTE_KEY_B", "sk-route-b")
    importlib.reload(sys.modules["hive_mind_proxy"])
    g = sys.modules["hive_mind_proxy"]
    proxy = g.AsyncHiveMindProxy()
    loop = "alpha beta gamma delta " * 40
    session = _Session([_length(loop), _stop()])
    proxy.session = session
    proxy.llm_session = proxy.session
    resp = asyncio.run(proxy.handle_proxy(_req()))
    assert _body(resp)["reason"] == "loop"
    assert len(session.calls) == 1


@pytest.mark.parametrize("code", [401, 403, 500])
def test_auth_and_server_do_not_move(monkeypatch, code):
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=512),
        _backend(MID, model="deepseek-flash", n_ctx=32768, max_output=4096),
    ])
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_status(code), _stop()])
    proxy.session = session
    proxy.llm_session = proxy.session
    resp = asyncio.run(proxy.handle_proxy(_req()))
    assert resp.status == code
    assert _body(resp)["error"] == "upstream_fault"
    assert len(session.calls) == 1


def test_unrecognised_400_does_not_move(monkeypatch):
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=512),
        _backend(MID, model="deepseek-flash", n_ctx=32768, max_output=4096),
    ])
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_status(400, "invalid_request_error"), _stop()])
    proxy.session = session
    proxy.llm_session = proxy.session
    resp = asyncio.run(proxy.handle_proxy(_req()))
    assert resp.status == 400
    assert len(session.calls) == 1


def test_walk_never_retries_a_backend(monkeypatch):
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=256),
        _backend(MID, model="deepseek-flash", n_ctx=32768, max_output=4096),
    ])
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_down(), _stop("kept")])
    proxy.session = session
    proxy.llm_session = proxy.session
    resp = asyncio.run(proxy.handle_proxy(_req()))
    assert resp.status == 200
    assert len(_urls(session)) == len(set(_urls(session))) == 2


def test_busy_local_does_not_move_to_paid(monkeypatch):
    g = _fresh(monkeypatch, [
        {**_backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=512), "max_inflight": 1},
        _backend(PAID, model="deepseek-flash", n_ctx=32768, max_output=4096, token_env="ROUTE_KEY_B"),
    ])
    monkeypatch.setenv("ROUTE_KEY_B", "sk-route-b")
    g = importlib.reload(g)
    g.LLM_MAX_INFLIGHT_POLL_S = 0.01
    g._llm_inflight[LOCAL] = 1
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_stop()])
    proxy.session = session
    proxy.llm_session = proxy.session

    async def _free():
        await asyncio.sleep(0.03)
        g._llm_inflight[LOCAL] = 0

    async def _run():
        task = asyncio.create_task(_free())
        resp = await proxy.handle_proxy(_req())
        await task
        return resp

    resp = asyncio.run(_run())
    assert resp.status == 200
    assert len(session.calls) == 1
    assert "9101" in _urls(session)[0]
    assert "example.test" not in _urls(session)[0]


def test_extra_body_cannot_override_max_tokens(monkeypatch):
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=512, extra={"max_tokens": 1, "temperature": 0}),
    ])
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_stop()])
    proxy.session = session
    proxy.llm_session = proxy.session
    asyncio.run(proxy.handle_proxy(_req()))
    sent = json.loads(session.calls[0]["data"].decode())
    assert sent["max_tokens"] != 1
    assert isinstance(sent["max_tokens"], int)
    assert sent["temperature"] == 0
    assert list(sent)[-1] == "max_tokens"


def test_backend_with_room_below_the_estimate_is_not_tried(monkeypatch):
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=100, max_output=80),
        _backend(MID, model="deepseek-flash", n_ctx=32768, max_output=4096),
    ])
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_stop(), _stop()])
    proxy.session = session
    proxy.llm_session = proxy.session
    resp = asyncio.run(proxy.handle_proxy(_req("y" * 5000)))
    assert len(session.calls) == 1
    assert MID.split("://", 1)[1].split("/")[0] in _urls(session)[0] or "9102" in _urls(session)[0]
    assert resp.status == 200


def test_no_context_does_not_retry_on_an_unrelated_pool_change():
    import llm_backlog
    ident = {"pool_id": "new-pool", "largest_ctx": 8192, "largest_reply": 1000, "boot_id": "boot"}
    assert llm_backlog.eligible("no_context", 8192, 100, "old-pool", "boot", ident) is False
    assert llm_backlog.eligible("no_context", 4096, 100, "old-pool", "boot", ident) is True
    assert llm_backlog.eligible("cut_off", 8192, 1000, "old-pool", "other-boot", ident) is False
    grown = dict(ident, largest_reply=2000)
    assert llm_backlog.eligible("cut_off", 8192, 1000, "same", "boot", grown) is True
    assert llm_backlog.eligible("loop", 1, 1, "new-pool", "boot", ident) is False
    assert llm_backlog.eligible("loop", 1, 1, "old-pool", "boot", ident) is True
    assert llm_backlog.eligible("connection_lost", 1, 1, "new-pool", "boot", ident) is False
    assert llm_backlog.eligible("write_failed", 1, 1, "p", "other", ident) is True


def test_move_on_down_false_stays_put(monkeypatch):
    monkeypatch.setenv("LLM_MOVE_ON_DOWN", "false")
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=512),
        _backend(MID, model="deepseek-flash", n_ctx=32768, max_output=4096),
    ])
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_down(), _stop()])
    proxy.session = session
    proxy.llm_session = proxy.session
    resp = asyncio.run(proxy.handle_proxy(_req()))
    assert resp.status == 503
    assert len(session.calls) == 1


def test_priority_then_smaller_context_first(monkeypatch):
    """Priority wins over n_ctx. Ignoring priority tries :9103 first."""
    g = _fresh(monkeypatch, [
        _backend("http://127.0.0.1:9103", model="late", n_ctx=3000, max_output=256, priority=2),
        _backend(MID, model="mid", n_ctx=9000, max_output=1024, priority=1),
        _backend(LOCAL, model="early", n_ctx=5000, max_output=256, priority=1),
    ])
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_context(), _stop("ok")])
    proxy.session = session
    proxy.llm_session = proxy.session
    resp = asyncio.run(proxy.handle_proxy(_req()))
    assert resp.status == 200
    hosts = _urls(session)
    assert len(hosts) == 2
    assert "9101" in hosts[0] and "9102" in hosts[1]


def test_escalation_log_lines(monkeypatch, caplog):
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=128),
        _backend(MID, model="deepseek-flash", n_ctx=32768, max_output=4096),
    ])
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_context(), _stop("ok")])
    proxy.session = session
    proxy.llm_session = proxy.session
    with caplog.at_level("WARNING"):
        asyncio.run(proxy.handle_proxy(_req()))
    assert "Escalating to model deepseek-flash due to context length exceeded at model shared-backend" in caplog.text

    session = _Session([_length("short unique cut off text here now"), _stop("ok")])
    proxy.session = session
    proxy.llm_session = proxy.session
    with caplog.at_level("WARNING"):
        asyncio.run(proxy.handle_proxy(_req()))
    assert "Escalating to model deepseek-flash due to truncated reply at model shared-backend" in caplog.text

    session = _Session([_down(), _stop("ok")])
    proxy.session = session
    proxy.llm_session = proxy.session
    with caplog.at_level("WARNING"):
        asyncio.run(proxy.handle_proxy(_req()))
    assert "Moving to model deepseek-flash because model shared-backend is down" in caplog.text

    session = _Session([_status(429), _stop("ok")])
    proxy.session = session
    proxy.llm_session = proxy.session
    with caplog.at_level("WARNING"):
        asyncio.run(proxy.handle_proxy(_req()))
    assert "Moving to model deepseek-flash because model shared-backend is rate limited" in caplog.text


def test_each_try_carries_only_its_own_key(monkeypatch):
    monkeypatch.setenv("ROUTE_KEY_A", "sk-route-a")
    monkeypatch.setenv("ROUTE_KEY_B", "sk-route-b")
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=128, token_env="ROUTE_KEY_A"),
        _backend(MID, model="deepseek-flash", n_ctx=32768, max_output=4096, token_env="ROUTE_KEY_B"),
    ])
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_context(), _stop("ok")])
    proxy.session = session
    proxy.llm_session = proxy.session
    asyncio.run(proxy.handle_proxy(_req()))
    first = session.calls[0]["headers"].get("Authorization")
    second = session.calls[1]["headers"].get("Authorization")
    assert first == "Bearer sk-route-a"
    assert second == "Bearer sk-route-b"
    assert "sk-route-b" not in (first or "")


def test_tokenless_backend_gets_no_authorization(monkeypatch):
    monkeypatch.setenv("ROUTE_KEY_B", "sk-route-b")
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=512),
        _backend(MID, model="deepseek-flash", n_ctx=32768, max_output=4096, token_env="ROUTE_KEY_B"),
    ])
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_stop("ok")])
    proxy.session = session
    proxy.llm_session = proxy.session
    asyncio.run(proxy.handle_proxy(_req()))
    assert "Authorization" not in {k.title(): v for k, v in session.calls[0]["headers"].items()}
    assert "authorization" not in {k.lower() for k in session.calls[0]["headers"]}


def test_specimen_and_log_do_not_carry_a_key(monkeypatch, caplog):
    monkeypatch.setenv("ROUTE_KEY_A", "sk-route-a")
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=256, token_env="ROUTE_KEY_A"),
    ])
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_length("preamble sk-route-a trailer")])
    proxy.session = session
    proxy.llm_session = proxy.session
    with caplog.at_level("WARNING"):
        resp = asyncio.run(proxy.handle_proxy(_req()))
    dumped = resp.body.decode() + caplog.text
    assert "sk-route-a" not in dumped
    assert "[redacted]" in _body(resp)["specimen"]


@pytest.mark.asyncio
async def test_daemon_cut_off_is_not_stored(monkeypatch):
    """Solo, batch, and fold each discard a length finish. Nothing is parsed."""
    import rem_loop
    from unittest.mock import patch

    daemon = rem_loop.REMDaemon.__new__(rem_loop.REMDaemon)
    monkeypatch.delenv("MOCK_LLM", raising=False)
    long = "x" * (2001)

    class _R:
        status_code = 200
        headers = {}
        text = ""

        def json(self):
            return {"choices": [{"finish_reason": "length", "message": {"content": '{"summary":"no"}'}}]}

    async def _post(self, url, **kwargs):
        return _R()

    monkeypatch.setattr("httpx.AsyncClient.post", _post)
    with patch.object(rem_loop, "_parse_llm_json") as parse:
        result, _model = await daemon._llm_process(long, rem_loop.KIND_FACT, pg_id=1)
    assert result is None
    parse.assert_not_called()

    items = [{"pg_id": 1, "content": long}]
    out, _timing, _model = await daemon._llm_process_batch(items)
    assert 1 not in out
    assert daemon._batch_action == "solo"

    import consolidation_loop as cl
    fold = cl.ConsolidationDaemon()

    async def _post_nrem(client, payload, ceiling_s=None, prompt_chars=0):
        return _R()

    monkeypatch.setattr(cl, "_post_nrem", _post_nrem)
    text = await fold._call_insight_llm("prompt", "Entity", units=1, items=[])
    assert text is None
    assert fold._last_backlog_reason == "cut_off"


def _run(monkeypatch, script, backends=None):
    g = _fresh(monkeypatch, backends or [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=512),
        _backend(MID, model="deepseek-flash", n_ctx=32768, max_output=4096),
    ])
    proxy = g.AsyncHiveMindProxy()
    session = _Session(script)
    proxy.session = session
    proxy.llm_session = session
    resp = asyncio.run(proxy.handle_proxy(_req()))
    return g, session, resp


def test_unpaid_503_then_503_queues_without_a_backlog(monkeypatch):
    """An HTTP 503 does not move, and it is not a paid reply."""
    _g, session, resp = _run(monkeypatch, [_status(503), _status(503)])
    assert len(session.calls) == 1
    assert resp.status == 503
    assert _body(resp).get("error") != "backlog"


def test_unpaid_429_then_429_queues_without_a_backlog(monkeypatch):
    """A 429 moves. Neither reply is paid, so the walk queues."""
    _g, session, resp = _run(monkeypatch, [_status(429), _status(429)])
    assert len(session.calls) == 2
    assert resp.status == 503
    assert _body(resp).get("error") != "backlog"


def test_free_context_then_503_queues_without_a_backlog(monkeypatch):
    """A context refusal with no usage is free. The following 503 stays queued."""
    _g, session, resp = _run(monkeypatch, [_context(), _status(503)])
    assert len(session.calls) == 2
    assert resp.status == 503
    assert _body(resp).get("error") != "backlog"


def test_paid_cut_off_then_503_backlogs_connection_lost(monkeypatch):
    _g, session, resp = _run(monkeypatch, [
        _length("one two three four five six seven eight"),
        _status(503),
    ])
    assert len(session.calls) == 2
    assert resp.status == 422
    assert _body(resp)["reason"] == "connection_lost"


@pytest.mark.parametrize("code", [501, 520, 524])
def test_every_5xx_stays_put(monkeypatch, code):
    _g, session, resp = _run(monkeypatch, [_status(code), _stop()])
    assert len(session.calls) == 1
    assert resp.status == code
    assert _body(resp).get("error") != "backlog"


def test_self_hosted_503_does_not_move(monkeypatch):
    monkeypatch.setenv("LLM_MOVE_ON_DOWN", "true")
    _g, session, resp = _run(monkeypatch, [_status(503), _stop()])
    assert len(session.calls) == 1
    assert resp.status == 503


def test_refused_connection_still_moves(monkeypatch):
    monkeypatch.setenv("LLM_MOVE_ON_DOWN", "true")
    _g, session, resp = _run(monkeypatch, [_down(), _stop()])
    assert _urls(session) == [f"{LOCAL}/v1/chat/completions", f"{MID}/v1/chat/completions"]
    assert resp.status == 200


def test_choices_object_is_unparseable_not_a_500(monkeypatch):
    body = {"choices": {"finish_reason": "stop", "message": {"content": "x"}}}
    _g, _session, resp = _run(monkeypatch, [_Upstream(200, body)])
    assert resp.status == 422
    assert _body(resp)["reason"] == "unparseable"


def test_proxy_error_after_a_paid_reply_is_connection_lost(monkeypatch):
    _g, _session, resp = _run(monkeypatch, [
        _length("one two three four five"),
        RuntimeError("boom"),
    ])
    assert resp.status == 422
    assert _body(resp)["reason"] == "connection_lost"


def test_tokenless_try_clears_key_attached(monkeypatch):
    monkeypatch.setenv("ROUTE_KEY_A", "sk-route-a")
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=512, token_env="ROUTE_KEY_A"),
        _backend(MID, model="deepseek-flash", n_ctx=32768, max_output=4096),
    ])
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_context(), _stop()])
    proxy.session = session
    proxy.llm_session = session
    req = _req()
    req["key_attached"] = True
    resp = asyncio.run(proxy.handle_proxy(req))
    assert resp.status == 200
    assert "key_attached" not in req
    assert "Authorization" not in session.calls[1]["headers"]
    assert session.calls[0]["headers"]["Authorization"] == "Bearer sk-route-a"


def test_extra_body_cannot_set_max_completion_tokens(monkeypatch):
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=256,
                 extra={"max_completion_tokens": 3, "max_tokens": 4, "thinking": False}),
    ])
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_stop()])
    proxy.session = session
    proxy.llm_session = session
    asyncio.run(proxy.handle_proxy(_req("hello world")))
    sent = json.loads(session.calls[0]["data"])
    assert "max_completion_tokens" not in sent
    assert sent["max_tokens"] != 4
    assert sent["thinking"] is False
    assert isinstance(sent["max_tokens"], int)


def test_pool_id_includes_extra_body(monkeypatch):
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=256, extra={"z": 1, "a": 2}),
    ])
    first = g.llm_pool_id()
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=256, extra={"a": 2, "z": 1}),
    ])
    assert g.llm_pool_id() == first
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=256, extra={"a": 2, "z": 9}),
    ])
    assert g.llm_pool_id() != first


@pytest.mark.asyncio
async def test_llm_path_sends_accept_encoding_identity(monkeypatch):
    from aiohttp import web as aw

    seen = {}

    async def handler(request):
        seen["ae"] = request.headers.get("Accept-Encoding")
        return aw.json_response({
            "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
        })

    app = aw.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = aw.AppRunner(app)
    await runner.setup()
    site = aw.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    g = _fresh(monkeypatch, [
        _backend(f"http://127.0.0.1:{port}", model="shared-backend", n_ctx=8192, max_output=256),
    ])
    proxy = g.AsyncHiveMindProxy()
    await proxy.start_session()
    try:
        resp = await proxy.handle_proxy(_req())
    finally:
        await proxy.cleanup()
        await runner.cleanup()
    assert seen["ae"] == "identity"
    assert resp.status == 200


_USAGE = {"prompt_tokens": 3, "completion_tokens": 4}


def test_paid_503_moves_and_self_hosted_503_stays(monkeypatch):
    """A non-self-hosted 503 moves. A self-hosted 503 does not."""
    _g, session, resp = _run(monkeypatch, [_status(503), _stop()], [
        _backend(PAID, model="paid", n_ctx=8192, max_output=512),
        _backend(MID, model="mid", n_ctx=32768, max_output=4096),
    ])
    assert len(session.calls) == 2
    assert resp.status == 200

    _g, session, resp = _run(monkeypatch, [_status(503), _stop()])
    assert len(session.calls) == 1
    assert resp.status == 503


@pytest.mark.parametrize("code", [502, 504])
def test_502_and_504_move_including_self_hosted(monkeypatch, code):
    _g, session, resp = _run(monkeypatch, [_status(code), _stop()])
    assert len(session.calls) == 2
    assert resp.status == 200


@pytest.mark.parametrize("code", [500, 501, 520])
def test_other_5xx_stay_on_a_paid_backend(monkeypatch, code):
    _g, session, resp = _run(monkeypatch, [_status(code), _stop()], [
        _backend(PAID, model="paid", n_ctx=8192, max_output=512),
        _backend(MID, model="mid", n_ctx=32768, max_output=4096),
    ])
    assert len(session.calls) == 1
    assert resp.status == code
    assert _body(resp).get("error") != "backlog"


@pytest.mark.parametrize("code", [502, 503, 504])
def test_move_on_down_false_stays_on_gateway_5xx(monkeypatch, code):
    monkeypatch.setenv("LLM_MOVE_ON_DOWN", "false")
    _g, session, resp = _run(monkeypatch, [_status(code), _stop()], [
        _backend(PAID, model="paid", n_ctx=8192, max_output=512),
        _backend(MID, model="mid", n_ctx=32768, max_output=4096),
    ])
    assert len(session.calls) == 1
    assert resp.status != 200


@pytest.mark.parametrize("code", [500, 503, 429])
def test_usage_on_the_only_reply_is_connection_lost(monkeypatch, code):
    """This try's own usage is paid, even when nothing follows it."""
    _g, session, resp = _run(monkeypatch, [_status(code, usage=_USAGE)], [
        _backend(PAID, model="paid", n_ctx=8192, max_output=512),
    ])
    assert len(session.calls) == 1
    body = _body(resp)
    assert resp.status == 422
    assert body["reason"] == "connection_lost"
    assert body["usage"]["completion_tokens"] == 4


def test_500_with_usage_does_not_move(monkeypatch):
    _g, session, resp = _run(monkeypatch, [_status(500, usage=_USAGE), _stop()], [
        _backend(PAID, model="paid", n_ctx=8192, max_output=512),
        _backend(MID, model="mid", n_ctx=32768, max_output=4096),
    ])
    assert len(session.calls) == 1
    assert _body(resp)["reason"] == "connection_lost"


def test_redirect_is_unrecognised(monkeypatch):
    _g, _session, resp = _run(monkeypatch, [_status(302)], [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=256),
    ])
    assert resp.status == 302
    assert _body(resp).get("reason") != "unparseable"
    assert _body(resp).get("error") != "backlog"


def test_cut_off_room_floor_skips_a_smaller_spend_cap(monkeypatch):
    """A later backend with less reply room is not tried after a cut-off."""
    small = "http://127.0.0.1:9102"
    wide = "http://127.0.0.1:9103"
    _g, session, resp = _run(
        monkeypatch,
        [_length("one two three four five six seven"), _stop(), _stop()],
        [
            _backend(LOCAL, model="first", n_ctx=8000, max_output=4000),
            _backend(small, model="tight", n_ctx=9000, max_output=10),
            _backend(wide, model="wide", n_ctx=100000, max_output=8000),
        ],
    )
    hosts = _urls(session)
    assert "9101" in hosts[0]
    assert "9103" in hosts[1]
    assert all("9102" not in host for host in hosts)
    assert resp.status == 200


def test_no_model_has_enough_context_log_line(monkeypatch, caplog):
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=256),
    ])
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_context()])
    proxy.session = session
    proxy.llm_session = session
    with caplog.at_level("WARNING"):
        resp = asyncio.run(proxy.handle_proxy(_req()))
    assert resp.status == 422
    assert _body(resp)["reason"] == "no_context"
    assert "No model has enough context -- backlogged" in caplog.text


def test_paid_then_down_is_connection_lost(monkeypatch):
    _g, session, resp = _run(monkeypatch, [
        _length("one two three four five six"),
        _down(),
    ])
    assert len(session.calls) == 2
    assert resp.status == 422
    assert _body(resp)["reason"] == "connection_lost"


def test_paid_then_busy_backend_waits(monkeypatch):
    """A paid cut-off followed by a busy backend waits. It is not connection_lost."""
    monkeypatch.setenv("LLM_MAX_INFLIGHT_POLL_S", "0.01")
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="first", n_ctx=8000, max_output=128),
        _backend(MID, model="second", n_ctx=32768, max_output=4096, max_inflight=1),
    ])
    g._llm_inflight[MID] = 1
    proxy = g.AsyncHiveMindProxy()
    session = _Session([
        _length("one two three four five six seven"),
        _stop("kept"),
    ])
    proxy.session = session
    proxy.llm_session = session

    async def _free():
        await asyncio.sleep(0.03)
        g._llm_inflight[MID] = 0

    async def _run():
        task = asyncio.create_task(_free())
        resp = await proxy.handle_proxy(_req())
        await task
        return resp

    resp = asyncio.run(_run())
    assert len(session.calls) == 2
    assert resp.status == 200
    assert _body(resp).get("reason") != "connection_lost"


@pytest.mark.asyncio
async def test_llm_connector_force_close(monkeypatch):
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=256),
    ])
    proxy = g.AsyncHiveMindProxy()
    await proxy.start_session()
    try:
        assert proxy.llm_session.connector._force_close is True
        assert proxy.session.connector._force_close is False
        assert proxy.llm_session.timeout.connect is None
        assert proxy.session.timeout.connect == 5.0
    finally:
        await proxy.cleanup()


@pytest.mark.asyncio
async def test_fold_stop_without_string_content_is_gateway_422(monkeypatch):
    """A stop reply with no string content is unparseable at the gateway.

    The fold receives 422. A daemon that reclassified a 200 would not see that status.
    """
    from aiohttp import web as aw
    from unittest.mock import AsyncMock
    import consolidation_loop as cl
    sys.path.insert(0, os.path.dirname(__file__))
    from test_backlog_cycle_guard import _arm_backlog

    upstream_calls = []
    gateway_seen = []

    async def upstream(_request):
        upstream_calls.append(1)
        return aw.json_response({
            "choices": [{"finish_reason": "stop", "message": None}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        })

    up = aw.Application()
    up.router.add_post("/v1/chat/completions", upstream)
    up_runner = aw.AppRunner(up)
    await up_runner.setup()
    up_site = aw.TCPSite(up_runner, "127.0.0.1", 0)
    await up_site.start()
    up_port = up_site._server.sockets[0].getsockname()[1]
    g = _fresh(monkeypatch, [
        _backend(f"http://127.0.0.1:{up_port}/v1", model="shared-backend",
                 n_ctx=100000, max_output=4096),
    ])
    proxy = g.AsyncHiveMindProxy()
    await proxy.start_session()

    async def gateway(request):
        resp = await proxy.handle_proxy(request)
        gateway_seen.append((resp.status, _body(resp)))
        return resp

    gw = aw.Application()
    gw.router.add_post("/v1/chat/completions", gateway)
    gw_runner = aw.AppRunner(gw)
    await gw_runner.setup()
    gw_site = aw.TCPSite(gw_runner, "127.0.0.1", 0)
    await gw_site.start()
    gw_port = gw_site._server.sockets[0].getsockname()[1]
    try:
        mem = _arm_backlog(monkeypatch)
        monkeypatch.delenv("MOCK_LLM", raising=False)
        monkeypatch.setattr(
            cl, "REASONER_URL", f"http://127.0.0.1:{gw_port}/v1/chat/completions")
        ids = [245, 267]
        types = {245: "decision", 267: "decision"}

        class _Cur:
            def __init__(self):
                self.sql = ""
                self.rowcount = 0

            def execute(self, sql, params=None):
                self.sql = sql

            def fetchall(self):
                if "ORDER BY id" in self.sql:
                    return [
                        (245, "Decision A", "p", "decision", {}),
                        (267, "Decision B", "p", "decision", {}),
                    ]
                return []

            def fetchone(self):
                return None

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _Pg:
            def cursor(self):
                return _Cur()

            def commit(self):
                return None

            def rollback(self):
                return None

            def close(self):
                return None

        monkeypatch.setattr(cl.psycopg2, "connect", lambda *a, **k: _Pg())
        monkeypatch.setattr(cl, "fetch_unreconciled_insights", lambda conn: [])
        monkeypatch.setattr(cl, "fetch_open_retro_record_ids", lambda conn: [])
        monkeypatch.setattr(cl, "fetch_refold_insights", lambda conn, ids: [])
        monkeypatch.setattr(cl, "fetch_active_insight_rows", lambda conn: [])
        monkeypatch.setattr(cl, "fetch_active_thematic_summary_id", lambda conn, p, d: None)
        monkeypatch.setattr(cl, "_fetch_outbox_created_at", lambda ids: {})
        monkeypatch.setattr(cl, "_crun_start", lambda ct: 42)
        monkeypatch.setattr(cl, "_crun_finish", lambda *a, **k: None)
        monkeypatch.setattr(cl, "fetch_reversal_context", lambda conn, ids: [])
        monkeypatch.setattr(cl, "fetch_insight_outbox_rows", lambda conn, ids: [])
        daemon = cl.ConsolidationDaemon()

        class _Ready:
            def session(self):
                return self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def run(self, query, **params):
                ids = list(params.get("ids") or [])
                class _R:
                    async def data(_self):
                        return [
                            {"pg_id": i, "rem_processed": True,
                             "superseded": False, "rem_invalid": False,
                             "labels": ["Fact"]}
                            for i in ids
                        ]
                return _R()

        daemon.driver = _Ready()
        daemon._find_fresh_insight_clusters = AsyncMock(
            return_value=[{
                "entity": "OutboxPattern",
                "decision_ids": ids,
                "judgement_ids": ids,
                "judgement_types": types,
                "projects": ["shared-memory-GitHub"],
                "domain": "architecture",
            }])
        await daemon.run_insight_cycle()
        await daemon.run_insight_cycle()
        key = cl._fold_backlog_key(ids)
        assert upstream_calls == [1]
        assert len(gateway_seen) == 1
        assert gateway_seen[0][0] == 422
        assert gateway_seen[0][1]["error"] == "backlog"
        assert gateway_seen[0][1]["reason"] == "unparseable"
        assert mem.rows[("fold", key)][0] == "unparseable"
    finally:
        await proxy.cleanup()
        await gw_runner.cleanup()
        await up_runner.cleanup()


@pytest.mark.parametrize("code", [402, 404, 413])
def test_free_4xx_queues_and_does_not_move(monkeypatch, code):
    _g, session, resp = _run(monkeypatch, [_status(code), _stop()])
    assert len(session.calls) == 1
    assert resp.status == code
    assert _body(resp)["error"] == "upstream_fault"
    assert _body(resp).get("reason") != "connection_lost"


@pytest.mark.parametrize("code", [402, 404, 413])
def test_4xx_with_usage_is_connection_lost(monkeypatch, code):
    _g, session, resp = _run(
        monkeypatch, [_status(code, usage={"completion_tokens": 4}), _stop()])
    assert len(session.calls) == 1
    assert resp.status == 422
    assert _body(resp)["reason"] == "connection_lost"
    assert _body(resp)["usage"]["completion_tokens"] == 4


def test_html_200_queues_without_a_row(monkeypatch):
    page = _Upstream(200, b"<html>maintenance</html>")
    page.headers["Content-Type"] = "text/html"
    _g, session, resp = _run(monkeypatch, [page, _stop()])
    assert len(session.calls) == 1
    assert resp.status == 503
    body = _body(resp)
    assert body["error"] == "upstream_fault"
    assert body["type"] == "queued"


def test_json_200_without_choices_is_unparseable(monkeypatch):
    _g, _session, resp = _run(monkeypatch, [_Upstream(200, {"usage": {"completion_tokens": 1}})])
    assert resp.status == 422
    assert _body(resp)["reason"] == "unparseable"


def test_prompt_echo_of_context_length_does_not_move(monkeypatch):
    echoed = {
        "error": {"type": "invalid_request_error", "message": "bad field"},
        "echo": "the prompt said maximum context length",
    }
    _g, session, resp = _run(monkeypatch, [_Upstream(400, echoed), _stop()])
    assert len(session.calls) == 1
    assert resp.status == 400


def test_context_length_in_the_error_message_moves(monkeypatch):
    body = {"error": {"type": "invalid_request_error", "message": "maximum context length exceeded"}}
    _g, session, resp = _run(monkeypatch, [_Upstream(400, body), _stop()])
    assert len(session.calls) == 2
    assert resp.status == 200


@pytest.mark.parametrize("usage", ["paid", 3, ["tok"], {}])
@pytest.mark.parametrize("code", [500, 503, 429])
def test_any_usage_shape_on_a_fault_is_paid(monkeypatch, usage, code):
    """A sole backend, so a 429 is the last reply. Self-hosted 503 does not move."""
    _g, session, resp = _run(
        monkeypatch,
        [_status(code, usage=usage)],
        [_backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=512)],
    )
    assert len(session.calls) == 1
    assert resp.status == 422
    assert _body(resp)["reason"] == "connection_lost"
    assert _body(resp)["usage"] == (usage if isinstance(usage, dict) else {})


def test_paid_cutoff_keeps_usage_and_specimen_after_reset(monkeypatch):
    text = "unique honest tail alpha beta"
    _g, session, resp = _run(monkeypatch, [
        _length(text),
        ConnectionResetError("reset"),
    ])
    assert len(session.calls) == 2
    body = _body(resp)
    assert body["reason"] == "connection_lost"
    assert body["usage"]["completion_tokens"] == 9
    assert "alpha beta" in body["specimen"]


def test_paid_cutoff_keeps_usage_and_specimen_after_free_500(monkeypatch):
    text = "unique honest tail alpha beta"
    _g, session, resp = _run(monkeypatch, [_length(text), _status(500)])
    assert len(session.calls) == 2
    body = _body(resp)
    assert body["reason"] == "connection_lost"
    assert body["usage"]["completion_tokens"] == 9
    assert "alpha beta" in body["specimen"]


def test_no_context_logs_the_retune_line(monkeypatch, caplog):
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=256),
    ])
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_context()])
    proxy.session = session
    proxy.llm_session = session
    with caplog.at_level("WARNING"):
        resp = asyncio.run(proxy.handle_proxy(_req()))
    assert resp.status == 422
    assert _body(resp)["reason"] == "no_context"
    fixed = [r.getMessage() for r in caplog.records if "No model has enough context" in r.getMessage()]
    detail = [r.getMessage() for r in caplog.records if "prompt estimate" in r.getMessage()]
    assert fixed == ["No model has enough context -- backlogged"]
    assert detail
    assert "LLM_CHARS_PER_TOKEN_RATIO" in detail[0]
    assert "FIT_MARGIN" in detail[0]
    assert "prompt estimate" not in fixed[0]


def test_specimen_redacts_a_key_before_the_cut(monkeypatch):
    monkeypatch.setenv("REM_TRUNCATION_SPECIMEN_CHARS", "6")
    monkeypatch.setenv("ROUTE_KEY_A", "sk-route-a")
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=256, token_env="ROUTE_KEY_A"),
    ])
    proxy = g.AsyncHiveMindProxy()
    session = _Session([_length("pad " * 40 + "sk-route-a")])
    proxy.session = session
    proxy.llm_session = session
    resp = asyncio.run(proxy.handle_proxy(_req()))
    specimen = _body(resp)["specimen"]
    assert "oute-a" not in specimen
    assert "sk-route" not in specimen


def test_client_cancel_is_not_a_latency_sample(monkeypatch):
    g = _fresh(monkeypatch, [
        _backend(LOCAL, model="shared-backend", n_ctx=8192, max_output=256),
    ])

    class _Hang:
        status = 200
        headers = {}
        content = None

        def __init__(self):
            self.content = self

        async def read(self, n=-1):
            await asyncio.Event().wait()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Sess:
        def request(self, *a, **k):
            return _Hang()

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _Sess()
    proxy.llm_session = proxy.session

    async def _run():
        task = asyncio.create_task(proxy.handle_proxy(_req()))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"
        return "finished"

    assert asyncio.run(_run()) == "cancelled"
    assert g._llm_latency_sum_s.get(LOCAL, 0.0) == 0.0
    assert g._llm_inflight.get(LOCAL, 0) == 0


def test_rem_attempts_is_not_read_or_written():
    import inspect
    import rem_loop
    import coordinator
    assert "rem_attempts" not in inspect.getsource(rem_loop)
    assert "rem_attempts" not in inspect.getsource(coordinator)
