"""Unit 2 (daemon side) — requirement-declaration LLM routing
(Local_Documentation/Model_Attributes_Routing_Plan_2026-08-18.md, REVISED
DESIGN section; Local_Documentation/ROUTING_UNIT2_BUILDER_BRIEF.md).

Covers the three things Unit 2 adds on top of Unit 1's gateway:

1. Every dream LLM call site sends the ONE new header, `X-SM-LLM-Role`, with
   the role the plan's taxonomy assigns it (R-1: {extract, judge} —
   `summarize` is RESERVED and never sent).
2. Each daemon RECOGNIZES the gateway's two structured routing refusals (422
   `no_eligible_backend` / 503 `backend_at_capacity`, both stamped
   `X-SM-Fault-Origin: gateway`) and skips the unit WITHOUT charging any
   record-chargeable counter (U2-I1) — keying on the STRUCTURED BODY + that
   header, never on status alone, so a real provider 422/503 passed through
   is never misread as the gateway declining to place the job. No daemon-side
   retry of a refused call within the same cycle (I-4's spirit).
3. `dream_telemetry.record_llm_call(..., prompt_chars=...)` (N-4, additive,
   shipped inert by Unit 1) is wired from every call site that already
   records telemetry, using the caller's own char-count of the prompt it
   built.

All Neo4j/Postgres/LLM I/O is mocked; no live infrastructure required.

⛔ Fixtures use only public project names (shared-memory / -GitHub /
-monitor) and generic entity names — no private instances.
"""
import asyncio
import logging
import os
import sys

import pytest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
sys.path.insert(0, os.path.dirname(__file__))   # tests/ itself, for cross-imports below

# Reuse the dynamic-import + daemon-construction helpers already established
# by each file's own suite (same convention test_rem_degeneration.py uses for
# rem_loop.py) rather than re-deriving them.
from test_rem_loop import rem_mod, _make_daemon, _ok_resp          # noqa: E402
from test_nrem_confidence import cl, daemon_with_fake_graph        # noqa: E402


# ── Shared fake response for httpx.AsyncClient.post (rem_loop /
#    consolidation_loop) — httpx's own Response.json() is a sync method
#    regardless of which client made the call, so one fake serves both
#    daemons. ─────────────────────────────────────────────────────────────────

class _RefusalResp:
    """Fakes the gateway's structured 422/503 routing refusal body."""
    def __init__(self, error="no_eligible_backend", constraint="role",
                role="extract", status_code=422, gateway_origin=True):
        self.status_code = status_code
        self.headers = {"X-SM-Fault-Origin": "gateway"} if gateway_origin else {}
        self._body = {"error": error, "constraint": constraint, "role": role}
        self.text = str(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        # Real httpx raises for any 4xx/5xx — a mutation that removes a
        # call site's refusal early-return must fall through to the SAME
        # exception path a live 422/503 would take.
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("refused", request=None, response=self)


class _ProviderErrorResp:
    """A REAL provider 422/503 passed through the proxy — no
    X-SM-Fault-Origin header, a provider-shaped error body. Must never be
    misread as a gateway routing refusal."""
    def __init__(self, status_code=422):
        self.status_code = status_code
        self.headers = {}
        self._body = {"error": "invalid_request_error", "message": "bad request"}
        self.text = str(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("provider error", request=None, response=self)


# ── 1. The pure _routing_refusal() helper — one copy per Unit 2 file ─────────
# (No shared module is owned by Unit 2's file list — see HANDOFF.md — so the
# helper is intentionally duplicated, not imported, between rem_loop.py and
# consolidation_loop.py. Parametrizing over both proves the duplicates agree.)

_ROUTING_MODULES = [rem_mod, cl]


@pytest.mark.parametrize("mod", _ROUTING_MODULES, ids=["rem_loop", "consolidation_loop"])
def test_routing_refusal_recognizes_no_eligible_backend(mod):
    refusal = mod._routing_refusal(_RefusalResp())
    assert refusal == {"error": "no_eligible_backend", "constraint": "role", "role": "extract"}


class _RefusalRespWithDeclaration(_RefusalResp):
    """W4 Ruling C(α)/E(α2) (§5): the additive `declaration` key the
    gateway's 422 body now carries on an undeclared/opt-in-less fleet."""
    def __init__(self, declaration="none", **kw):
        super().__init__(**kw)
        self._body["declaration"] = declaration
        self.text = str(self._body)


@pytest.mark.parametrize("mod", _ROUTING_MODULES, ids=["rem_loop", "consolidation_loop"])
@pytest.mark.parametrize("declaration", ["none", "no_role_less_opt_in"])
def test_routing_refusal_ignores_the_additive_declaration_key(mod, declaration):
    """§5 (Ruling C(α)/E(α2)): both daemons' _routing_refusal() build their
    return value from an explicit three-key dict literal
    (`{"error": ..., "constraint": ..., "role": ...}`) — an ADDITIVE
    `declaration` key on the gateway's 422 body must never surface in the
    daemon's own parsed refusal, by construction. MUTATION TARGET: were
    either daemon to instead do `dict(body)` or otherwise pass the raw
    body through, this test would start seeing a 4th key."""
    refusal = mod._routing_refusal(_RefusalRespWithDeclaration(declaration=declaration))
    assert refusal == {"error": "no_eligible_backend", "constraint": "role", "role": "extract"}
    assert "declaration" not in refusal


@pytest.mark.parametrize("mod", _ROUTING_MODULES, ids=["rem_loop", "consolidation_loop"])
def test_routing_refusal_recognizes_backend_at_capacity(mod):
    resp = _RefusalResp(error="backend_at_capacity", constraint=None, role=None, status_code=503)
    refusal = mod._routing_refusal(resp)
    assert refusal["error"] == "backend_at_capacity"


@pytest.mark.parametrize("mod", _ROUTING_MODULES, ids=["rem_loop", "consolidation_loop"])
def test_routing_refusal_never_fires_on_status_alone(mod):
    """A REAL provider 422/503 (no X-SM-Fault-Origin: gateway) must NEVER be
    misread as a routing refusal — recognition keys on the structured body +
    that header, never status alone."""
    assert mod._routing_refusal(_ProviderErrorResp(422)) is None
    assert mod._routing_refusal(_ProviderErrorResp(503)) is None


@pytest.mark.parametrize("mod", _ROUTING_MODULES, ids=["rem_loop", "consolidation_loop"])
def test_routing_refusal_requires_the_gateway_origin_header_even_with_a_matching_body(mod):
    """Isolates the X-SM-Fault-Origin check from the error-field check: a
    body that HAPPENS to carry the exact gateway refusal shape (status +
    error string) but is missing (or lies about) the gateway-origin header
    must still be rejected. Without this test, deleting the header check
    alone is invisible — every OTHER fixture's error field already differs
    from the two known refusal strings, so the error-field check catches it
    regardless of the header, masking the header check's own necessity."""
    resp = _RefusalResp(gateway_origin=False)   # exact refusal body, no header
    assert mod._routing_refusal(resp) is None
    resp2 = _RefusalResp(gateway_origin=False)
    resp2.headers = {"X-SM-Fault-Origin": "upstream"}   # present but wrong value
    assert mod._routing_refusal(resp2) is None


@pytest.mark.parametrize("mod", _ROUTING_MODULES, ids=["rem_loop", "consolidation_loop"])
def test_routing_refusal_ignores_unrelated_status(mod):
    resp = _RefusalResp(status_code=400)   # gateway-origin header, but not 422/503
    assert mod._routing_refusal(resp) is None


@pytest.mark.parametrize("mod", _ROUTING_MODULES, ids=["rem_loop", "consolidation_loop"])
def test_routing_refusal_ignores_unrecognized_gateway_error(mod):
    """X-SM-Fault-Origin: gateway present but the error field is neither of
    the two known refusal shapes — must not misclassify a third gateway-
    origin error class as a routing refusal."""
    class _R:
        status_code = 422
        headers = {"X-SM-Fault-Origin": "gateway"}
        text = "{}"
        def json(self):
            return {"error": "something_else"}
    assert mod._routing_refusal(_R()) is None


# ── 2. rem_loop.py — role headers, per call site ──────────────────────────────
#
# Only a record OVER the summary threshold reaches an LLM call at all
# (`decision:1664`), so every REM case below uses one.
_REM_LONG = "x" * (rem_mod.REM_SUMMARY_THRESHOLD + 1)

@pytest.mark.asyncio
async def test_rem_solo_call_sends_extract_role_header(monkeypatch):
    daemon, _ = _make_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    captured = {}
    async def _fake_post(self, url, **kwargs):
        captured["headers"] = kwargs.get("headers", {})
        return _ok_resp('{"summary":"s","relationships":[]}')
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)
    await daemon._llm_process(_REM_LONG, rem_mod.KIND_FACT, pg_id=1)
    assert captured["headers"].get("X-SM-LLM-Role") == "extract"


@pytest.mark.asyncio
async def test_rem_batch_call_sends_extract_role_header(monkeypatch):
    daemon, _ = _make_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    captured = {}
    async def _fake_post(self, url, **kwargs):
        captured["headers"] = kwargs.get("headers", {})
        return _ok_resp('{"idx":0,"relationships":[]}')
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)
    items = [{"pg_id": 1, "content": _REM_LONG}]
    await daemon._llm_process_batch(items)
    assert captured["headers"].get("X-SM-LLM-Role") == "extract"


# ── 3. rem_loop.py — U2-I1: routing refusal never charges rem_attempts ───────

@pytest.mark.asyncio
async def test_rem_solo_routing_refusal_does_not_charge_an_attempt(monkeypatch):
    """U2-I1: a gateway routing refusal is evidence about the FLEET's
    config, never about this record — must never count toward the
    dead-letter cap, exactly like a plain transport failure (F1)."""
    daemon, _ = _make_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    async def _fake_post(self, url, **kwargs):
        return _RefusalResp(constraint="role", role="extract")
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    with patch.object(daemon, "_bump_rem_attempts", new=AsyncMock()) as bump:
        ok = await daemon._process_fact(7, _REM_LONG, rem_mod.KIND_FACT,
                                        None, asyncio.get_running_loop())

    assert ok is False
    bump.assert_not_awaited()
    assert daemon._last_llm_failure == rem_mod.LLM_FAIL_ROUTING_REFUSED
    assert rem_mod.LLM_FAIL_ROUTING_REFUSED not in rem_mod.LLM_FAIL_CHARGEABLE


@pytest.mark.asyncio
async def test_rem_solo_refusal_never_retries_the_widened_bound(monkeypatch):
    """No daemon-side retry of a refused call within the same cycle (I-4's
    spirit) — a truncation gets a widened-bound retry; a refusal must not."""
    daemon, _ = _make_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    bounds = []
    async def _fake_post(self, url, **kwargs):
        bounds.append(kwargs.get("json", {})["max_tokens"])
        return _RefusalResp()
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)
    result, _model = await daemon._llm_process(_REM_LONG, rem_mod.KIND_FACT, pg_id=1)
    assert result is None
    assert len(bounds) == 1, "a routing refusal must not trigger the widen-once retry ladder"


@pytest.mark.asyncio
async def test_rem_batch_routing_refusal_charges_no_record(monkeypatch):
    """Batch mirror of F1: one refusal must not demote the whole batch to
    solo — no attempt is charged to any of the batched records."""
    daemon, _ = _make_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    async def _fake_post(self, url, **kwargs):
        return _RefusalResp(constraint="fit", role="extract")
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)
    items = [{"pg_id": i, "content": _REM_LONG} for i in (1, 2, 3)]
    results, timing, _model = await daemon._llm_process_batch(items)
    assert results is None, "a refused CALL must be distinguishable from empty results"
    assert timing is None
    assert daemon._last_llm_failure == rem_mod.LLM_FAIL_ROUTING_REFUSED


@pytest.mark.asyncio
async def test_rem_backend_at_capacity_gets_the_same_no_charge_treatment(monkeypatch):
    """Assigner ruling (HANDOFF.md): 503 backend_at_capacity is treated
    exactly like 422 no_eligible_backend — both mean 'the gateway declined
    to place this job right now', never evidence about the record."""
    daemon, _ = _make_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    async def _fake_post(self, url, **kwargs):
        return _RefusalResp(error="backend_at_capacity", constraint=None,
                            role="extract", status_code=503)
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)
    with patch.object(daemon, "_bump_rem_attempts", new=AsyncMock()) as bump:
        ok = await daemon._process_fact(7, _REM_LONG, rem_mod.KIND_FACT,
                                        None, asyncio.get_running_loop())
    assert ok is False
    bump.assert_not_awaited()
    assert daemon._last_llm_failure == rem_mod.LLM_FAIL_ROUTING_REFUSED


# ── 4. rem_loop.py — N-4: prompt_chars wired into telemetry ──────────────────

@pytest.mark.asyncio
async def test_rem_solo_call_wires_prompt_chars(monkeypatch):
    daemon, _ = _make_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    sent = {}
    async def _fake_post(self, url, **kwargs):
        sent["prompt"] = kwargs["json"]["messages"][1]["content"]
        return _ok_resp('{"summary":"s","relationships":[]}')
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)
    recorded = {}
    def _spy(*a, **kw):
        recorded.update(kw)
        return {}
    monkeypatch.setattr(rem_mod, "record_llm_call", _spy)
    await daemon._llm_process(_REM_LONG, rem_mod.KIND_FACT, pg_id=1)
    assert recorded.get("prompt_chars") == len(sent["prompt"])
    assert recorded["prompt_chars"] > 0


@pytest.mark.asyncio
async def test_rem_batch_call_wires_prompt_chars(monkeypatch):
    daemon, _ = _make_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    sent = {}
    async def _fake_post(self, url, **kwargs):
        sent["prompt"] = kwargs["json"]["messages"][1]["content"]
        return _ok_resp('{"idx":0,"relationships":[]}')
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)
    recorded = {}
    def _spy(*a, **kw):
        recorded.update(kw)
        return {}
    monkeypatch.setattr(rem_mod, "record_llm_call", _spy)
    items = [{"pg_id": 1, "content": _REM_LONG}]
    await daemon._llm_process_batch(items)
    assert recorded.get("prompt_chars") == len(sent["prompt"])
    assert recorded["prompt_chars"] > 0


# ── 5. consolidation_loop.py — role header, refusal, no-retry, prompt_chars ──

@pytest.mark.asyncio
async def test_nrem_insight_call_sends_judge_role_header(monkeypatch):
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, _ = daemon_with_fake_graph()
    captured = {}
    async def _fake_post(self, url, **kwargs):
        captured["headers"] = kwargs.get("headers", {})
        return _ok_resp("SLOT 1: rationale.\nPRINCIPLE: p.")
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)
    rows = [(1, "Decision A\n\nra", "p", "decision", {})]
    slots = await daemon.generate_insight_slots("E", rows)
    assert captured["headers"].get("X-SM-LLM-Role") == "judge"
    assert slots is not None


@pytest.mark.asyncio
async def test_nrem_insight_routing_refusal_no_retry_no_poison(monkeypatch):
    """U2-I1 for NREM: a refusal fails the CALL without poisoning the fold —
    neither _last_llm_truncated nor _last_llm_missing_slots is set, so
    _fold_insight's three-way branch takes its plain 'ledger rows stay open,
    next sweep retries' path (no truncation_failures/slot_failures charge).
    Never widens to the second (retry) bound."""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, _ = daemon_with_fake_graph()
    calls = []
    async def _fake_post(self, url, **kwargs):
        calls.append(kwargs.get("json", {}).get("max_tokens"))
        return _RefusalResp(constraint="fit", role="judge")
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)
    rows = [(1, "Decision A\n\nra", "p", "decision", {})]
    slots = await daemon.generate_insight_slots("E", rows)
    assert slots is None
    assert len(calls) == 1, "a refused call must not widen to the truncation-retry bound"
    assert daemon._last_llm_truncated is False
    assert daemon._last_llm_missing_slots is False


@pytest.mark.asyncio
async def test_nrem_insight_routing_refusal_logs_constraint_and_role(monkeypatch, caplog):
    """The distinguishing behaviour a mutation removing the refusal branch
    would break: the loud, entity-scoped log naming constraint + role. The
    generic non-200 fallback names only the HTTP status, never these."""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, _ = daemon_with_fake_graph()
    async def _fake_post(self, url, **kwargs):
        return _RefusalResp(constraint="fit", role="judge")
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)
    rows = [(1, "Decision A\n\nra", "p", "decision", {})]
    with caplog.at_level(logging.WARNING, logger="ConsolidationDaemon"):
        slots = await daemon.generate_insight_slots("E", rows)
    assert slots is None
    assert any("REFUSED by gateway routing" in r.message and "constraint=fit" in r.message
              and "role=judge" in r.message
              for r in caplog.records)


@pytest.mark.asyncio
async def test_nrem_backend_at_capacity_gets_the_same_no_poison_treatment(monkeypatch):
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, _ = daemon_with_fake_graph()
    async def _fake_post(self, url, **kwargs):
        return _RefusalResp(error="backend_at_capacity", constraint=None,
                            role="judge", status_code=503)
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)
    rows = [(1, "Decision A\n\nra", "p", "decision", {})]
    slots = await daemon.generate_insight_slots("E", rows)
    assert slots is None
    assert daemon._last_llm_truncated is False
    assert daemon._last_llm_missing_slots is False


@pytest.mark.asyncio
async def test_nrem_insight_call_wires_prompt_chars(monkeypatch):
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, _ = daemon_with_fake_graph()
    sent = {}
    async def _fake_post(self, url, **kwargs):
        sent["prompt"] = kwargs["json"]["messages"][1]["content"]
        return _ok_resp("SLOT 1: rationale.\nPRINCIPLE: p.")
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)
    recorded = {}
    def _spy(*a, **kw):
        recorded.update(kw)
        return {}
    monkeypatch.setattr(cl, "record_llm_call", _spy)
    rows = [(1, "Decision A\n\nra", "p", "decision", {})]
    await daemon.generate_insight_slots("E", rows)
    assert recorded.get("prompt_chars") == len(sent["prompt"])
    assert recorded["prompt_chars"] > 0
