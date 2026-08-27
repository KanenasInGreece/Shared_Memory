"""Grep-level contract: update_framework.sh's domain backfill is OPT-IN.

O1 (v0.9.69, fact:1734 C(d) / decision:1736): no orchestration script applies
an axis rewrite unless the operator asked for it on THAT invocation. This is
the narrow, source-reading companion to the executable coverage in
test_update_framework_no_domain_backfill.py and
test_update_framework_live_execution.py (named explicitly in the plan as
`test_update_framework_domain_backfill_opt_in`) -- it pins the CONTRACT
(the flag exists, gates the step, and the old default-on knob is a
documented no-op) directly against the script text, independent of any
sandboxed subprocess run.
"""
import os

_SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "shared-memory", "scripts", "update_framework.sh"
)


def _read() -> str:
    with open(_SCRIPT, encoding="utf-8") as fh:
        return fh.read()


def test_domain_backfill_flag_is_declared_and_defaults_off():
    text = _read()
    assert "--domain-backfill" in text
    assert "DOMAIN_BACKFILL=0" in text, (
        "the opt-in flag's variable must default to 0 -- the backfill step "
        "must not run unless the operator passed --domain-backfill on this "
        "invocation"
    )


def test_step_6_is_gated_on_the_opt_in_variable_not_dry_run_alone():
    text = _read()
    assert 'if [[ "$DOMAIN_BACKFILL" != "1" ]]' in text, (
        "step 6's selection must branch on the opt-in flag first -- gating "
        "only on DRY_RUN would run the backfill for real on every "
        "unflagged live invocation"
    )


def test_no_domain_backfill_is_a_documented_noop():
    text = _read()
    assert "--no-domain-backfill" in text
    assert "no-op" in text.lower(), (
        "the old opt-out flag must be documented as a no-op now that the "
        "default it used to request is the default behaviour"
    )
