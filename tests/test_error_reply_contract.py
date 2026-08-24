"""The error-reply contract between the gateway and its two front doors.

THE MEASURED DEFECT (fact:1503). A postflight canary save by an agent holding a
VALID but READ-ONLY token was answered by the gateway with HTTP 403 and
aiohttp's plain-text page::

    403: Read-only token: this route requires a write-capable agent token

The CLI client guarded only ``status_code == 401`` before calling ``r.json()``,
so that page reached the decoder, raised ``JSONDecodeError: Extra data: line 1
column 4 (char 3)``, and was caught by the generic transport handler, which
reported::

    Memory coordinator unreachable at http://localhost:8888 — is
    hive_mind_proxy.py running?

An authorization refusal presented as a dead gateway. Three wrong diagnoses
followed.

It is a CLASS, not a 403 special case: ``json.loads`` of ANY aiohttp plain-text
``"NNN: reason"`` page raises the identical error, so EVERY status the site did
not enumerate fell into the decoder. v0.9.33 patched one call site; the class
shipped again. So this file pins the two halves of the rule:

  * the GATEWAY answers its auth/role refusals with the JSON error body the
    rest of the gateway already speaks — status codes, status lines and headers
    unchanged;
  * the CLIENTS decode ONLY after branching on the status class, through a
    single helper, so no reply that the gateway ANSWERED can ever be reported
    as an unreachable gateway.

Hermetic: a real loopback HTTP server for the client half (a real plain-text
body, real headers, a real refused connection), the middleware invoked directly
for the gateway half, and one end-to-end case that runs the real middleware
behind a real aiohttp server and drives the real client against it.
"""

import asyncio
import http.server
import importlib.util
import json
import os
import socket
import sys
import threading

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
_SCRIPTS = os.path.join(_ROOT, "shared-memory", "scripts")


def _load(name, *parts):
    path = os.path.normpath(os.path.join(_ROOT, *parts))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The FRAMEWORK copy — the file this change edits. (The shared-memory-skill
# copy is the merger's to sync; testing the copy would test the wrong artifact.)
memory_bridge = _load("memory_bridge_error_contract",
                      "shared-memory", "scripts", "memory_bridge.py")
vector_skill = _load("vector_skill_error_contract", "mcp", "vector-skill.py")


def load_coordinator(agent_tokens: str = "", agent_roles: str = ""):
    """Import coordinator.py with AGENT_TOKENS / AGENT_ROLES pre-set, one fresh
    module per test. Mirrors tests/test_auth.py's loader, including its
    secure_env cache reset — os.environ alone can no longer simulate "unset"
    once anything in this session has loaded a real .env."""
    if _SCRIPTS not in sys.path:
        sys.path.insert(0, _SCRIPTS)
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    if agent_tokens:
        os.environ["AGENT_TOKENS"] = agent_tokens
    else:
        os.environ.pop("AGENT_TOKENS", None)
    if agent_roles:
        os.environ["AGENT_ROLES"] = agent_roles
    else:
        os.environ.pop("AGENT_ROLES", None)
    path = os.path.join(_SCRIPTS, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator_error_contract", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── A loopback gateway that answers exactly what we tell it to ────────────────

class _StubGateway:
    """A real HTTP server on 127.0.0.1, so the body, the Content-Type and the
    status line are the real thing rather than a mock's attributes. A MagicMock
    cannot reproduce this defect: the defect IS a real plain-text page meeting a
    real JSON decoder."""

    def __init__(self, status: int, body: bytes, content_type: str,
                 headers: dict | None = None):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _serve(self):
                self.send_response(outer.status)
                self.send_header("Content-Type", outer.content_type)
                self.send_header("Content-Length", str(len(outer.body)))
                for k, v in (outer.headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(outer.body)

            do_GET = do_POST = _serve

            def log_message(self, *args):
                pass

        self.status, self.body, self.content_type, self.headers = (
            status, body, content_type, headers)
        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._srv.shutdown()
        self._srv.server_close()
        self._thread.join(timeout=5)


def _aiohttp_plaintext_page(status: int, reason: str) -> bytes:
    """aiohttp's own rendering of a bare HTTPException — the exact byte shape
    that reached json.loads and produced "Extra data: line 1 column 4"."""
    return f"{status}: {reason}".encode()


READ_ONLY_REASON = "Read-only token: this route requires a write-capable agent token"


@pytest.fixture(autouse=True)
def _force_tcp_and_no_token(monkeypatch):
    """Every client test talks TCP to the stub, with a known token state.
    COORDINATOR_UDS="" disables the Unix-socket auto-detect, which would
    otherwise hijack the connection on a machine where the real gateway is up.
    """
    monkeypatch.setenv("COORDINATOR_UDS", "")
    monkeypatch.setenv("AGENT_TOKEN", "tok_test_error_contract")
    monkeypatch.setattr(memory_bridge, "_CAPABILITY_CACHE", None, raising=False)
    monkeypatch.setattr(vector_skill, "_CAPABILITY_CACHE", None, raising=False)


def _point_client_at(monkeypatch, url, module=memory_bridge):
    monkeypatch.setattr(module, "COORDINATOR_BASE", url)


# ── CLIENT — the four status classes ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_plaintext_403_is_reported_as_an_authorization_refusal(monkeypatch):
    """THE regression case. The literal page that shipped, through the real
    save path, must come back naming the refusal — and must NOT name a dead
    gateway.

    Executes _reply_json's 403 branch.
    """
    with _StubGateway(403, _aiohttp_plaintext_page(403, READ_ONLY_REASON),
                      "text/plain; charset=utf-8") as gw:
        _point_client_at(monkeypatch, gw.url)
        result = await memory_bridge.save_artifact("canary", '{"source":"test"}')

    msg = result["message"]
    assert result["status"] == "error"
    assert "403" in msg
    assert "Read-only token" in msg, (
        "the gateway's own refusal must reach the operator verbatim — this is "
        "the sentence three wrong diagnoses were missing")
    assert "unreachable" not in msg.lower(), (
        "fact:1503: a live gateway refusing on authorization was reported as a "
        "dead one")
    assert "hive_mind_proxy" not in msg


@pytest.mark.asyncio
async def test_json_403_surfaces_the_gateways_own_message(monkeypatch):
    """The other half of the pair: once the gateway sends the JSON body this
    change adds, the client renders that message rather than a raw page.

    Executes _reply_json's 403 branch via _gateway_message.
    """
    body = json.dumps({"status": "error",
                       "message": "Read-only token: this route requires a "
                                  "write-capable agent token. The credential is VALID."}).encode()
    with _StubGateway(403, body, "application/json") as gw:
        _point_client_at(monkeypatch, gw.url)
        result = await memory_bridge.save_artifact("canary", '{"source":"test"}')

    assert "The credential is VALID" in result["message"]
    assert "unreachable" not in result["message"].lower()


@pytest.mark.asyncio
async def test_plaintext_503_names_the_status_and_never_says_unreachable(monkeypatch):
    """A quiesce/capacity 503 page is the same class as the 403 — proof the fix
    is a rule and not a 403 special case.

    Executes _reply_json's generic >= 400 branch.
    """
    with _StubGateway(503, _aiohttp_plaintext_page(503, "backup in progress"),
                      "text/plain; charset=utf-8") as gw:
        _point_client_at(monkeypatch, gw.url)
        result = await memory_bridge.save_artifact("canary", '{"source":"test"}')

    msg = result["message"]
    assert "503" in msg
    assert "backup in progress" in msg
    assert "unreachable" not in msg.lower()


@pytest.mark.asyncio
async def test_a_2xx_with_an_unparseable_body_names_a_LIVE_gateway(monkeypatch):
    """The one decode that survives: a 200 whose body is not JSON. That is a
    malformed reply from a running gateway — a different fault with a different
    fix from an unreachable one, so it must not borrow that message.

    Executes _reply_json's 2xx decode-failure branch.
    """
    with _StubGateway(200, b"<html>proxy interference</html>", "text/html") as gw:
        _point_client_at(monkeypatch, gw.url)
        result = await memory_bridge.save_artifact("canary", '{"source":"test"}')

    msg = result["message"]
    assert "unreachable" not in msg.lower()
    assert "LIVE" in msg or "live" in msg
    assert "malformed" in msg.lower()


@pytest.mark.asyncio
async def test_a_refused_connection_still_says_unreachable(monkeypatch):
    """The message _coordinator_unavailable exists for is still produced where
    it is TRUE. Narrowing it must not delete it.

    Executes _coordinator_unavailable's transport branch.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()

    _point_client_at(monkeypatch, f"http://127.0.0.1:{dead_port}")
    result = await memory_bridge.save_artifact("canary", '{"source":"test"}')

    assert "unreachable" in result["message"].lower()
    assert "hive_mind_proxy.py" in result["message"]


# ── CLIENT — the 401 wording is a regression pin, by VALUE ───────────────────
# fact:1309: an equality between two expressions is half a guard. These pin the
# literal sentences, so the pair cannot drift together.

_401_SENT = ("Coordinator rejected this agent's token. Check that AGENT_TOKEN "
             "in this agent's .env matches an entry in the gateway's AGENT_TOKENS.")
_401_NOT_SENT = ("No AGENT_TOKEN was sent and this gateway requires authentication. "
                 "Set AGENT_TOKEN in this agent's .env.")


@pytest.mark.asyncio
async def test_401_wording_unchanged_when_a_token_was_sent(monkeypatch):
    """Executes _reply_json's 401 branch, token-presented sub-branch."""
    with _StubGateway(401, _aiohttp_plaintext_page(401, "Authorization required"),
                      "text/plain; charset=utf-8",
                      {"WWW-Authenticate": 'Bearer error="invalid_token"'}) as gw:
        _point_client_at(monkeypatch, gw.url)
        monkeypatch.setenv("AGENT_TOKEN", "tok_presented")
        result = await memory_bridge.save_artifact("canary", '{"source":"test"}')

    assert result["message"] == _401_SENT


@pytest.mark.asyncio
async def test_401_wording_unchanged_when_no_token_was_sent(monkeypatch):
    """Executes _reply_json's 401 branch, nothing-presented sub-branch."""
    with _StubGateway(401, _aiohttp_plaintext_page(401, "Authorization required"),
                      "text/plain; charset=utf-8",
                      {"WWW-Authenticate": "Bearer"}) as gw:
        _point_client_at(monkeypatch, gw.url)
        monkeypatch.delenv("AGENT_TOKEN", raising=False)
        monkeypatch.setattr(memory_bridge, "_AGENT_TOKEN_FROM_FILE", "")
        result = await memory_bridge.save_artifact("canary", '{"source":"test"}')

    assert result["message"] == _401_NOT_SENT


# ── CLIENT — the rule holds at the other call sites, not just save ───────────

@pytest.mark.asyncio
async def test_search_403_is_not_reported_as_an_unreachable_gateway(monkeypatch):
    """search_and_rerank is a second call site with its own error path (it
    carries a ceiling). One helper, so one behaviour.

    Executes _reply_json's 403 branch from search_and_rerank.
    """
    with _StubGateway(403, _aiohttp_plaintext_page(403, READ_ONLY_REASON),
                      "text/plain; charset=utf-8") as gw:
        _point_client_at(monkeypatch, gw.url)
        result = await memory_bridge.search_and_rerank("anything")

    assert isinstance(result, dict) and result["status"] == "error"
    assert "Read-only token" in result["message"]
    assert "unreachable" not in result["message"].lower()


def test_telemetry_403_is_not_reported_as_an_unreachable_gateway(monkeypatch):
    """The SYNC client path (httpx.Client, not AsyncClient) goes through the
    same helper — a second transport is exactly where a per-site guard was
    forgotten before.

    Executes _reply_json's 403 branch from get_telemetry.
    """
    with _StubGateway(403, _aiohttp_plaintext_page(403, READ_ONLY_REASON),
                      "text/plain; charset=utf-8") as gw:
        _point_client_at(monkeypatch, gw.url)
        result = memory_bridge.get_telemetry()

    assert result["status"] == "error"
    assert "Read-only token" in result["message"]
    assert "unreachable" not in result["message"].lower()


def test_a_gateway_reply_can_never_be_labelled_unreachable(monkeypatch):
    """The structural guard. Even handed straight to the transport reporter —
    the shape a future call site would take if it forgot its own
    `except GatewayReplyError` — an ANSWERED reply keeps its own message.

    Executes _coordinator_unavailable's GatewayReplyError branch.
    """
    payload = {"status": "error", "message": "Gateway refused this request (HTTP 403)."}
    out = memory_bridge._coordinator_unavailable(
        memory_bridge.GatewayReplyError(payload))
    assert out == payload


# ── CLIENT — MCP front door (Group 1 parity) ─────────────────────────────────

@pytest.mark.asyncio
async def test_mcp_front_door_403_is_not_reported_as_an_unreachable_gateway(monkeypatch):
    """mcp/vector-skill.py carried the identical decode-before-status idiom at
    twelve sites. Group 1: two front doors, one gateway, one error contract.

    Executes vector-skill _reply_json's 403 branch.
    """
    with _StubGateway(403, _aiohttp_plaintext_page(403, READ_ONLY_REASON),
                      "text/plain; charset=utf-8") as gw:
        _point_client_at(monkeypatch, gw.url, module=vector_skill)
        result = await vector_skill.save_artifact(
            "canary", '{"source":"test-model","project":"shared-memory-GitHub"}')

    assert "Read-only token" in result
    assert "unreachable" not in result.lower()


# ── POSTFLIGHT — the operator's line actually carries the refusal ────────────

def test_postflight_a4_and_a5_print_the_bridge_message():
    """A4 and A5 render the client's own `message`, so the improvement reaches
    the operator without a change to the script. Pinned because a later edit
    that swapped either for a fixed string would silently re-hide the reason.
    """
    src = open(os.path.join(_ROOT, "shared-memory", "scripts", "postflight.sh"),
               encoding="utf-8").read()
    assert 'msg="$(printf \'%s\' "$save_out" | json_get message' in src, (
        "A4 no longer surfaces the gateway reply's message field")
    assert src.count('print("ERROR:" + str(d.get("message")') == 2, (
        "A5's two search-verdict readers must both surface the reply's message")
    assert 'bad A5 "search failed: ${verdict#ERROR:}"' in src


def test_the_403_message_survives_postflight_a5s_200_character_slice():
    """MEASURED, re-runnable. A5 slices a search error to 200 characters, so a
    preamble longer than that would restore the defect one level up: the
    operator sees an error and still not the reason. The gateway's own words
    are therefore front-loaded — this pins that they land inside the cut.

    Executes _reply_json's 403 branch via _gateway_message, on the body the
    deployed gateway now returns for a read-only refusal.
    """
    class _Reply:
        status_code = 403
        text = ""

        @staticmethod
        def json():
            return {"status": "error", "message": (
                "Read-only token: this route requires a write-capable agent token. The "
                "credential is VALID — it is confined to the read allowlist, so this is "
                "a role refusal, not an authentication failure.")}

    with pytest.raises(memory_bridge.GatewayReplyError) as caught:
        memory_bridge._reply_json(_Reply())
    message = caught.value.payload["message"]
    assert "write-capable agent token" in message[:200], (
        f"A5's 200-character slice would cut the refusal away: {message[:200]!r}")


# ── GATEWAY — the refusals carry a JSON body ─────────────────────────────────

def _forbidden_from(mod, path, token, method="POST"):
    """Drive auth_middleware and return the HTTPException it raised."""
    from unittest.mock import MagicMock
    req = MagicMock()
    req.path, req.method = path, method
    req.headers = {"Authorization": f"Bearer {token}"} if token else {}
    req.get = MagicMock(return_value=None)
    req.__setitem__ = MagicMock()

    async def handler(request):
        from aiohttp import web
        return web.json_response({"ok": True})

    from aiohttp import web
    with pytest.raises(web.HTTPException) as caught:
        asyncio.run(mod.auth_middleware(req, handler))
    return caught.value


def _assert_json_error_body(exc, *, status, must_say):
    """The documented shape: {"status": "error", "message": <str>} — the same
    body every json_response error in coordinator.py already returns."""
    assert exc.status == status
    assert exc.content_type == "application/json", (
        f"HTTP {status} still answers with a plain-text page; json.loads of it "
        f"raises JSONDecodeError, which is fact:1503")
    body = json.loads(exc.text)
    assert set(body) == {"status", "message"}
    assert body["status"] == "error"
    assert isinstance(body["message"], str) and body["message"]
    assert must_say in body["message"]


def test_read_only_403_answers_with_a_json_body(monkeypatch):
    mod = load_coordinator("monitor:tok_m", agent_roles="monitor:read")
    exc = _forbidden_from(mod, "/memory/save", "tok_m")
    _assert_json_error_body(exc, status=403, must_say="Read-only token")
    assert exc.reason == READ_ONLY_REASON, "the status line must not move"


def test_401_answers_with_a_json_body_and_keeps_its_headers(monkeypatch):
    mod = load_coordinator("claude:tok_abc")
    exc = _forbidden_from(mod, "/memory/save", "tok_wrong")
    _assert_json_error_body(exc, status=401, must_say="Bearer token")
    assert exc.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'
    assert exc.headers["X-SM-Fault-Origin"] == "gateway"


def test_401_without_a_token_keeps_the_bare_challenge(monkeypatch):
    mod = load_coordinator("claude:tok_abc")
    exc = _forbidden_from(mod, "/memory/save", None)
    _assert_json_error_body(exc, status=401, must_say="Bearer token")
    assert exc.headers["WWW-Authenticate"] == "Bearer"


def test_admin_confinement_403_answers_with_a_json_body(monkeypatch):
    mod = load_coordinator("backupbot:tok_b", agent_roles="backupbot:admin")
    exc = _forbidden_from(mod, "/memory/save", "tok_b")
    _assert_json_error_body(exc, status=403, must_say="confined to /admin/*")


def test_quiesce_503_answers_with_a_json_body_and_keeps_retry_after(monkeypatch):
    mod = load_coordinator("claude:tok_abc")
    mod._backup_quiesce = True
    exc = _forbidden_from(mod, "/memory/save", "tok_abc")
    _assert_json_error_body(exc, status=503, must_say="Backup in progress")
    assert exc.headers["Retry-After"] == str(mod.BACKUP_RETRY_AFTER)


def test_principal_required_403_answers_with_a_json_body(monkeypatch):
    mod = load_coordinator("claude:tok_abc")
    mod.GATEWAY_REQUIRE_PRINCIPAL = True
    exc = _forbidden_from(mod, "/memory/save", "tok_abc")
    _assert_json_error_body(exc, status=403, must_say="kernel-attested principal")


def test_capacity_503_answers_with_a_json_body(monkeypatch):
    mod = load_coordinator("claude:tok_abc")
    mod.GATEWAY_INFLIGHT_MAX = 1
    mod._inflight = 5
    exc = _forbidden_from(mod, "/memory/save", "tok_abc")
    _assert_json_error_body(exc, status=503, must_say="at capacity")
    assert exc.headers["Retry-After"] == "1"


# ── END TO END — the real middleware, a real socket, the real client ─────────

@pytest.mark.asyncio
async def test_a_read_only_token_save_reports_the_role_refusal_end_to_end(monkeypatch):
    """fact:1503's exact scenario, reproduced with no mocks in the path: a
    valid read-only token, the real auth middleware, a real aiohttp server on a
    real port, and the real client save.

    Green units proved nothing about this composition — the defect lived in the
    seam between a gateway that answered plain text and a client that decoded
    before branching. Executes the middleware's read-role 403 and the client's
    _reply_json 403 branch, in one pass.
    """
    from aiohttp import web

    mod = load_coordinator("monitor:tok_m", agent_roles="monitor:read")

    async def handler(request):
        return web.json_response({"status": "success", "pg_id": 1})

    app = web.Application(middlewares=[mod.auth_middleware])
    app.router.add_post("/memory/save", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        _point_client_at(monkeypatch, f"http://127.0.0.1:{port}")
        monkeypatch.setenv("AGENT_TOKEN", "tok_m")
        result = await memory_bridge.save_artifact("canary", '{"source":"test"}')
    finally:
        await runner.cleanup()

    msg = result["message"]
    assert result["status"] == "error"
    assert "Read-only token" in msg, (
        "the postflight operator must be told the token is read-only")
    assert "unreachable" not in msg.lower(), (
        "this is the sentence that produced three wrong diagnoses")
    assert "hive_mind_proxy" not in msg


# ── CLIENT — one refused call is ONE audit line (finding CQ-F1-01) ───────────
# Centralising the decode moved the 401 log inside _reply_json and routed the
# 401 through the same catch block as every other refusal, so a single rejected
# credential started writing two audit lines in the CLI client and a new one in
# the MCP client. The pre-change semantics are the contract: a 401 is ONE line,
# and every other class keeps the save_failed / save_rejected line — which is
# deliberate NEW signal, replacing a `coordinator_down` / `gateway_down` entry
# that was a lie about a gateway that had answered.


class _AuditRecorder:
    """Stands in for `_append_log` and keeps every event name it is handed.

    Deliberately records regardless of `min_level`: the question is which audit
    lines the code DECIDES to write, not which a particular MEMORY_LOG_LEVEL
    would let through, and counting the decisions is the stricter test.
    """

    def __init__(self):
        self.events: list[str] = []

    def __call__(self, tool, min_level, event, data, content=None):
        self.events.append(event)

    def count(self, event: str) -> int:
        return self.events.count(event)


@pytest.mark.asyncio
async def test_a_401_save_logs_auth_failed_once_and_never_save_failed(monkeypatch):
    """CLI front door. A rejected credential is ONE `auth_failed` line — the
    count is asserted BY VALUE, because "at least one" would pass on the
    double-log this pins against.

    Executes _reply_json's 401 branch and save_artifact's GatewayReplyError
    catch, with the status distinction carried on the exception.
    """
    recorder = _AuditRecorder()
    monkeypatch.setattr(memory_bridge, "_append_log", recorder)

    with _StubGateway(401, _aiohttp_plaintext_page(401, "Authorization required"),
                      "text/plain; charset=utf-8",
                      {"WWW-Authenticate": 'Bearer error="invalid_token"'}) as gw:
        _point_client_at(monkeypatch, gw.url)
        monkeypatch.setenv("AGENT_TOKEN", "tok_presented")
        result = await memory_bridge.save_artifact("canary", '{"source":"test"}')

    assert result["message"] == _401_SENT
    assert recorder.count("auth_failed") == 1, (
        f"a 401 must write exactly one auth_failed line, got {recorder.events}")
    assert recorder.count("save_failed") == 0, (
        f"a 401 already logged as auth_failed must NOT also log save_failed — "
        f"one refused save, one audit line: {recorder.events}")
    assert recorder.count("coordinator_down") == 0


@pytest.mark.asyncio
async def test_a_403_save_still_logs_save_failed(monkeypatch):
    """The other half of the ruling. 403 is NOT already logged, so it keeps its
    `save_failed` line — the signal that replaced the `coordinator_down` entry
    a role refusal used to produce. Removing the double-log must not take this
    with it.

    Executes _reply_json's 403 branch and save_artifact's catch.
    """
    recorder = _AuditRecorder()
    monkeypatch.setattr(memory_bridge, "_append_log", recorder)

    with _StubGateway(403, _aiohttp_plaintext_page(403, READ_ONLY_REASON),
                      "text/plain; charset=utf-8") as gw:
        _point_client_at(monkeypatch, gw.url)
        result = await memory_bridge.save_artifact("canary", '{"source":"test"}')

    assert "Read-only token" in result["message"]
    assert recorder.count("save_failed") == 1, (
        f"a 403 is new signal and must be recorded once: {recorder.events}")
    assert recorder.count("auth_failed") == 0, (
        "a 403 is an authorization refusal — logging it as an auth failure "
        "sends the operator to inspect the credential that was ACCEPTED")
    assert recorder.count("coordinator_down") == 0


@pytest.mark.asyncio
async def test_mcp_401_save_logs_auth_failed_once_and_never_save_rejected(monkeypatch):
    """MCP front door, Group 1 parity. Before the centralised decode this path
    returned early on 401 with `auth_failed` alone and no `save_rejected`; that
    is the semantics restored here.

    Executes vector-skill _reply_json's 401 branch and save_artifact's catch.
    """
    recorder = _AuditRecorder()
    monkeypatch.setattr(vector_skill, "_append_log", recorder)

    with _StubGateway(401, _aiohttp_plaintext_page(401, "Authorization required"),
                      "text/plain; charset=utf-8",
                      {"WWW-Authenticate": 'Bearer error="invalid_token"'}) as gw:
        _point_client_at(monkeypatch, gw.url, module=vector_skill)
        result = await vector_skill.save_artifact(
            "canary", '{"source":"test-model","project":"shared-memory-GitHub"}')

    assert "Error:" in result
    assert recorder.count("auth_failed") == 1, (
        f"a 401 must write exactly one auth_failed line, got {recorder.events}")
    assert recorder.count("save_rejected") == 0, (
        f"the pre-change 401 path logged no save_rejected at all: {recorder.events}")
    assert recorder.count("gateway_down") == 0


@pytest.mark.asyncio
async def test_mcp_403_save_still_logs_save_rejected(monkeypatch):
    """Executes vector-skill _reply_json's 403 branch and save_artifact's catch."""
    recorder = _AuditRecorder()
    monkeypatch.setattr(vector_skill, "_append_log", recorder)

    with _StubGateway(403, _aiohttp_plaintext_page(403, READ_ONLY_REASON),
                      "text/plain; charset=utf-8") as gw:
        _point_client_at(monkeypatch, gw.url, module=vector_skill)
        result = await vector_skill.save_artifact(
            "canary", '{"source":"test-model","project":"shared-memory-GitHub"}')

    assert "Read-only token" in result
    assert recorder.count("save_rejected") == 1, (
        f"a 403 is new signal and must be recorded once: {recorder.events}")
    assert recorder.count("auth_failed") == 0
    assert recorder.count("gateway_down") == 0


# ── CLIENT — the gateway's message is reflected, so it is bounded (SEC-F1-01) ─
# COORDINATOR_BASE is an env-overridable default: the endpoint whose `message`
# both clients splice into audit lines and terminal output is not axiomatically
# trusted. Cap by VALUE and strip the characters a terminal ACTS on.

class _JsonReply:
    """A 403 whose JSON body carries whatever message a test wants to reflect."""

    status_code = 403
    text = ""

    def __init__(self, message):
        self._message = message

    def json(self):
        return {"status": "error", "message": self._message}


@pytest.mark.parametrize("module", [memory_bridge, vector_skill],
                         ids=["cli", "mcp"])
def test_a_gateway_message_is_capped_at_600_characters(module):
    """The cap is pinned BY VALUE at both front doors. 600 is measured headroom
    — the longest message any deployed middleware refusal emits is 378
    characters — so a change to this number is a change to a measured claim and
    has to be argued, not absorbed.

    Executes _clean_gateway_text's truncation via _gateway_message.
    """
    assert module._GATEWAY_MESSAGE_MAX == 600
    out = module._gateway_message(_JsonReply("A" * 700))
    assert len(out) == 600, f"an over-long message reached the log at {len(out)} chars"
    assert out == "A" * 600
    # The boundary itself: a message exactly at the cap is NOT truncated.
    assert module._gateway_message(_JsonReply("B" * 600)) == "B" * 600
    # And the longest message a deployed refusal can produce survives whole.
    assert len(module._gateway_message(_JsonReply("C" * 378))) == 378


@pytest.mark.parametrize("module", [memory_bridge, vector_skill],
                         ids=["cli", "mcp"])
def test_control_characters_never_reach_the_operators_terminal(module):
    """A message printed to a terminal is not inert: ESC[2J clears the screen
    and BEL is not a diagnosis. Newline and tab stay — they are formatting a
    multi-line refusal legitimately uses.

    Executes _clean_gateway_text's control-character strip via _gateway_message.
    """
    out = module._gateway_message(
        _JsonReply("Read-only\x1b[2J\x07token\x00 refused\r\nline two\tcolumn"))

    assert "\x1b" not in out, "an ANSI escape reached the reflected message"
    assert "\x07" not in out
    assert "\x00" not in out
    assert "\r" not in out
    assert not any(ord(ch) < 32 and ch not in "\n\t" for ch in out), (
        f"a control character survived: {out!r}")
    assert "\x7f" not in out
    assert "Read-only" in out and "token" in out and "refused" in out
    assert "\n" in out and "\t" in out, (
        "newline and tab are formatting, not payload — stripping them would "
        "mangle a legitimate multi-line refusal")


@pytest.mark.parametrize("module", [memory_bridge, vector_skill],
                         ids=["cli", "mcp"])
def test_a_message_that_is_only_control_characters_is_no_message(module):
    """Cleaning can empty a string that was non-empty. The caller falls back to
    the body snippet then, rather than splicing "" in as the gateway's reason.

    Executes _gateway_message's post-clean emptiness branch.
    """
    assert module._gateway_message(_JsonReply("\x1b\x07\x00")) is None


def test_a_non_json_error_body_cannot_carry_an_ansi_escape_to_the_terminal():
    """_body_snippet is the same hazard as _gateway_message one path over: a
    NON-JSON error page is gateway-controlled text headed for a terminal, and
    str.split() collapses whitespace without touching ESC or BEL. Mutation
    target: removing _clean_gateway_text from _body_snippet must kill this."""
    import memory_bridge as mb

    class _R:
        status_code = 502
        text = "bad \x1b[2Jgateway\x07 page"
        def json(self):
            raise ValueError("not json")

    try:
        mb._reply_json(_R())
        raise AssertionError("expected GatewayReplyError")
    except mb.GatewayReplyError as exc:
        msg = exc.payload["message"]
    assert "\x1b" not in msg and "\x07" not in msg
    assert "bad" in msg and "gateway" in msg


def test_mcp_a_non_json_error_body_cannot_carry_an_ansi_escape():
    import importlib.util, pathlib
    spec = importlib.util.find_spec("vector_skill") if False else None
    import sys
    vs = sys.modules.get("vector_skill")
    if vs is None:
        root = pathlib.Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "vector_skill_snippet_probe", root / "mcp" / "vector-skill.py")
        vs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(vs)

    class _R:
        status_code = 502
        text = "bad \x1b[2Jgateway\x07 page"
        def json(self):
            raise ValueError("not json")

    out = vs._body_snippet(_R())
    assert "\x1b" not in out and "\x07" not in out
    assert "bad" in out and "gateway" in out
