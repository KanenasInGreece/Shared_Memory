"""W2 — visibility before behaviour (decision:1832): an undeclared or
ineligible fleet is now visible on /health instead of reading `ok`.

Covers D1 (the LLM_POOL_CONFIG_EMPTY marker), D2 (`_llm_pool_dependency`'s
liveness-first ordering + the fleet-wide eligibility verdict) and D3 (the
dream-slots-impossible verdict surfaced through `_rem_dependency`/
`_nrem_dependency`, with the same down -> config -> probe-timing ordering).

⚠ W4-FORWARD GUARD (Opus F7): every assertion below pins the CURRENT (W2)
default value by name — a `degraded` a legacy zero-config install now reads
where it used to read `ok`. W4 (fallback retirement) must consciously edit
these when that default changes; they are not incidental.
"""
import importlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def _fresh(monkeypatch):
    """Auth-off reload — mirrors test_model_attributes_routing.py's _fresh().
    Callers set LLM_BACKENDS / LLM_BACKENDS_JSON BEFORE calling this."""
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)
    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)
    return g


# ══════════════════════════════════════════════════════════════════════════════
# D1 — LLM_POOL_CONFIG_EMPTY: absence vs exclusion
# ══════════════════════════════════════════════════════════════════════════════

def test_config_empty_marker_set_on_absence_only(monkeypatch):
    """Nothing declared at all -> CONFIG_EMPTY True, FALLBACK_REASON None
    (agy 1: the two markers are mutually exclusive by construction)."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    g = _fresh(monkeypatch)
    assert g.LLM_POOL_CONFIG_EMPTY is True
    assert g.LLM_POOL_FALLBACK_REASON is None
    assert g.LLM_BACKENDS == [g.DEFAULT_TARGET]


def test_config_empty_marker_never_set_on_exclusion(monkeypatch):
    """LLM_BACKENDS_JSON present but every entry excluded -> FALLBACK_REASON
    set, CONFIG_EMPTY stays False — this is EXCLUSION, not ABSENCE, and only
    one of the two markers may explain a given fallback."""
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.x.ai/v1", "token_env": "XAI_API_KEY"},
    ]))
    g = _fresh(monkeypatch)
    assert g.LLM_POOL_FALLBACK_REASON and "no usable backend" in g.LLM_POOL_FALLBACK_REASON
    assert g.LLM_POOL_CONFIG_EMPTY is False


def test_config_empty_marker_not_set_when_a_fleet_is_declared(monkeypatch):
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    g = _fresh(monkeypatch)
    assert g.LLM_POOL_CONFIG_EMPTY is False


# ══════════════════════════════════════════════════════════════════════════════
# D2 — _llm_pool_dependency: liveness first, then configuration
# ══════════════════════════════════════════════════════════════════════════════

def test_ruled_check_verbatim_fleet_wide_ineligibility_reads_degraded(monkeypatch):
    """THE RULED MUTATION-CHECK TARGET (fact:1309, brief verbatim): force
    _eligible_backends() -> [] and assert the LITERAL "degraded" on
    dependencies.llm_pool.state, plus the reason naming it. A declared,
    fully-live fleet that cannot serve ANY traffic class is invisible today
    without this — every probe reads ok, so llm_pool used to read ok too."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    g = _fresh(monkeypatch)
    monkeypatch.setattr(g, "_eligible_backends", lambda *a, **kw: [])
    dep = g._llm_pool_dependency({"http://a:5000": "ok"})
    assert dep["state"] == "degraded"
    assert "no backend is eligible for any traffic" in dep["reason"]


def test_a_hole_for_some_roles_only_does_not_touch_llm_pool(monkeypatch):
    """agy 3 / Opus F6: a backend serving OTHER traffic fine must not be
    reported down/degraded on llm_pool just because ONE role has no home."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    g = _fresh(monkeypatch)
    # Role-less traffic is eligible (default private_ok=True, roles absent);
    # "judge"/"extract" are also eligible (roles absent = serves-all) — so
    # this reflects a REAL fleet with no hole at all, proving the ok path
    # still fires when eligibility is not force-emptied.
    dep = g._llm_pool_dependency({"http://a:5000": "ok"})
    assert dep["state"] == "ok"


def test_ordering_pin_config_empty_and_all_down_reads_literal_down(monkeypatch):
    """D2 item 1 (Opus F2): liveness is NEVER softened by configuration. Before
    the ordering fix, LLM_POOL_FALLBACK_REASON-style checks ran BEFORE the
    down computation and could read `degraded` here; now `down` always wins,
    with the M1 composed reason naming the absent config."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    g = _fresh(monkeypatch)
    assert g.LLM_POOL_CONFIG_EMPTY is True
    dep = g._llm_pool_dependency({g.LLM_BACKENDS[0]: "down"})
    assert dep["state"] == "down"
    assert "nothing serves the built-in fallback" in dep["reason"]
    assert "no backend declared" in dep["reason"]


def test_config_empty_and_serving_reads_degraded_the_new_state(monkeypatch):
    """D2 item 3 (decision:1832): the NEW state. A legacy zero-config install
    with something merely serving localhost:5000 used to read `ok` — now
    reads `degraded`, visibility ahead of W4 retiring the fallback."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    g = _fresh(monkeypatch)
    dep = g._llm_pool_dependency({g.LLM_BACKENDS[0]: "ok"})
    assert dep["state"] == "degraded"
    assert dep["reason"] == (
        "no backend declared — serving the built-in localhost:5000 fallback")


def test_declared_fleet_healthy_still_reads_ok_unchanged(monkeypatch):
    """A genuinely declared, healthy, eligible fleet is UNCHANGED by W2 —
    only the two new gaps (config-empty, fleet-wide ineligibility) newly
    surface as degraded."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    g = _fresh(monkeypatch)
    assert g.LLM_POOL_CONFIG_EMPTY is False
    dep = g._llm_pool_dependency({"http://a:5000": "ok"})
    assert dep["state"] == "ok"


def test_partial_down_still_degraded_unchanged(monkeypatch):
    """F6-era behaviour, untouched by W2: some (not all) backends down stays
    degraded with the m/n reason."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000,http://b:5000")
    g = _fresh(monkeypatch)
    dep = g._llm_pool_dependency({"http://a:5000": "ok", "http://b:5000": "down"})
    assert dep["state"] == "degraded"
    assert dep["reason"] == "1/2 backend(s) down"


# ══════════════════════════════════════════════════════════════════════════════
# D3 — dream-slots-impossible surfaced through rem_daemon/nrem_daemon
# ══════════════════════════════════════════════════════════════════════════════

def _partial_role_fleet(monkeypatch):
    """One backend, roles={"judge"} only — does not cover the full dream
    role set {extract, judge}, so _counts_free_slot reads False for it (the
    partial-role case C-1/decision:1357 already warns about at startup)."""
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["judge"]},
    ]))
    return _fresh(monkeypatch)


def _full_role_fleet(monkeypatch):
    """One backend covering both dream roles -> _counts_free_slot True."""
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["extract", "judge"]},
    ]))
    return _fresh(monkeypatch)


def test_dream_slots_impossible_degrades_rem_and_nrem_with_ruled_reason(monkeypatch):
    g = _partial_role_fleet(monkeypatch)
    assert g._dream_slots_impossible_reason() is not None

    rem_dep = g._rem_dependency(True, None)
    assert rem_dep["state"] == "degraded"
    assert rem_dep["reason"] == (
        "no backend counts toward dream slots — REM and NREM will never "
        "run against this fleet")

    nrem_dep = g._nrem_dependency(True, {"stalled": False}, 5)
    assert nrem_dep["state"] == "degraded"
    assert "no backend counts toward dream slots" in nrem_dep["reason"]


def test_full_role_fleet_reads_ok_on_both_dream_dependencies(monkeypatch):
    g = _full_role_fleet(monkeypatch)
    assert g._dream_slots_impossible_reason() is None
    assert g._rem_dependency(True, None)["state"] == "ok"
    assert g._nrem_dependency(True, {"stalled": False}, 5)["state"] == "ok"


def test_not_yet_probed_and_slots_impossible_asserts_literal_degraded(monkeypatch):
    """B3 (decision:1832), the exact ruled test: the config verdict is
    knowable before any probe, so it must WIN OVER the `unknown` "not yet
    probed" state rather than wait behind it."""
    g = _partial_role_fleet(monkeypatch)
    dep = g._nrem_dependency(True, None, 5)   # consolidation not yet a dict
    assert dep["state"] == "degraded"
    assert "no backend counts toward dream slots" in dep["reason"]


def test_dead_letter_reason_leads_and_slots_reason_appends(monkeypatch):
    """B3: when BOTH apply, the liveness/dead-letter reason wins the lead
    position and the slots reason appends after it."""
    g = _partial_role_fleet(monkeypatch)
    dep = g._rem_dependency(True, 3)
    assert dep["state"] == "degraded"
    assert dep["reason"].startswith("dead_letters:3")
    assert "no backend counts toward dream slots" in dep["reason"]


def test_down_still_wins_over_the_slots_config_verdict(monkeypatch):
    """The ordering's first tier is unconditional: a dead process is `down`
    regardless of what the fleet's config verdict would otherwise say."""
    g = _partial_role_fleet(monkeypatch)
    assert g._rem_dependency(False, None)["state"] == "down"
    assert g._nrem_dependency(False, None, 5)["state"] == "down"
