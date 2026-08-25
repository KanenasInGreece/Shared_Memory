"""INVARIANT: the capability probe's sleep is a function of the LAST PROBE'S
OUTCOME — a failing (or never-landed) backend is re-probed on the short
retry interval; a serving backend (`ok`, and `too_slow` — which IS serving,
only expensively) waits the full interval.

The defect (measured, fact:1609): after a reboot the first probe hit the
encoders ~30 s before they finished loading and the daemon then slept the
full CAPABILITY_PROBE_INTERVAL_S (600 s), so /health read `degraded` and
search answered [] for ten minutes over a sixty-second cold start.

Pure tests over `_probe_sleep_s`; values are asserted on one side
(fact:1309), never as equality between two constants.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def _g(monkeypatch):
    import importlib
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    monkeypatch.setenv("CAPABILITY_PROBE_INTERVAL_S", "600")
    monkeypatch.setenv("CAPABILITY_PROBE_RETRY_S", "15")
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    return g


def _cap(rer, emb):
    return {"status": "x", "reranker": {"status": rer}, "embedder": {"status": emb}}


def test_failing_backend_retries_short(monkeypatch):
    g = _g(monkeypatch)
    assert g._probe_sleep_s(_cap("failing", "ok")) == 15.0
    assert g._probe_sleep_s(_cap("ok", "failing")) == 15.0
    assert g._probe_sleep_s(_cap("failing", "failing")) == 15.0


def test_never_probed_retries_short(monkeypatch):
    g = _g(monkeypatch)
    assert g._probe_sleep_s({"status": "unknown", "probed_at": None}) == 15.0
    assert g._probe_sleep_s(None) == 15.0


def test_ok_waits_full_interval(monkeypatch):
    g = _g(monkeypatch)
    assert g._probe_sleep_s(_cap("ok", "ok")) == 600.0


def test_too_slow_is_serving_and_waits_full_interval(monkeypatch):
    """A fast re-probe of a too_slow backend adds load to the encoder that is
    already struggling — the reason the interval is long in the first place."""
    g = _g(monkeypatch)
    assert g._probe_sleep_s(_cap("too_slow", "ok")) == 600.0
    assert g._probe_sleep_s(_cap("too_slow", "too_slow")) == 600.0


def test_env_overrides_are_honoured(monkeypatch):
    g = _g(monkeypatch)
    assert g._probe_sleep_s(_cap("failing", "ok"), interval_s=100.0, retry_s=7.0) == 7.0
    assert g._probe_sleep_s(_cap("ok", "ok"), interval_s=100.0, retry_s=7.0) == 100.0


def test_daemon_uses_outcome_dependent_sleep(monkeypatch):
    """The loop must consult _probe_sleep_s, not the constant — otherwise the
    pure function is decoration. Read the source rather than run the daemon."""
    import inspect
    g = _g(monkeypatch)
    src = inspect.getsource(g._capability_probe_daemon)
    assert "_probe_sleep_s(" in src
    assert "timeout=CAPABILITY_PROBE_INTERVAL_S" not in src
