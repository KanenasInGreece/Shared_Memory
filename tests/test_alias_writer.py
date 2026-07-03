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
