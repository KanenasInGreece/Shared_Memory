"""REM writes NO edges and NO labels — it summarises (`decision:1664`).

Entities, attribution, grounding and the decision extras are written at FIRST
WRITE from the operator's own metadata. REM's only output is `rem_summary` plus
the `rem_processed` mark, so:

  * the prompt asks for a summary and nothing else, for every record kind;
  * the Cypher REM builds contains no `MERGE` and no `SET e:<label>`;
  * a record with no summary to write costs no LLM call at all;
  * `rem_processed` is still set LAST, so a failed write leaves the record
    unprocessed and it is retried.

All Neo4j / Postgres / LLM I/O is mocked; no live infrastructure required.
"""

import asyncio
import importlib.util
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest


def load_rem_loop():
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    path = os.path.join(scripts_dir, "rem_loop.py")
    spec = importlib.util.spec_from_file_location("rem_loop", path)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules["rem_loop"] = mod
    spec.loader.exec_module(mod)
    return mod


rem_mod = load_rem_loop()
ONT     = rem_mod.ONT

KINDS = (rem_mod.KIND_FACT, rem_mod.KIND_DECISION, rem_mod.KIND_RETRO)
LONG  = "x" * (rem_mod.REM_SUMMARY_THRESHOLD + 1)
SHORT = "a short curated fact"

# The relationship vocabulary REM used to advertise. Read off the ontology, not
# restated: a rename in ontology.yaml must not let the old ask back in unnoticed.
RETIRED_RELS = (
    ONT.entity_link, ONT.was_attributed_to, ONT.was_assisted_by, ONT.informed_by,
    ONT.considered, ONT.rejected, ONT.under_conditions, ONT.produces_insight,
)
RETIRED_FIELDS = ('"relationships"', '"entities"', '"considered"', '"rejected"',
                  '"under_conditions"', '"produces_insight"', '"rel_type"', '"type"')


class _async_ctx:
    def __init__(self, val):  self._val = val
    async def __aenter__(self): return self._val
    async def __aexit__(self, *_): pass


def _make_daemon(data_rows=None):
    d = rem_mod.REMDaemon.__new__(rem_mod.REMDaemon)
    d.is_running = True
    mock_result = MagicMock()
    mock_result.data = AsyncMock(return_value=data_rows or [])
    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=mock_result)
    mock_driver = MagicMock()
    mock_driver.session = MagicMock(return_value=_async_ctx(mock_session))
    d.driver = mock_driver
    d._last_llm_failure = None
    return d, mock_session


class _Cur:
    def __init__(self, log): self._log = log
    def execute(self, sql, params=None): self._log.append(" ".join(sql.split()))
    def fetchone(self): return None
    def fetchall(self): return []
    def __enter__(self): return self
    def __exit__(self, *exc): return False


def _make_conn():
    executed = []
    conn = MagicMock()
    conn.cursor = MagicMock(side_effect=lambda: _Cur(executed))
    return conn, executed


def _ok_resp(content):
    class _Resp:
        status_code = 200
        headers = {}
        def json(self):
            return {"choices": [{"finish_reason": "stop",
                                 "message": {"content": content}}]}
    return _Resp()


# ── The prompt asks for a summary and nothing else ────────────────────────────

@pytest.mark.parametrize("kind", KINDS)
def test_single_prompt_asks_for_a_summary_and_nothing_else(kind):
    p = rem_mod.build_single_prompt(LONG, kind)
    assert "summary: one paragraph, at most 5 sentences" in p
    assert '"summary": "<paragraph>"' in p
    # The injection guard survives the strip-down.
    assert "RETRIEVED DATA" in p
    for rel in RETIRED_RELS:
        assert rel not in p, f"the prompt still advertises {rel}"
    for field in RETIRED_FIELDS:
        assert field not in p, f"the prompt still asks for the {field} field"
    for block in ("KNOWN TYPED NODES", "ONTOLOGY", "CAPTURE MANIFEST", "DELTA"):
        assert block not in p, f"the prompt still carries the {block} block"


def test_batch_prompt_asks_for_a_summary_and_nothing_else():
    items = [{"pg_id": 1, "content": LONG}, {"pg_id": 2, "content": LONG}]
    p = rem_mod.REMDaemon._build_batch_prompt(None, items)
    assert "[FACT 0]" in p and "[FACT 1]" in p
    assert "EXACTLY 2 lines" in p and '"idx"' in p
    assert '"summary"' in p
    assert "RETRIEVED DATA" in p
    for rel in RETIRED_RELS:
        assert rel not in p, f"the batch prompt still advertises {rel}"
    for field in RETIRED_FIELDS:
        assert field not in p, f"the batch prompt still asks for the {field} field"
    for block in ("KNOWN TYPED NODES", "ONTOLOGY", "MANIFEST"):
        assert block not in p, f"the batch prompt still carries the {block} block"


# ── The Cypher REM builds writes no edge and no label ─────────────────────────

@pytest.mark.parametrize("kind", KINDS)
def test_write_builds_no_merge_and_no_sublabel(kind):
    """Asserts on the Cypher text the write actually builds — the only place a
    resurrected edge or sub-label could reach the graph from."""
    daemon, session = _make_daemon()
    asyncio.run(daemon._write_neo4j_rem(42, "a summary", kind=kind,
                                        original_content=LONG))
    cyphers = [c.args[0] for c in session.run.call_args_list]
    assert cyphers, "the write must still issue its rem_processed statement"
    for c in cyphers:
        assert "MERGE" not in c, f"REM built an edge MERGE: {c}"
        assert "SET e:" not in c, f"REM built a sub-label SET: {c}"


@pytest.mark.parametrize("kind", KINDS)
def test_write_marks_rem_processed_in_the_last_statement(kind):
    daemon, session = _make_daemon()
    asyncio.run(daemon._write_neo4j_rem(42, "a summary", kind=kind,
                                        original_content=LONG))
    cyphers = [c.args[0] for c in session.run.call_args_list]
    assert "rem_processed" in cyphers[-1]
    assert "rem_attempts = 0" in cyphers[-1]


# ── One call per record — and none at all when nothing is asked ───────────────

@pytest.mark.asyncio
async def test_a_long_record_costs_exactly_one_llm_call(monkeypatch):
    """The verification round-trip is gone with the proposals it verified:
    a record is one call, never one plus k."""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    monkeypatch.setattr(rem_mod, "AUDIT_LOG_PATH", None)
    daemon, session = _make_daemon([{"content": LONG[:2000]}])
    conn, _ = _make_conn()
    calls = []

    async def _fake_post(self, url, **kwargs):
        calls.append(kwargs.get("json", {}))
        return _ok_resp('{"summary": "the summary"}')
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    loop = asyncio.get_running_loop()
    ok = await daemon._process_fact(7, LONG, rem_mod.KIND_FACT, conn, loop)

    assert ok is True
    assert len(calls) == 1, f"expected exactly one LLM call, got {len(calls)}"


@pytest.mark.asyncio
async def test_a_short_record_costs_no_llm_call_and_is_marked_processed(monkeypatch):
    """A record under REM_SUMMARY_THRESHOLD is asked for nothing at all, so it
    is never sent: it is marked processed without a round-trip."""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    monkeypatch.setattr(rem_mod, "AUDIT_LOG_PATH", None)
    daemon, session = _make_daemon([{"content": SHORT}])
    conn, _ = _make_conn()
    calls = []

    async def _fake_post(self, url, **kwargs):
        calls.append(kwargs.get("json", {}))
        return _ok_resp("{}")
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    loop = asyncio.get_running_loop()
    ok = await daemon._process_fact(7, SHORT, rem_mod.KIND_FACT, conn, loop)

    assert ok is True
    assert calls == [], f"a short record must cost no LLM call, got {len(calls)}"
    cyphers = [c.args[0] for c in session.run.call_args_list]
    assert any("rem_processed" in c for c in cyphers)


@pytest.mark.asyncio
async def test_a_short_fact_in_a_batch_is_not_sent_to_the_llm(monkeypatch):
    """Same rule through the batched path: the short fact gets the empty answer
    without being put in the prompt, and only the long one is sent."""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, _ = _make_daemon()
    prompts = []

    async def _fake_post(self, url, **kwargs):
        prompts.append(kwargs["json"]["messages"][1]["content"])
        return _ok_resp('{"idx": 0, "summary": "the summary"}')
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    out, _timing, _model = await daemon._llm_process_batch(
        [{"pg_id": 1, "content": SHORT}, {"pg_id": 2, "content": LONG}])

    assert len(prompts) == 1
    assert SHORT not in prompts[0], "a short fact must not reach the prompt"
    assert out[1] == {}                      # complete answer, no call
    assert out[2]["summary"] == "the summary"


@pytest.mark.asyncio
async def test_a_batch_of_only_short_facts_makes_no_call_at_all(monkeypatch):
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, _ = _make_daemon()

    async def _fake_post(self, url, **kwargs):
        raise AssertionError("no LLM call may be made for short facts")
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    out, timing, _model = await daemon._llm_process_batch(
        [{"pg_id": 1, "content": SHORT}, {"pg_id": 2, "content": SHORT}])
    assert out == {1: {}, 2: {}} and timing is None


# ── The machinery is gone, not merely unused ──────────────────────────────────

def test_the_link_machinery_is_removed_from_the_module():
    """Removed, not disabled (`decision:1664`). A dormant planner or ontology
    vocabulary is an invitation for a later change to re-enable it, and a
    reader who finds one reasonably concludes REM still links."""
    for name in ("plan_edges", "_ONTOLOGY_VOCAB", "_MINT_RULE", "_VERIFY_RULE",
                 "_build_verify_prompt", "_resolve_rel", "_entity_lines",
                 "select_prompt_slice", "_existing_edge_set", "canonical_name",
                 "_build_entity_registry", "collapse_alias_components",
                 "build_manifest", "_manifest_block", "_safe_label",
                 "_KNOWN_LABELS", "_LABEL_ALLOWED_RELS", "_LABEL_DEFAULT_REL",
                 "_ENTITY_SUBLABELS", "_EXTRA_RESULT_KEYS", "VERIFY_CALLS",
                 "ENTITY_PROMPT_K", "ENTITY_SET_LIMIT", "ENTITY_REGISTRY_LIMIT"):
        assert not hasattr(rem_mod, name), f"{name} is still in rem_loop"
    for method in ("_verify_novel_edges", "_llm_verify_call", "_grounding_slice",
                   "_fetch_closed_entity_set", "_fetch_existing_edges",
                   "_nearest_entity_names", "_embed"):
        assert not hasattr(rem_mod.REMDaemon, method), f"{method} is still on REMDaemon"


# ── The summary policy is untouched by the removal ────────────────────────────

def _apply(daemon, conn, kind, result, original):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(daemon._apply_fact_result(
            42, kind, result, conn, loop, original_content=original))
    finally:
        loop.close()


def test_apply_required_summary_missing_fails():
    """A record over REM_SUMMARY_THRESHOLD whose result carries no summary is
    skipped (retries next cycle) — the summary was requested."""
    daemon, _ = _make_daemon()
    conn, _ = _make_conn()
    assert _apply(daemon, conn, rem_mod.KIND_DECISION, {}, LONG) is False


def test_apply_unsolicited_summary_dropped():
    """A short record never stores a volunteered summary — rem_summary must not
    appear anywhere in the write."""
    daemon, session = _make_daemon()
    conn, _ = _make_conn()

    ok = _apply(daemon, conn, rem_mod.KIND_DECISION,
                {"summary": "volunteered anyway"}, SHORT)

    assert ok is True
    assert not any("rem_summary" in c.args[0] for c in session.run.call_args_list)


def test_process_fact_end_to_end_marks_outbox_and_notifies_nrem(monkeypatch):
    """Full _process_fact path for a fact anchor: verbatim content out,
    rem_processed set, outbox marked, NREM notified."""
    monkeypatch.setattr(rem_mod, "AUDIT_LOG_PATH", None)   # isolate from env leakage
    daemon, session = _make_daemon([{"content": SHORT}])   # consistency read
    conn, executed = _make_conn()

    loop = asyncio.new_event_loop()
    try:
        ok = loop.run_until_complete(
            daemon._process_fact(7, SHORT, rem_mod.KIND_FACT, conn, loop))
    finally:
        loop.close()

    assert ok is True
    cyphers = [c.args[0] for c in session.run.call_args_list]
    assert any("f.content = $orig" in c and "rem_processed" in c for c in cyphers)
    assert not any("rem_summary" in c for c in cyphers)       # short → no summary
    assert any("rem_reviewed" in s for s in executed)         # outbox marked
    assert any("pg_notify" in s for s in executed)            # NREM notified


# ── An empty object is a PARSED object, not a parse failure ───────────────────
#
# Under the old contract every result carried at least `"relationships": []`, so
# an empty dict could only mean the parse had gone wrong. Now a model that has
# nothing to add legitimately answers `{}`, and treating that as a failure would
# charge the record an attempt every cycle until it dead-lettered.

@pytest.mark.asyncio
async def test_an_empty_parsed_object_is_not_a_parse_failure(monkeypatch):
    monkeypatch.delenv("MOCK_LLM", raising=False)
    daemon, _ = _make_daemon()

    async def _fake_post(self, url, **kwargs):
        return _ok_resp("{}")
    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    result, _model = await daemon._llm_process(LONG, rem_mod.KIND_FACT, pg_id=1)

    assert result == {}
    assert daemon._last_llm_failure is None, (
        "an empty object parsed cleanly — it is not a parse failure")


def test_a_salvaged_empty_object_is_returned_not_rejected():
    """The json_repair salvage path: a body strict json.loads refuses, which
    repair resolves to an empty object, is SALVAGED. Rejecting it returned None
    and charged the record a parse failure for an answer that parsed."""
    assert rem_mod._parse_llm_json("{ : }") == {}
    # A non-object salvage is still a failure — the caller expects a dict.
    assert rem_mod._parse_llm_json('{ "a" }') is None
    assert rem_mod._parse_llm_json("not json at all !!!") is None
