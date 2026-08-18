"""Per-backend request-body overrides (LLM_BACKENDS_JSON "extra_body").

A cloud backend can need provider-specific switches no caller knows to send —
the motivating case is a hybrid reasoning model whose thinking mode is ON by
default and is disabled per REQUEST, in the body. The gateway owns the
backend's dialect exactly as it owns its model id and its credential: the
operator declares the overrides once, per backend, and every routed payload
carries them. A malformed extra_body excludes the backend from the pool —
for a metered backend, "reached without its overrides" is the very
misconfiguration the field exists to prevent.
"""
import asyncio
import importlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


class _CaptureSession:
    """Records the body handed to .request() then aborts before any real
    network call — mirrors _HeaderCaptureSession in test_llm_backend_secrets."""
    closed = False

    def __init__(self):
        self.captured_data = None

    def request(self, *a, **kw):
        self.captured_data = kw.get("data")
        raise RuntimeError("capture-only session — no real upstream call")


class _Req:
    method = "POST"
    path = "/v1/chat/completions"        # not in ROUTING_MAP -> the LLM pool branch
    rel_url = "/v1/chat/completions"
    headers = {"Authorization": "Bearer client-gateway-token"}
    can_read_body = True

    async def read(self):
        return b'{"messages":[],"model":"local-model","temperature":0.1}'


# ── the pure merge ──────────────────────────────────────────────────────────

def _fresh():
    import hive_mind_proxy as g
    importlib.reload(g)
    return g


def test_extra_body_keys_are_merged_and_override_the_caller(monkeypatch):
    g = _fresh()
    out = json.loads(g._apply_backend_body_overrides(
        b'{"messages":[],"temperature":0.6}', None,
        {"thinking": {"type": "disabled"}, "temperature": 0.1}))
    assert out["thinking"] == {"type": "disabled"}
    assert out["temperature"] == 0.1          # backend config wins
    assert out["messages"] == []              # caller fields survive


def test_model_override_beats_a_model_left_in_extra_body(monkeypatch):
    g = _fresh()
    out = json.loads(g._apply_backend_body_overrides(
        b'{"messages":[],"model":"local-model"}', "real-model-id",
        {"model": "stale-model-id"}))
    assert out["model"] == "real-model-id"


def test_model_rewrite_contract_unchanged_when_caller_sent_no_model(monkeypatch):
    """The long-standing model override only rewrites a model field the caller
    sent — extra_body must not change that contract by smuggling one in."""
    g = _fresh()
    out = json.loads(g._apply_backend_body_overrides(
        b'{"messages":[]}', "real-model-id", {"thinking": {"type": "disabled"}}))
    assert "model" not in out


def test_unparseable_body_is_forwarded_unchanged(monkeypatch):
    g = _fresh()
    raw = b"\xff\xfenot json"
    assert g._apply_backend_body_overrides(raw, "m", {"k": 1}) == raw


def test_no_overrides_means_the_body_is_untouched_bytes(monkeypatch):
    g = _fresh()
    raw = b'{"messages":[],  "spacing": "preserved"}'
    assert g._apply_backend_body_overrides(raw, None, None) is raw


# ── config parsing ──────────────────────────────────────────────────────────

def test_extra_body_lands_in_the_backend_table(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000"},
        {"url": "http://b:4000", "extra_body": {"thinking": {"type": "disabled"}}},
    ]))
    g = _fresh()
    assert g.LLM_BACKEND_EXTRAS["http://a:5000"] is None
    assert g.LLM_BACKEND_EXTRAS["http://b:4000"] == {"thinking": {"type": "disabled"}}


def test_non_object_extra_body_excludes_the_backend(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000"},
        {"url": "http://b:4000", "extra_body": "thinking=off"},
    ]))
    g = _fresh()
    assert "http://b:4000" not in g.LLM_BACKENDS
    assert "http://a:5000" in g.LLM_BACKENDS


def test_legacy_comma_form_has_no_extras(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000@2,http://b:4000")
    g = _fresh()
    assert g.LLM_BACKEND_EXTRAS == {"http://a:5000": None, "http://b:4000": None}


# ── through the proxy ───────────────────────────────────────────────────────

def test_routed_payload_carries_the_backend_overrides(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "model": "real-model-id",
         "extra_body": {"thinking": {"type": "disabled"}}},
    ]))
    g = _fresh()
    proxy = g.AsyncHiveMindProxy()
    session = _CaptureSession()
    proxy.session = session
    asyncio.run(proxy.handle_proxy(_Req()))

    body = json.loads(session.captured_data)
    assert body["thinking"] == {"type": "disabled"}
    assert body["model"] == "real-model-id"
    assert body["temperature"] == 0.1
