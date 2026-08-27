"""normalize_projects.py requires --apply to write (O1, v0.9.69, fact:1734 C(e)
/ decision:1736): no orchestration script rewrites an axis unless the operator
asked for it on THIS invocation. `--dry-run` used to be the opt-OUT of a
default-apply run; it is replaced by `--apply`, an opt-IN required to write
anything — the default previews.

Drives the module's own `build_arg_parser()` directly with `parse_args(...)`
rather than grepping the source text — a string match for
`action="store_true"` would pass against a flag that was declared but never
actually wired to anything; `parse_args` proves the parser really produces
the values the rest of the module reads.

Full behavioural coverage (the flag actually gating every write, across a
committed pair, a vetoed pair, and a partially-failing map) lives in
test_project_rename_atomicity.py's `test_default_invocation_previews_and_writes_nothing`
and `test_apply_flag_is_required_to_write`, which drive the real `main()`
against a mocked Postgres/Neo4j pair.
"""
import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

import normalize_projects as np_mod  # noqa: E402


def test_default_parse_means_preview_not_apply():
    args = np_mod.build_arg_parser().parse_args([])
    assert args.apply is False
    dry_run = not args.apply
    assert dry_run is True, "the default (no --apply) must mean preview, not write"


def test_apply_flag_flips_it_to_write():
    args = np_mod.build_arg_parser().parse_args(["--apply"])
    assert args.apply is True
    dry_run = not args.apply
    assert dry_run is False, "--apply must flip the derived dry_run to False"


def test_dry_run_flag_is_no_longer_accepted():
    """The old opt-out flag must be gone, not merely superseded — a caller
    who still passes --dry-run should get argparse's own unknown-argument
    refusal (SystemExit), never a silent no-op."""
    import pytest
    with pytest.raises(SystemExit):
        np_mod.build_arg_parser().parse_args(["--dry-run"])


def test_map_flag_still_parses_and_defaults_to_none():
    """--map defaults to None (not the env value) so main() can tell whether
    the map came from --map or the $PROJECT_ALIASES fallback and report the
    source — a caller who omits it entirely must see that distinction
    preserved at the parser level."""
    args = np_mod.build_arg_parser().parse_args([])
    assert args.map is None
    args = np_mod.build_arg_parser().parse_args(["--map", "old=new"])
    assert args.map == "old=new"
