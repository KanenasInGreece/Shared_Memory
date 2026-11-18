"""Little's-law invariant (fact:3716, decision:3731): the install-time capacity record must not move
when the timeout cycle removes the gateway-chosen encoder timeouts.

The expected values below were captured from the code BEFORE the cycle changed anything (framework
1.1.7), from fixed probe inputs, and are pinned literally on the record the gateway builds
(decision:1424, the capacity instrument). One value moved on purpose: the slow-probe scenario's
reranker_status was too_slow and is ok, because the status no longer compares a measurement with a
gateway-chosen timeout that no longer exists. The probe inputs are fixed on purpose: the record copies
the probe's status, and status decides whether a record is written (hive_mind_proxy.py
_maybe_derive_capacity), so identical arithmetic on live probes is not identical persistence.
A change to any field of `probe` or `derived` for these inputs fails this test; a change that is
meant must be named in the brief and re-pinned here by hand (decision:2671, fact:1309: the value is
asserted, not an equality between two expressions).
"""
import importlib
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

FP={"hardware":{"mem_total_bytes":16*1024**3}}
def _cap(rr_tp, rr_proj, emb_tp, emb_proj, rstatus="ok", extra=None):
    r={"throughput_chars_s":rr_tp,"projected_full_payload_s":rr_proj,"status":rstatus,"last_ok_at":"2026-01-01T00:00:00Z"}
    e={"throughput_chars_s":emb_tp,"projected_full_payload_s":emb_proj,"status":"ok","last_ok_at":"2026-01-01T00:00:00Z"}
    return {"reranker":r,"embedder":e,"probed_at":"2026-01-01T00:00:00Z"}
def _coord(succ, fail, chars_total, docs_total, chars_max):
    return types.SimpleNamespace(_rerank_successes=succ,_rerank_failures=fail,_rerank_payload_chars_total=chars_total,_rerank_payload_docs_total=docs_total,_rerank_payload_chars_max=chars_max)
SC={
 "fresh_theoretical": (_cap(1500.0,37.2,9000.0,2.0), None),
 "measured_after_samples": (_cap(1500.0,37.2,9000.0,2.0), _coord(40,2,2_100_000,840,90_000)),
 "probe_failing_no_throughput": (_cap(None,None,None,None,rstatus="failing"), None),
 "no_capability": (None, None),
 "slow_probe_ok_status": (_cap(400.0,140.0,900.0,20.0,rstatus="ok"), None),
}

GOLDEN = json.loads(r'''
{
 "fresh_theoretical": {
  "derived": {
   "client_ceiling_s": 73.80000000000001,
   "payload_basis": "theoretical",
   "payload_basis_sample_count": 0,
   "payload_max_chars_measured": null,
   "payload_mean_chars_measured": null,
   "queue_bound": 0,
   "recommended_reranker_mem_limit_bytes": 536870912,
   "s_max_measured_s": null,
   "s_mean_measured_s": null,
   "s_mean_s": 37.2,
   "single_search_exceeds_wait": true,
   "tolerable_wait_s": 30.0
  },
  "probe": {
   "embedder_chars_per_s": 9000.0,
   "embedder_measured_at": "2026-01-01T00:00:00Z",
   "probe_stale": false,
   "probed_at": "2026-01-01T00:00:00Z",
   "reranker_chars_per_s": 1500.0,
   "reranker_measured_at": "2026-01-01T00:00:00Z",
   "reranker_status": "ok"
  }
 },
 "measured_after_samples": {
  "derived": {
   "client_ceiling_s": 73.80000000000001,
   "payload_basis": "measured",
   "payload_basis_sample_count": 42,
   "payload_max_chars_measured": 90000,
   "payload_mean_chars_measured": 50000.0,
   "queue_bound": 0,
   "recommended_reranker_mem_limit_bytes": 536870912,
   "s_max_measured_s": 60.0,
   "s_mean_measured_s": 33.3,
   "s_mean_s": 37.2,
   "single_search_exceeds_wait": true,
   "tolerable_wait_s": 30.0
  },
  "probe": {
   "embedder_chars_per_s": 9000.0,
   "embedder_measured_at": "2026-01-01T00:00:00Z",
   "probe_stale": false,
   "probed_at": "2026-01-01T00:00:00Z",
   "reranker_chars_per_s": 1500.0,
   "reranker_measured_at": "2026-01-01T00:00:00Z",
   "reranker_status": "ok"
  }
 },
 "no_capability": {
  "derived": {
   "client_ceiling_s": 120.0,
   "payload_basis": "theoretical",
   "payload_basis_sample_count": 0,
   "payload_max_chars_measured": null,
   "payload_mean_chars_measured": null,
   "queue_bound": null,
   "recommended_reranker_mem_limit_bytes": 536870912,
   "s_max_measured_s": null,
   "s_mean_measured_s": null,
   "s_mean_s": null,
   "single_search_exceeds_wait": null,
   "tolerable_wait_s": 30.0
  },
  "probe": {
   "embedder_chars_per_s": null,
   "embedder_measured_at": null,
   "probe_stale": false,
   "probed_at": null,
   "reranker_chars_per_s": null,
   "reranker_measured_at": null,
   "reranker_status": null
  }
 },
 "probe_failing_no_throughput": {
  "derived": {
   "client_ceiling_s": 120.0,
   "payload_basis": "theoretical",
   "payload_basis_sample_count": 0,
   "payload_max_chars_measured": null,
   "payload_mean_chars_measured": null,
   "queue_bound": null,
   "recommended_reranker_mem_limit_bytes": 536870912,
   "s_max_measured_s": null,
   "s_mean_measured_s": null,
   "s_mean_s": null,
   "single_search_exceeds_wait": null,
   "tolerable_wait_s": 30.0
  },
  "probe": {
   "embedder_chars_per_s": null,
   "embedder_measured_at": "2026-01-01T00:00:00Z",
   "probe_stale": false,
   "probed_at": "2026-01-01T00:00:00Z",
   "reranker_chars_per_s": null,
   "reranker_measured_at": "2026-01-01T00:00:00Z",
   "reranker_status": "failing"
  }
 },
 "slow_probe_ok_status": {
  "derived": {
   "client_ceiling_s": 255.0,
   "payload_basis": "theoretical",
   "payload_basis_sample_count": 0,
   "payload_max_chars_measured": null,
   "payload_mean_chars_measured": null,
   "queue_bound": 0,
   "recommended_reranker_mem_limit_bytes": 536870912,
   "s_max_measured_s": null,
   "s_mean_measured_s": null,
   "s_mean_s": 140.0,
   "single_search_exceeds_wait": true,
   "tolerable_wait_s": 30.0
  },
  "probe": {
   "embedder_chars_per_s": 900.0,
   "embedder_measured_at": "2026-01-01T00:00:00Z",
   "probe_stale": false,
   "probed_at": "2026-01-01T00:00:00Z",
   "reranker_chars_per_s": 400.0,
   "reranker_measured_at": "2026-01-01T00:00:00Z",
   "reranker_status": "ok"
  }
 }
}
''')


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_BACKENDS_JSON", '[{"url": "http://a:5000", "n_ctx": 8192, "max_output_tokens": 2048}]')
    monkeypatch.setenv("CAPACITY_LOG_PATH", str(tmp_path / "capacity.jsonl"))
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    return g


@pytest.mark.parametrize("name", sorted(SC))
def test_capacity_record_is_unchanged_for_fixed_probe_inputs(gateway, name):
    capability, coordinator = SC[name]
    rec = gateway._build_capacity_record(capability, FP, "startup", coordinator)
    assert rec["probe"] == GOLDEN[name]["probe"]
    assert rec["derived"] == GOLDEN[name]["derived"]
