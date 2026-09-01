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
    reported down/degraded on llm_pool just because ONE role has no home.

    Fix round Q7: the fleet here has a REAL, verified hole — a single
    backend declaring roles={"judge"} only (`_partial_role_fleet`, shared
    with the D3 tests below) leaves "extract" traffic with ZERO eligible
    backends. Role-less and "judge" traffic both still have a home, so
    llm_pool must stay `ok` — proving the fleet-wide-only gate (D2.4) does
    NOT fire on a partial hole, not merely that it doesn't fire on a fleet
    with no hole at all (the prior version of this test had none, per its
    own comment — conceding it tested nothing about this claim)."""
    g = _partial_role_fleet(monkeypatch)
    assert g._eligible_backends("extract") == [], (
        "fixture drift: this test needs a REAL hole for 'extract' specifically")
    assert g._eligible_backends("") and g._eligible_backends("judge"), (
        "fixture drift: role-less and 'judge' traffic must both still have a home")
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


def test_excluded_fleet_whose_fallback_is_also_down_composes_both_facts(monkeypatch):
    """Handback H2: the DOWN branch used to drop LLM_POOL_FALLBACK_REASON
    entirely — a declared fleet that was entirely EXCLUDED (falls back to
    the legacy target) whose fallback is ALSO unreachable read a bare
    "all 1 backend(s) down", with the explanatory fact (WHY the fallback is
    even in effect) gone. Composed now, the same discipline item 9 applies
    to the degraded branch."""
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "https://api.x.ai/v1", "token_env": "XAI_API_KEY"},
    ]))
    g = _fresh(monkeypatch)
    assert g.LLM_POOL_FALLBACK_REASON and "no usable backend" in g.LLM_POOL_FALLBACK_REASON
    assert g.LLM_POOL_CONFIG_EMPTY is False   # mutually exclusive with FALLBACK_REASON (D1)
    dep = g._llm_pool_dependency({g.LLM_BACKENDS[0]: "down"})
    assert dep["state"] == "down"
    assert "no usable backend" in dep["reason"]        # the FALLBACK_REASON fact
    assert "all 1 backend(s) down" in dep["reason"]     # the liveness fact


def test_config_empty_and_serving_reads_degraded_the_new_state(monkeypatch):
    """D2 item 3 (decision:1832): the NEW state. A legacy zero-config install
    with something merely serving the built-in fallback used to read `ok` —
    now reads `degraded`, visibility ahead of W4 retiring the fallback.

    Fix round Q2 (portability): the reason names the EFFECTIVE DEFAULT_TARGET
    value, scrubbed — never a hardcoded "localhost:5000" literal, since our
    ports are one valid configuration, not the only one.

    W4 (§6.5, fact:1824): the fallback backend is now ALSO fleet-wide
    ineligible by construction (private_ok defaults False), so the
    ineligibility fact composes onto the config-empty fact. No remedy text
    rides either here — a totally fresh install (neither LLM_BACKENDS nor
    LLM_DEFAULT_TARGET ever set) has nothing to migrate FROM, so
    LLM_POOL_LEGACY_KEY_PRESENT is correctly False."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.delenv("LLM_DEFAULT_TARGET", raising=False)
    g = _fresh(monkeypatch)
    assert g.LLM_POOL_LEGACY_KEY_PRESENT is False
    dep = g._llm_pool_dependency({g.LLM_BACKENDS[0]: "ok"})
    assert dep["state"] == "degraded"
    assert dep["reason"] == (
        f"no backend declared — serving the built-in "
        f"{g.scrub_url_credentials(g.DEFAULT_TARGET)} fallback"
        f"; configured, but no backend is eligible for any traffic")


def test_llm_default_target_alone_pin(monkeypatch):
    """§8: LLM_DEFAULT_TARGET-alone pin. A bare LLM_DEFAULT_TARGET override
    (no LLM_BACKENDS, no LLM_BACKENDS_JSON) is a legacy shape too (Operator
    ruling 2026-08-31: it alone is NOT a declaration) — config_empty stays
    True, the fleet is fleet-wide ineligible, and BOTH composed reasons
    carry the remedy since LLM_POOL_LEGACY_KEY_PRESENT is True here."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_DEFAULT_TARGET", "http://custom-fallback:9000")
    g = _fresh(monkeypatch)
    assert g.LLM_POOL_CONFIG_EMPTY is True
    assert g.LLM_POOL_LEGACY_KEY_PRESENT is True
    dep = g._llm_pool_dependency({g.LLM_BACKENDS[0]: "ok"})
    assert dep["state"] == "degraded"
    assert dep["reason"] == (
        "no backend declared — serving the built-in "
        "http://custom-fallback:9000 fallback; " + g._LLM_POOL_LEGACY_REMEDY
        + "; configured, but no backend is eligible for any traffic; "
        + g._LLM_POOL_LEGACY_REMEDY)


def test_dream_slot_composition_pin_undeclared_fleet(monkeypatch):
    """§8: dream-slot composition pin. An undeclared (legacy CSV) fleet ->
    _counts_free_slot reads False for it (private_ok defaults False) ->
    _dream_slots_impossible_reason() fires -> both W2 dream dependencies
    read degraded with the SAME reason string _rem_dependency/
    _nrem_dependency have always used."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    g = _fresh(monkeypatch)
    assert g._counts_free_slot("http://a:5000") is False
    assert g._dream_slots_impossible_reason() == (
        "no backend counts toward dream slots — REM and NREM will never "
        "run against this fleet")
    assert g._rem_dependency(True, None)["state"] == "degraded"
    assert g._nrem_dependency(True, {"stalled": False}, 5)["state"] == "degraded"


def test_declared_fleet_healthy_still_reads_ok_unchanged(monkeypatch):
    """MEANING CHANGE (W4, §7 MEANING_CHANGES ①②): a legacy LLM_BACKENDS CSV
    fleet is no longer "genuinely declared" in the private_ok sense — W4
    default-deny means an undeclared role-less backend serves nothing, so
    this exact fleet now DOES hit the fleet-wide-ineligibility gap this
    file's own module docstring calls out as one of the "two new gaps".
    Was: unchanged/ok. Now: degraded, remedy attached (LLM_BACKENDS is a
    legacy key)."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    g = _fresh(monkeypatch)
    assert g.LLM_POOL_CONFIG_EMPTY is False
    assert g.LLM_POOL_LEGACY_KEY_PRESENT is True
    dep = g._llm_pool_dependency({"http://a:5000": "ok"})
    assert dep["state"] == "degraded"
    assert dep["reason"] == (
        "configured, but no backend is eligible for any traffic; "
        + g._LLM_POOL_LEGACY_REMEDY)


def test_partial_down_still_degraded_unchanged(monkeypatch):
    """F6-era behaviour, untouched by W2 for the m/n fact itself — but W4
    (§7 MEANING_CHANGES ②) means this legacy CSV pair is ALSO fleet-wide
    ineligible now, so that fact composes after the liveness fact, with the
    remedy (LLM_BACKENDS is a legacy key)."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000,http://b:5000")
    g = _fresh(monkeypatch)
    dep = g._llm_pool_dependency({"http://a:5000": "ok", "http://b:5000": "down"})
    assert dep["state"] == "degraded"
    assert dep["reason"] == (
        "1/2 backend(s) down; configured, but no backend is eligible for "
        "any traffic; " + g._LLM_POOL_LEGACY_REMEDY)


def test_coexisting_degraded_reasons_compose_never_drop_a_fact(monkeypatch):
    """Fix round Q9: llm_pool composes coexisting reasons the same way
    rem_daemon/nrem_daemon do — a partial-down fact and a fleet-wide
    ineligibility fact can BOTH be true at once (a hole for `bad`, forced
    ineligible via monkeypatch), and the old early-return chain would have
    reported only whichever check ran first, silently dropping the other."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000,http://b:5000")
    g = _fresh(monkeypatch)
    monkeypatch.setattr(g, "_eligible_backends", lambda *a, **kw: [])
    dep = g._llm_pool_dependency({"http://a:5000": "ok", "http://b:5000": "down"})
    assert dep["state"] == "degraded"
    assert "1/2 backend(s) down" in dep["reason"]
    assert "no backend is eligible for any traffic" in dep["reason"]


def test_config_empty_marker_does_not_leak_across_calls_without_reload(monkeypatch):
    """Fix round Q11: LLM_POOL_CONFIG_EMPTY is SET-ONLY on every path through
    _load_llm_backends() — calling it again in the SAME process (no module
    reload) with a genuinely declared fleet must read False, never retain a
    prior True from an earlier call that had nothing declared."""
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    g = _fresh(monkeypatch)
    assert g.LLM_POOL_CONFIG_EMPTY is True
    monkeypatch.setenv("LLM_BACKENDS", "http://a:5000")
    g._load_llm_backends()   # same process, no importlib.reload this time
    assert g.LLM_POOL_CONFIG_EMPTY is False, (
        "LLM_POOL_CONFIG_EMPTY leaked True from the PRIOR call — it must be "
        "set explicitly on every path, never left to whatever an earlier "
        "call happened to leave behind")


# ══════════════════════════════════════════════════════════════════════════════
# D3 — dream-slots-impossible surfaced through rem_daemon/nrem_daemon
# ══════════════════════════════════════════════════════════════════════════════

def _partial_role_fleet(monkeypatch):
    """One backend, roles={"judge"} only — does not cover the full dream
    role set {extract, judge}, so _counts_free_slot reads False for it (the
    partial-role case C-1/decision:1357 already warns about at startup).
    W4 default-deny: private_ok=true explicit gives it role-less AND "judge"
    eligibility (R-3′) while leaving "extract" a real hole — a roles-carrying
    entry WITH an explicit privacy opt-in still serves only its declared
    roles for role-carrying traffic (_role_eligible: `role in roles`)."""
    monkeypatch.delenv("LLM_BACKENDS", raising=False)
    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "roles": ["judge"], "private_ok": True},
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
