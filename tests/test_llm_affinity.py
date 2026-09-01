"""Cache-affinity gateway dispatch (advisor-reviewed). Verifies REM's stable
grounding prefix pins to one warm card and NREM avoids evicting it."""
import importlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def _fresh(monkeypatch):
    # W4 default-deny: role-less traffic now needs an explicit private_ok
    # opt-in — this fixture is about affinity/cache routing, not privacy, so
    # both backends declare it explicitly (fact:1195-safe public fixture names).
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "private_ok": True},
        {"url": "http://b:4000", "private_ok": True},
    ]))
    import hive_mind_proxy
    importlib.reload(hive_mind_proxy)
    return hive_mind_proxy


def _body(prefix, tail):
    return json.dumps({"messages": [{"role": "user", "content": prefix + tail}]}).encode()


def test_affinity_key_prefix_stable(monkeypatch):
    g = _fresh(monkeypatch)
    ground = "X" * 7000
    assert g._affinity_key(_body(ground, " fact-1")) == g._affinity_key(_body(ground, " fact-2"))
    assert g._affinity_key(_body(ground, "")) != g._affinity_key(_body("Y" * 7000, ""))
    assert g._affinity_key(b"not json") is None
    assert g._affinity_key(b'{"messages": []}') is None


def test_rem_pins_to_one_warm_card(monkeypatch):
    g = _fresh(monkeypatch)
    k = g._affinity_key(_body("G" * 7000, " f1"))
    r1 = g._select_llm_backend("", k)
    g._llm_inflight[r1] = 0            # request completed
    r2 = g._select_llm_backend("", k)  # same prefix → cache hit → same card
    assert r1 == r2
    assert g._llm_affinity_hits >= 1


def test_nrem_avoids_protected_rem_card(monkeypatch):
    g = _fresh(monkeypatch)
    krem = g._affinity_key(_body("G" * 7000, " f1"))
    rem = g._select_llm_backend("", krem)
    g._llm_inflight[rem] = 0
    g._select_llm_backend("", krem)    # 2nd route → hits>=2 → rem card protected
    g._llm_inflight[rem] = 0
    knrem = g._affinity_key(_body("N" * 7000, " cluster"))
    nrem = g._select_llm_backend("", knrem)
    assert nrem != rem                 # NREM routed to the other card, protecting the warm cache


def test_unhealthy_affine_backend_falls_back(monkeypatch):
    g = _fresh(monkeypatch)
    import time
    k = g._affinity_key(_body("G" * 7000, " f1"))
    first = g._select_llm_backend("", k)
    g._llm_unhealthy_until[first] = time.monotonic() + 300   # affine card in cooldown
    g._llm_inflight[first] = 0
    second = g._select_llm_backend("", k)
    assert second != first             # falls back to a healthy card
