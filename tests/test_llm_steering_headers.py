"""S-14 (Credential_Custody_Plan PR A5): X-SM-LLM-* backend-steering headers
are a daemon/admin capability, not an any-client one. A client-originated
request gets them stripped before the routing decision is EVEN READ — a
non-steering identity's header never influences routing at all.

⚠ UPDATED (Model_Attributes_Routing_Plan_2026-08-18, P-6): a daemon/admin
identity's header now behaves differently on the two sides of the gateway —
it is READ (may SET the header, and it DOES influence which backend gets
picked) but never FORWARDED upstream: the provider must never see routing
metadata on the wire, so the header is stripped again right before the
upstream call, for every caller including daemons. Before this cycle a
daemon's header survived all the way to the upstream request; that specific
assertion changed in test_daemon_identity_keeps_steering_header and
test_rem_daemon_identity_also_keeps_steering_header below. See
tests/test_model_attributes_routing.py for the routing-still-works half of
this story.

Uses the request pattern from tests/test_llm_backend_secrets.py — role
selection surfaces in _select_llm_backend's routing choice, which we observe
indirectly by checking which backend actually gets called."""
import asyncio
import importlib
import json
import os
import sys

from yarl import URL

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
    # T-1 (HYG round): a REAL yarl.URL — the credentialed-route gates read
    # rel_url.path_safe / .query_string, the values actually forwarded.
    r.rel_url = URL("/v1/chat/completions", encoded=True)
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
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([{"url": "http://a:5000", "private_ok": True}]))
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
    """An auth-unset install has no identity to gate steering on at all, so
    S-14's identity gate (_may_steer_llm) is a no-op and the header IS READ
    for the gateway's own routing decision here -- same backward-compat
    shape as every other identity-gated check in this file
    (AUTH_CONFIGURED_AT_STARTUP False means "behave exactly as before") for
    THAT axis.

    ⚠ DELIBERATELY CHANGED on the FORWARDING axis (Model_Attributes_Routing_
    Plan_2026-08-18, P-6): the header is now stripped before the upstream
    call regardless of identity or auth state -- P-6 is upstream-forward
    hygiene ("the provider must never see routing metadata"), a separate
    concern from S-14's "who may set this for routing" gate, which this
    test still exercises via test_p6_role_still_drives_routing_even_though_
    stripped_on_forward in tests/test_model_attributes_routing.py."""
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    g = _load_gateway(monkeypatch)
    assert g.AUTH_CONFIGURED_AT_STARTUP is False
    proxy = g.AsyncHiveMindProxy()
    session = _RoleCaptureSession()
    proxy.session = session
    req = _req({"X-SM-LLM-Role": "judge"})
    asyncio.run(proxy.handle_proxy(req))
    assert "X-SM-LLM-Role" not in session.captured_headers


def test_daemon_identity_keeps_steering_header(monkeypatch):
    """A request authenticated as one of the two framework daemons is
    permitted to SET X-SM-LLM-Role for the gateway's own routing decision --
    requires AUTH_CONFIGURED_AT_STARTUP True, so a real AGENT_TOKENS entry is
    configured first.

    DELIBERATELY CHANGED (Model_Attributes_Routing_Plan_2026-08-18, P-6):
    the header is no longer forwarded upstream even for a daemon identity --
    "may steer" now means "may set the header for gateway-internal routing",
    never "may have it reach the provider". See
    tests/test_model_attributes_routing.py's
    test_p6_daemon_role_header_still_stripped_before_upstream_forward (the
    strip, confirmed for THIS SAME daemon identity) and
    test_p6_role_still_drives_routing_even_though_stripped_on_forward (the
    role still drives WHICH backend is picked, despite the strip)."""
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
    assert "X-SM-LLM-Role" not in session.captured_headers


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
    """DELIBERATELY CHANGED (P-6, see test_daemon_identity_keeps_steering_
    header's docstring above): the rem_daemon identity may SET the header
    for gateway-internal routing, but it is stripped before the upstream
    forward exactly like every other identity."""
    monkeypatch.setenv("AGENT_TOKENS", "rem_daemon:tok_rem_test")
    g = _load_gateway(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    session = _RoleCaptureSession()
    proxy.session = session
    req = _req({"X-SM-LLM-Role": "judge"})
    req["authenticated_agent"] = "rem_daemon"
    asyncio.run(proxy.handle_proxy(req))
    assert "X-SM-LLM-Role" not in session.captured_headers


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
