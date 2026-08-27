"""Grep-level contract: normalize_projects.py requires --apply to write.

O1 (v0.9.69, fact:1734 C(e) / decision:1736): no orchestration script rewrites
an axis unless the operator asked for it on THIS invocation. `--dry-run` used
to be the opt-OUT of a default-apply run; it is replaced by `--apply`, an
opt-IN required to write anything — the default previews.

Full behavioural coverage (the flag actually gating every write, across a
committed pair, a vetoed pair, and a partially-failing map) lives in
test_project_rename_atomicity.py's `test_default_invocation_previews_and_writes_nothing`
and `test_apply_flag_is_required_to_write`, which drive the real `main()`
against a mocked Postgres/Neo4j pair. This file is the narrow, source-reading
companion the plan names explicitly.
"""
import os

_SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "shared-memory", "scripts", "normalize_projects.py"
)


def _read() -> str:
    with open(_SCRIPT, encoding="utf-8") as fh:
        return fh.read()


def test_apply_flag_is_declared_store_true():
    text = _read()
    assert '"--apply"' in text
    assert 'action="store_true"' in text.split('"--apply"', 1)[1][:120], (
        "--apply must be a store_true flag (default off) — the write path is "
        "opt-in, never a default"
    )


def test_dry_run_flag_is_gone():
    text = _read()
    assert '"--dry-run"' not in text, (
        "the old opt-out flag must be removed, not merely superseded — a "
        "caller who still passes --dry-run should get argparse's unknown-"
        "argument refusal, never a silent no-op"
    )


def test_main_derives_dry_run_as_the_negation_of_apply():
    text = _read()
    assert "dry_run = not args.apply" in text, (
        "the default must be preview (dry_run True) unless --apply was given"
    )
