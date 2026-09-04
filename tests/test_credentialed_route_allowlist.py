"""S-04 (Critical, Credential_Custody_Plan PR A5): a request bound for a
credentialed backend (one with a resolved token_env) may only be POST to a
framework-owned endpoint. Everything else gets 403 before any upstream call
is attempted, plus one credential-audit line — never the key itself.

Binds ONLY the credentialed branch: an uncredentialed backend (no token_env)
keeps today's full pass-through, unaffected.

Reload pattern (coordinator then hive_mind_proxy) mirrors tests/test_llm_
fault_origin.py's proven approach for tests that need a real credential-
audit-log file write."""
import asyncio
import importlib
import json
import os
import sys

import pytest
from yarl import URL

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


@pytest.fixture(autouse=True)
def _isolated_route_denial_counter():
    """Same isolation contract as test_llm_fault_origin.py's
    _isolated_fault_counters — this counter is a process-lifetime module
    global mutated directly by the real proxy code path."""
    import coordinator
    coordinator._credential_counters["credentialed_route_denied"] = 0
    yield
    coordinator._credential_counters["credentialed_route_denied"] = 0
    coordinator._credential_audit_writer = None


class _MustNotCallSession:
    """.request() raising AssertionError proves the 403 short-circuit fired
    before any upstream call was attempted — a RuntimeError (as the capture
    sessions elsewhere in this suite use) would be swallowed by handle_proxy's
    own exception handling and read as "passed the gate, then failed to
    connect", which is the wrong signal here."""
    closed = False

    def request(self, *a, **kw):
        raise AssertionError("must not reach the upstream call — the allowlist should have 403'd first")


class _HeaderCaptureSession:
    """Records headers/body then aborts before any real network call —
    mirrors the same-named class in test_llm_backend_secrets.py."""
    closed = False

    def __init__(self):
        self.captured_headers = None

    def request(self, *a, **kw):
        self.captured_headers = kw.get("headers")
        raise RuntimeError("capture-only session — no real upstream call")


def _req(method: str, raw: str):
    """`raw` is the request target EXACTLY as it would arrive on the wire —
    percent-encoding and query string included.

    T-1 (HYG round): `rel_url` is a REAL `yarl.URL(raw, encoded=True)`, because
    both gates now read `rel_url.path_safe` and `rel_url.query_string` — the
    values that are actually forwarded. `path` is the URL's DECODED `.path`,
    which is what production gives it, so it NEVER contains '?': a stub that
    put the query in `path` would produce a 403 that looks like the R-B query
    denial but is really an allowlist miss on a path no caller ever sends
    (ADV2-11)."""
    class _Req:
        pass
    r = _Req()
    r.method = method
    rel = URL(raw, encoded=True)
    r.rel_url = rel
    r.path = rel.path
    r.headers = {}
    r.can_read_body = True

    async def read():
        return b'{"messages":[],"model":"local-model"}'
    r.read = read
    return r


def _load_credentialed_gateway(monkeypatch):
    # private_ok: true (M-5, Model_Attributes_Routing_Plan_2026-08-18) — this
    # file is about the S-04 credentialed-route allowlist on role-less
    # traffic, not the M-5 startup choice (its own coverage lives in
    # tests/test_model_attributes_routing.py); explicit here so role-less
    # traffic stays eligible for this backend and reaches the allowlist
    # check at all.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-allowlist-test")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY",
         "model": "deepseek-chat", "private_ok": True},
    ]))
    import hive_mind_proxy as g
    importlib.reload(g)
    return g


def test_post_chat_completions_to_credentialed_backend_passes(monkeypatch):
    g = _load_credentialed_gateway(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    session = _HeaderCaptureSession()
    proxy.session = session
    resp = asyncio.run(proxy.handle_proxy(_req("POST", "/v1/chat/completions")))
    # The capture session raises RuntimeError once .request() is actually
    # called -- handle_proxy's own exception handling turns that into a 500,
    # which is proof the allowlist let the call through (not a 403).
    assert resp.status != 403
    assert session.captured_headers is not None
    assert session.captured_headers["Authorization"] == "Bearer sk-allowlist-test"


def test_get_to_credentialed_backend_403s_before_any_upstream_call(monkeypatch):
    g = _load_credentialed_gateway(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _MustNotCallSession()
    _forbid_selection(monkeypatch, g)
    resp = asyncio.run(proxy.handle_proxy(_req("GET", "/v1/chat/completions")))
    assert resp.status == 403
    body = json.loads(resp.body.decode())
    assert "sk-allowlist-test" not in json.dumps(body)
    # Honest, non-leaky: names the RULE, not the backend roster.
    assert "framework endpoints" in body["error"]
    assert "deepseek" not in body["error"].lower()


def test_arbitrary_path_to_credentialed_backend_403s(monkeypatch):
    """Not just wrong method -- a POST to a path the framework never calls
    (e.g. an admin endpoint on the provider) is denied too."""
    g = _load_credentialed_gateway(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _MustNotCallSession()
    resp = asyncio.run(proxy.handle_proxy(_req("POST", "/v1/admin/delete-everything")))
    assert resp.status == 403


def test_uncredentialed_backend_keeps_full_pass_through(monkeypatch):
    """The allowlist binds ONLY the credentialed branch -- a local backend
    with no token_env is unaffected, same as before this change."""
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([{"url": "http://a:5000", "private_ok": True}]))
    import hive_mind_proxy as g
    importlib.reload(g)

    proxy = g.AsyncHiveMindProxy()
    session = _HeaderCaptureSession()
    proxy.session = session
    resp = asyncio.run(proxy.handle_proxy(_req("GET", "/some/arbitrary/path")))
    assert resp.status != 403
    assert session.captured_headers is not None


def test_denied_route_bumps_credential_counter_and_writes_audit_line(monkeypatch, tmp_path):
    log_path = tmp_path / "credential-audit.jsonl"
    monkeypatch.setenv("CREDENTIAL_AUDIT_LOG_PATH", str(log_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-allowlist-test")
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.deepseek.com/v1", "token_env": "DEEPSEEK_API_KEY", "private_ok": True},
    ]))
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)

    proxy = g.AsyncHiveMindProxy()
    proxy.session = _MustNotCallSession()
    resp = asyncio.run(proxy.handle_proxy(_req("DELETE", "/v1/models")))
    assert resp.status == 403

    asyncio.run(coordinator._credential_audit_writer.flush())
    assert coordinator._credential_counters["credentialed_route_denied"] == 1
    assert coordinator._credential_last_ts["credentialed_route_denied"] is not None
    content = log_path.read_text()
    assert '"event":"credentialed_route_denied"' in content
    assert '"method":"DELETE"' in content
    assert '"path":"/v1/models"' in content
    assert "sk-allowlist-test" not in content


# ══════════════════════════════════════════════════════════════════════════
# R-B (HYG round) — the gate compares the FORWARDED form, and a query denies
#
# ⚠ THERE ARE TWO GATES AND ONE TEST CANNOT PIN BOTH (ADV2-11). R-4 (the
# pre-dispatch gate) short-circuits with 403 whenever EVERY eligible backend
# is credentialed, which is exactly the fleet `_load_credentialed_gateway`
# builds — so on that fleet S-04 is never reached, and mutating S-04 alone
# leaves every test above green. S-04 only fires when the eligible set was
# MIXED (R-4's `all(...)` false) and selection nevertheless landed on the
# credentialed member. Each set below therefore builds the fleet shape that
# reaches ITS gate, and names the gate in the assertion message.
# ══════════════════════════════════════════════════════════════════════════

# ── Set 1: the ALL-CREDENTIALED fleet → R-4 is the gate ──────────────────────

def _forbid_selection(monkeypatch, g):
    """Make backend SELECTION fatal, so only a PRE-DISPATCH denial can pass.

    ⚠ MEASURED, and the reason this exists (fact:1321 — check the instrument).
    Without it these tests are a false green: restore `request.path` at R-4
    and the request simply falls through to selection, where S-04 — still
    comparing the forwarded form — issues the same 403. The status assertion
    cannot tell the two gates apart, so a mutation of the gate the test NAMES
    leaves it passing. R-4 returns before `_select_llm_backend` is ever
    called, so a selector that raises is what makes "denied at R-4" observable
    rather than assumed."""
    def _never(*a, **k):
        raise AssertionError(
            "R-4 (pre-dispatch) must deny before backend selection is reached")
    monkeypatch.setattr(g, "_select_llm_backend", _never)

@pytest.mark.parametrize("spelling", [
    "/v1%2fchat/completions",       # encoded slash, leading segment
    "/v1/chat%2fcompletions",       # encoded slash, inner segment
    "/v1/chat/completio%6es",       # encoded ordinary letter ('n')
])
def test_r4_denies_a_percent_encoded_spelling_of_an_allowed_route(monkeypatch, spelling):
    """THE RULE: this gate compares the RAW request-target path — the exact
    string `_upstream_url` forwards — so a caller that percent-encodes an
    allowed path is REFUSED. No framework caller encodes anything, so the
    only traffic this turns away spells a framework endpoint in a way the
    framework never does.

    Each spelling here DECODES to `/v1/chat/completions`, which is why the
    old `request.path` compare approved all three and then signed and sent
    the encoded form to the provider — the string that was CHECKED and the
    string that was SENT were different strings, security fix A1's shape on
    the credentialed path.

    ⚠ `raw_path`, never `path_safe` (measured, yarl 1.24.5, and stated in its
    own docstring): `path_safe` is the ROUTER-matched form and decodes every
    escape except `%2F` and `%25`, so it would catch the first two spellings
    and let `%6e` through reading as the allowed route."""
    g = _load_credentialed_gateway(monkeypatch)
    _forbid_selection(monkeypatch, g)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _MustNotCallSession()
    resp = asyncio.run(proxy.handle_proxy(_req("POST", spelling)))
    assert resp.status == 403, (
        "R-4 (pre-dispatch) must deny the percent-encoded spelling — it is "
        "not the string that gets forwarded")
    assert "framework endpoints" in json.loads(resp.body.decode())["error"]


def test_r4_denies_a_query_string_on_an_allowed_credentialed_route(monkeypatch):
    """R-B. The path IS on the allowlist; the query is not examined by it and
    is forwarded verbatim, so a `?key=…`-style parameter would steer a signed
    request past a check that cannot see it. No framework caller sends one."""
    g = _load_credentialed_gateway(monkeypatch)
    _forbid_selection(monkeypatch, g)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _MustNotCallSession()
    resp = asyncio.run(proxy.handle_proxy(_req("POST", "/v1/chat/completions?x=y")))
    assert resp.status == 403, "R-4 (pre-dispatch) must deny a query on a credentialed route"


def test_r4_still_allows_the_plain_spelling(monkeypatch):
    """The counterweight, as a VALUE: the ordinary framework call must still
    reach the upstream with its key. A gate that denied everything would pass
    both tests above."""
    g = _load_credentialed_gateway(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    session = _HeaderCaptureSession()
    proxy.session = session
    resp = asyncio.run(proxy.handle_proxy(_req("POST", "/v1/chat/completions")))
    assert resp.status != 403
    assert session.captured_headers["Authorization"] == "Bearer sk-allowlist-test"


# ── Set 2: the MIXED fleet → S-04 is the gate ────────────────────────────────

_CREDENTIALED_URL = "https://api.deepseek.com/v1"


def _load_mixed_fleet(monkeypatch):
    """One uncredentialed local backend + one credentialed cloud backend, with
    SELECTION FORCED onto the credentialed member.

    R-4's `all(...)` is False on this fleet, so it falls through; the forced
    selection is what makes S-04 — and only S-04 — the gate that answers.
    `_select_llm_backend` is monkeypatched rather than left to least-busy
    ordering so the test cannot pass for the wrong reason on a different
    iteration order."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-allowlist-test")
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://local:5000", "private_ok": True},
        {"url": _CREDENTIALED_URL, "token_env": "DEEPSEEK_API_KEY",
         "model": "deepseek-chat", "private_ok": True},
    ]))
    import hive_mind_proxy as g
    importlib.reload(g)
    monkeypatch.setattr(g, "_select_llm_backend",
                        lambda *a, **k: _CREDENTIALED_URL)
    return g


def test_mixed_fleet_sanity_r4_does_not_fire(monkeypatch):
    """Instrument check (fact:1321): prove the fleet really does fall through
    R-4, so the two S-04 tests below are pinning the gate they name. An
    allowed route on this fleet reaches the upstream with the provider key —
    which it could not do if R-4 had answered."""
    g = _load_mixed_fleet(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    session = _HeaderCaptureSession()
    proxy.session = session
    resp = asyncio.run(proxy.handle_proxy(_req("POST", "/v1/chat/completions")))
    assert resp.status != 403
    assert session.captured_headers["Authorization"] == "Bearer sk-allowlist-test"


@pytest.mark.parametrize("spelling", [
    "/v1%2fchat/completions",
    "/v1/chat%2fcompletions",
    "/v1/chat/completio%6es",
])
def test_s04_denies_a_percent_encoded_spelling_of_an_allowed_route(monkeypatch, spelling):
    """The same rule at the other gate. Pinned separately and on the MIXED
    fleet because R-4 never runs here — the two gates cannot be pinned by one
    test (ADV2-11), in either direction."""
    g = _load_mixed_fleet(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _MustNotCallSession()
    resp = asyncio.run(proxy.handle_proxy(_req("POST", spelling)))
    assert resp.status == 403, (
        "S-04 (post-selection) must deny the percent-encoded spelling — on a "
        "mixed fleet it is the only gate between this request and the key")


def test_s04_denies_a_query_string_on_an_allowed_credentialed_route(monkeypatch):
    g = _load_mixed_fleet(monkeypatch)
    proxy = g.AsyncHiveMindProxy()
    proxy.session = _MustNotCallSession()
    resp = asyncio.run(proxy.handle_proxy(_req("POST", "/v1/chat/completions?x=y")))
    assert resp.status == 403, (
        "S-04 (post-selection) must deny a query on a credentialed route")


def test_an_uncredentialed_backend_still_accepts_a_query(monkeypatch):
    """R-B binds the CREDENTIALED branch only (ADV2-8). A local backend with
    no key keeps today's full pass-through, query and all — the same
    boundary the rest of this file draws."""
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps(
        [{"url": "http://a:5000", "private_ok": True}]))
    import hive_mind_proxy as g
    importlib.reload(g)

    proxy = g.AsyncHiveMindProxy()
    session = _HeaderCaptureSession()
    proxy.session = session
    resp = asyncio.run(proxy.handle_proxy(_req("POST", "/v1/chat/completions?x=y")))
    assert resp.status != 403
    assert session.captured_headers is not None


# ── Mutation check target ────────────────────────────────────────────────────
# See A5_HANDOFF.md's mutation-check table: inverting the `route not in
# CREDENTIALED_BACKEND_ALLOWED_ROUTES` condition in handle_proxy makes
# test_get_to_credentialed_backend_403s_before_any_upstream_call and
# test_post_chat_completions_to_credentialed_backend_passes both fail (the
# allowed route starts 403ing, the denied one starts reaching the session).
