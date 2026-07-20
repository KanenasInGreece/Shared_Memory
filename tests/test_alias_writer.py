"""alias_writer (ADR-017 "A") — pure-logic tests, no infra/LLM.

Locks the novel behaviour of the alias-writer: the normalized-exact auto-accept
key, the JSON-salvage parse, and batched adjudication parsing (idx matching +
graceful LLM failure). Candidate DB/embedding paths are exercised live, not here.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
import alias_writer  # noqa: E402


# ── normalized-exact key (Tier-1 auto-accept) ────────────────────────────────

def test_normalize_collapses_case_and_punctuation():
    # These are the case/format variants that dominate the fragmentation and are
    # provably the same token → safe to auto-accept.
    assert alias_writer.normalize("API_VERSION") == alias_writer.normalize("api_version")
    assert alias_writer.normalize("ADR-001") == alias_writer.normalize("ADR001")
    assert alias_writer.normalize("Antigravity") == alias_writer.normalize("antigravity")
    assert alias_writer.normalize("/health") == alias_writer.normalize("health")


def test_normalize_keeps_genuinely_different_names_apart():
    assert alias_writer.normalize("Skill Registry") != alias_writer.normalize("Skills Registry")
    assert alias_writer.normalize("ADR-001") != alias_writer.normalize("ADR-002")


# ── JSON salvage (Gemma-4 slips) ─────────────────────────────────────────────

def test_parse_llm_json_strict():
    assert alias_writer._parse_llm_json('{"idx":0,"verdict":"alias"}') == {"idx": 0, "verdict": "alias"}


def test_parse_llm_json_salvages_missing_comma():
    obj = alias_writer._parse_llm_json('{"idx": 1 "verdict": "distinct"}')
    assert obj and obj.get("verdict") == "distinct"


def test_parse_llm_json_garbage_not_a_usable_dict():
    # json_repair may return '' rather than raise; the contract callers rely on is
    # only that garbage never yields a usable verdict dict.
    assert not isinstance(alias_writer._parse_llm_json("not json at all"), dict)


# ── batched adjudication ─────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, content):
        self._c = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._c}}]}


def _batch():
    return [
        {"a": "A770 GPU hardware constraints", "b": "Hardware constraints on A770 GPU",
         "cosine": 0.98, "lexical_jaccard": 0.8, "shared_facts": 0, "domain_disjoint": False},
        {"a": "/memory/telemetry", "b": "memory_telemetry.py",
         "cosine": 0.85, "lexical_jaccard": 0.0, "shared_facts": 1, "domain_disjoint": False},
    ]


def test_adjudicate_batch_parses_jsonl_by_idx(monkeypatch):
    resp = ('{"idx":0,"verdict":"alias","confidence":0.95,"rationale":"reordered"}\n'
            '{"idx":1,"verdict":"distinct","confidence":0.9,"rationale":"url vs file"}')
    monkeypatch.setattr(alias_writer.httpx, "post", lambda *a, **k: _FakeResp(resp))
    out = alias_writer.adjudicate_batch(_batch())
    assert out[0]["verdict"] == "alias"
    assert out[1]["verdict"] == "distinct"


def test_adjudicate_batch_tolerates_partial_jsonl(monkeypatch):
    # Only one line parseable — the other pair is simply left for a later sweep.
    monkeypatch.setattr(alias_writer.httpx, "post",
                        lambda *a, **k: _FakeResp('{"idx":0,"verdict":"alias"}\n<garbage>'))
    out = alias_writer.adjudicate_batch(_batch())
    assert set(out) == {0}


def test_adjudicate_batch_handles_llm_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("timeout")
    monkeypatch.setattr(alias_writer.httpx, "post", boom)
    assert alias_writer.adjudicate_batch(_batch()) == {}


def test_adjudicate_batch_tolerates_salvaged_idx(monkeypatch):
    # A Gemma-4 slip salvaged to `"idx": 4,` must not crash the whole sweep.
    monkeypatch.setattr(alias_writer.httpx, "post",
                        lambda *a, **k: _FakeResp('{"idx": 0, "verdict": "alias"}\n'
                                                  '{"idx": "1,", "verdict": "distinct"}'))
    out = alias_writer.adjudicate_batch(_batch())
    assert out[0]["verdict"] == "alias"
    assert out[1]["verdict"] == "distinct"


# ── tier2_due (decision 852 — single-condition cadence, pure) ────────────────

def test_tier2_due_never_run_is_maximally_stale():
    due, reason = alias_writer.tier2_due(None)
    assert due is True and reason == "never_run"


def test_tier2_due_before_interval_is_not_due():
    due, reason = alias_writer.tier2_due(10.0, interval_hours=24.0)
    assert due is False and reason == "not_due"


def test_tier2_due_at_or_past_interval_fires():
    due, reason = alias_writer.tier2_due(24.0, interval_hours=24.0)
    assert due is True and reason == "interval_elapsed"
    due, reason = alias_writer.tier2_due(500.0, interval_hours=24.0)
    assert due is True and reason == "interval_elapsed"


def test_tier2_due_respects_env_default():
    # Regression guard for the dead-code bug this decision fixed: there must
    # be exactly ONE threshold, not extra force/backstop thresholds that sit
    # above it and can therefore never be reached.
    assert alias_writer.SWEEP_INTERVAL_HOURS == 24.0


# ── backend-count-aware interval scaling ─────────────────────────────────────

def test_llm_backend_count_defaults_to_one_when_unset(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    assert alias_writer._llm_backend_count() == 1


def test_llm_backend_count_ignores_weight_suffix_and_blanks(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS", "http://localhost:5000@3,http://localhost:4000@1,")
    assert alias_writer._llm_backend_count() == 2


def test_effective_interval_unscaled_at_one_backend(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    assert alias_writer.effective_sweep_interval_hours() == alias_writer.SWEEP_INTERVAL_HOURS


def test_effective_interval_scales_down_with_more_backends(monkeypatch):
    monkeypatch.setenv("LLM_BACKENDS", "http://localhost:5000,http://localhost:4000")
    assert alias_writer.effective_sweep_interval_hours() == (
        alias_writer.SWEEP_INTERVAL_HOURS / 2)


def test_effective_interval_never_drops_below_floor(monkeypatch):
    monkeypatch.setenv(
        "LLM_BACKENDS",
        ",".join(f"http://localhost:{p}" for p in range(5000, 5000 + 20)))
    assert alias_writer.effective_sweep_interval_hours() == alias_writer.ALIAS_SWEEP_FLOOR_HOURS


# ── hours_since_last_tier2_apply (thin SQL wrapper) ──────────────────────────

class _StubCursor:
    def __init__(self, row):
        self._row = row
    def execute(self, sql, params=None):
        assert "alias_adjudications" in sql and "method = 'llm'" in sql
    def fetchone(self):
        return self._row
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False


class _StubConn:
    def __init__(self, row):
        self._row = row
    def cursor(self):
        return _StubCursor(self._row)


def test_hours_since_last_tier2_apply_none_when_never_run():
    assert alias_writer.hours_since_last_tier2_apply(_StubConn((None,))) is None


def test_hours_since_last_tier2_apply_returns_hours():
    assert alias_writer.hours_since_last_tier2_apply(_StubConn((12.5,))) == 12.5


# ── run_sweep Tier-2 concurrent dispatch (fakes DB/Neo4j) ────────────────────

class _FakePgConn:
    """Records every executed statement; cursor() is a no-op context manager
    good enough for execute_values-based inserts (psycopg2.extras is not
    monkeypatched — _record_adjudications's execute_values call would need a
    real connection, so this test patches _record_adjudications directly and
    only checks it was called with the right accumulated rows)."""
    def close(self):
        pass


class _FakeSession:
    def __init__(self):
        self.written_edges = []
    def run(self, *a, **k):
        self.written_edges.append(k)
        class _Result:
            def consume(self):
                return None
        return _Result()
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False


class _FakeDriver:
    def __init__(self, session):
        self._session = session
    def session(self):
        return self._session
    def close(self):
        pass


def test_run_sweep_dispatches_all_tier2_batches_concurrently(monkeypatch):
    # Two batches' worth of LLM candidates; adjudicate_batch is monkeypatched
    # to return a verdict keyed only by the batch's OWN local index (0/1) —
    # exactly what the real function does — so this also guards against a
    # regression where concurrent dispatch cross-wires one batch's verdicts
    # onto another's candidates.
    candidates = [{"a": f"A{i}", "b": f"B{i}", "cosine": 0.9, "lexical_jaccard": 0.5,
                   "shared_facts": 0, "domain_disjoint": False} for i in range(12)]
    monkeypatch.setattr(alias_writer, "build_candidates",
                        lambda threshold, k: {"auto_accept": [], "llm_candidates": candidates})
    monkeypatch.setattr(alias_writer.psycopg2, "connect", lambda *a, **k: _FakePgConn())
    session = _FakeSession()
    monkeypatch.setattr(alias_writer, "GraphDatabase",
                        type("_G", (), {"driver": staticmethod(lambda *a, **k: _FakeDriver(session))}))
    monkeypatch.setattr(alias_writer.alias_graph, "refresh_components", lambda s: 0)

    seen_batches = []
    recorded = []
    def fake_adjudicate(batch):
        seen_batches.append(batch)
        return {i: {"verdict": "alias", "confidence": 0.9} for i in range(len(batch))}
    monkeypatch.setattr(alias_writer, "adjudicate_batch", fake_adjudicate)
    monkeypatch.setattr(alias_writer, "_record_adjudications",
                        lambda conn, rows: recorded.extend(rows))

    result = alias_writer.run_sweep(limit=None)

    # LLM_BATCH default is 10 → 12 candidates split into batches of 10 + 2.
    assert sorted(len(b) for b in seen_batches) == [2, 10]
    assert sum(len(b) for b in seen_batches) == 12          # every candidate dispatched exactly once
    assert result["aliases_written"] == 12                   # all verdicts were "alias"
    assert result["llm_unresolved"] == 0
    assert len(session.written_edges) == 12                  # one ALIASES write per accepted pair
    assert len(recorded) == 12                                # both batches' rows made it to the ledger
