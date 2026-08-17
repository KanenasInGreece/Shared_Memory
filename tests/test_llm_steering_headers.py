"""S-14 (Credential_Custody_Plan PR A5): X-SM-LLM-* backend-steering headers
are a daemon/admin capability, not an any-client one. A client-originated
request gets them stripped before the routing decision is made; a request
authenticated as one of the two framework daemons keeps them.

Uses the request pattern from tests/test_llm_backend_secrets.py — role
selection surfaces in _select_llm_backend's routing choice, which we observe
indirectly by checking which backend actually gets called."""
import asyncio
import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


class _RoleCaptureSession:
    """Records the headers actually forwarded upstream -- the X-SM-LLM-Role
    header must never reach here for a client caller (S-14: it's gateway-
    internal, never meant to cross to the backend either), and must survive
    for a daemon caller."""
    closed = False

    def __init__(self):
        self.captured_headers = None

    def request(self, *a, **kw):
        self.captured_headers = kw.get("headers")
        raise RuntimeError("capture-only session — no real upstream call")


def _req(headers: dict):
    class _Req(dict):
        pass
    r = _Req()
    r.method = "POST"
    r.path = "/v1/chat/completions"
    r.rel_url = "/v1/chat/completions"
    r.headers = headers
    r.can_read_body = True

    async def read():
        return b'{"messages":[],"model":"local-model"}'
    r.read = read
    return r


def _load_gateway(monkeypatch):
    """Reloads coordinator FIRST, same order as tests/test_llm_fault_origin.
    py's proven pattern -- AUTH_CONFIGURED_AT_STARTUP is captured once at
    coordinator's own module-load time, so a caller that needs it to reflect
    a freshly-set AGENT_TOKENS must reload coordinator before hive_mind_
    proxy re-imports the name by value."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)  # see test_auth.load_coordinator's docstring
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    return g


def test_client_steering_header_stripped_before_forwarding(monkeypatch):
    """A plain (full-role, non-daemon) client identity never gets its
    X-SM-LLM-Role header forwarded upstream -- requires auth ON, since an
    auth-off install has no identity to gate on at all (see the next test)."""
    monkeypatch.setenv("AGENT_TOKENS", "claude:tok_client_test")
    g = _load_gateway(monkeypatch)
    assert g.AUTH_CONFIGURED_AT_STARTUP is True
    proxy = g.AsyncHiveMindProxy()
    session = _RoleCaptureSession()
    proxy.session = session
    req = _req({"X-SM-LLM-Role": "judge"})
    req["authenticated_agent"] = "claude"
    asyncio.run(proxy.handle_proxy(req))
    assert session.captured_headers is not None
    assert "X-SM-LLM-Role" not in session.captured_headers
    assert "x-sm-llm-role" not in {k.lower() for k in session.captured_headers}


def test_auth_off_install_keeps_backward_compatible_pass_through(monkeypatch):
    """An auth-unset install has no identity to gate steering on at all --
    same backward-compat shape as every other identity-gated check in this
    file (AUTH_CONFIGURED_AT_STARTUP False means "behave exactly as
    before"), so the header is NOT stripped here. This is deliberate, not
    an oversight: S-14 narrows what an AUTHENTICATED non-daemon caller can
    do, it does not newly restrict an install that never turned auth on."""
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    g = _load_gateway(monkeypatch)
    assert g.AUTH_CONFIGURED_AT_STARTUP is False
    proxy = g.AsyncHiveMindProxy()
    session = _RoleCaptureSession()
    proxy.session = session
    req = _req({"X-SM-LLM-Role": "judge"})
    asyncio.run(proxy.handle_proxy(req))
    assert session.captured_headers.get("X-SM-LLM-Role") == "judge"


def test_daemon_identity_keeps_steering_header(monkeypatch):
    """A request authenticated as one of the two framework daemons keeps
    X-SM-LLM-Role -- requires AUTH_CONFIGURED_AT_STARTUP True, so a real
    AGENT_TOKENS entry is configured first."""
    monkeypatch.setenv("AGENT_TOKENS", "consolidation:tok_daemon_test")
    g = _load_gateway(monkeypatch)
    assert g.AUTH_CONFIGURED_AT_STARTUP is True

    proxy = g.AsyncHiveMindProxy()
    session = _RoleCaptureSession()
    proxy.session = session
    req = _req({"X-SM-LLM-Role": "judge"})
    req["authenticated_agent"] = "consolidation"
    asyncio.run(proxy.handle_proxy(req))
    assert session.captured_headers is not None
    assert session.captured_headers.get("X-SM-LLM-Role") == "judge"


def test_full_role_client_identity_gets_header_stripped_even_with_auth_on(monkeypatch):
    """MUTATION TARGET: a full-access (non-daemon, non-admin) agent identity
    is NOT exempt just because auth happens to be configured -- only the two
    daemon names (or an admin-role token) qualify."""
    monkeypatch.setenv("AGENT_TOKENS", "claude:tok_claude_test")
    g = _load_gateway(monkeypatch)
    assert g.AUTH_CONFIGURED_AT_STARTUP is True

    proxy = g.AsyncHiveMindProxy()
    session = _RoleCaptureSession()
    proxy.session = session
    req = _req({"X-SM-LLM-Role": "judge"})
    req["authenticated_agent"] = "claude"
    asyncio.run(proxy.handle_proxy(req))
    assert session.captured_headers is not None
    assert "X-SM-LLM-Role" not in session.captured_headers


def test_rem_daemon_identity_also_keeps_steering_header(monkeypatch):
    monkeypatch.setenv("AGENT_TOKENS", "rem_daemon:tok_rem_test")
    g = _load_gateway(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    session = _RoleCaptureSession()
    proxy.session = session
    req = _req({"X-SM-LLM-Role": "judge"})
    req["authenticated_agent"] = "rem_daemon"
    asyncio.run(proxy.handle_proxy(req))
    assert session.captured_headers.get("X-SM-LLM-Role") == "judge"


def test_admin_role_identity_also_exempt(monkeypatch):
    """Direct unit test of _may_steer_llm's admin branch -- unreachable via
    the real routing today (auth_middleware confines admin tokens to
    /admin/*, so one can never reach handle_proxy at all), but the gate
    itself must still be correct per spec."""
    monkeypatch.setenv("AGENT_TOKENS", "backup:tok_backup_test")
    monkeypatch.setenv("AGENT_ROLES", "backup:admin")
    g = _load_gateway(monkeypatch)

    req = _req({"X-SM-LLM-Role": "judge"})
    req["authenticated_agent"] = "backup"
    assert g._may_steer_llm(req) is True


def test_non_llm_headers_pass_through_unaffected(monkeypatch):
    """The strip is scoped to X-SM-LLM-* only -- an unrelated custom header
    a client sent survives even while X-SM-LLM-Role is being stripped."""
    monkeypatch.setenv("AGENT_TOKENS", "claude:tok_client_test")
    g = _load_gateway(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    session = _RoleCaptureSession()
    proxy.session = session
    req = _req({"X-SM-LLM-Role": "judge", "X-Custom-Header": "keep-me"})
    req["authenticated_agent"] = "claude"
    asyncio.run(proxy.handle_proxy(req))
    assert session.captured_headers.get("X-Custom-Header") == "keep-me"
    assert "X-SM-LLM-Role" not in session.captured_headers


# ── Mutation check target ────────────────────────────────────────────────────
# See A5_HANDOFF.md's mutation-check table: making _may_steer_llm always
# return True makes test_client_steering_header_stripped_before_forwarding
# and test_full_role_client_identity_gets_header_stripped_even_with_auth_on
# both fail (the header starts surviving for a plain client identity).
