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
        {"url": "http://a:5000", "private_ok": True, "n_ctx": 8192, "max_output_tokens": 2048},
        {"url": "http://b:4000", "private_ok": True, "n_ctx": 8192, "max_output_tokens": 2048},
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
    """Equal keys stay in declaration order until a finished call warms one."""
    g = _fresh(monkeypatch)
    k = g._affinity_key(_body("G" * 7000, " f1"))
    first = g._order_backends(0.0, k)[0]
    g._note_affinity(k, first)
    second = g._order_backends(0.0, k)[0]
    assert first == second == "http://a:5000"
    # The first note is a miss. The second request is the hit.
    g._note_affinity(k, first)
    assert g._llm_affinity_hits >= 1


def test_a_different_prefix_does_not_avoid_the_warm_card(monkeypatch):
    """Affinity is only a tie-break for the same prefix. It does not protect a card."""
    g = _fresh(monkeypatch)
    krem = g._affinity_key(_body("G" * 7000, " f1"))
    rem = g._order_backends(0.0, krem)[0]
    g._note_affinity(krem, rem)
    g._note_affinity(krem, rem)
    knrem = g._affinity_key(_body("N" * 7000, " cluster"))
    nrem = g._order_backends(0.0, knrem)[0]
    assert nrem == rem


def test_unhealthy_affine_backend_stays_first_in_the_order(monkeypatch):
    """Cooldown is not an order key. The walk skips a cooled backend when it tries."""
    g = _fresh(monkeypatch)
    import time
    k = g._affinity_key(_body("G" * 7000, " f1"))
    first = g._order_backends(0.0, k)[0]
    g._note_affinity(k, first)
    g._llm_unhealthy_until[first] = time.monotonic() + 300
    second = g._order_backends(0.0, k)[0]
    assert second == first
