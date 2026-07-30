"""
Guards for sync_skills.sh — the script that propagates the CLIENT surface to every
agent install.

The defect these pin: the per-agent loop short-circuited on `[ -L memory_bridge.py ]`
with "already current" and `continue`, which skipped the whole install — but
memory_bridge.py is the only symlinked file. SKILL.md is COPIED, so on every install
whose script was repo-linked it was written once and never refreshed again. Measured
on a live machine: three of four agents were serving a SKILL.md many versions behind
while sync reported them current on every run.

That is the worst possible file to rot silently, because SKILL.md IS the elicitation
surface — a stale copy asks the operator for the wrong fields, and the capture-surface
release gate is reviewed against the repo, not against what agents actually have.

⚠ SCOPE: the ordering test asserts a STRUCTURAL property of the script text, not its
execution. Executing the script would write into the repo's own tracked copy, so it is
not run here — the behaviour was verified by hand against all four live installs.
"""

import os

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
SCRIPT = os.path.join(ROOT, "shared-memory", "scripts", "sync_skills.sh")


def _script() -> str:
    with open(SCRIPT, encoding="utf-8") as f:
        return f.read()


# ── The two tracked SKILL.md copies must agree ───────────────────────────────

def test_tracked_skill_md_copies_are_byte_identical():
    """SKILL.md ships as TWO tracked files — the source under the server tree and
    the copy agents install from. They are kept in agreement only by sync_skills.sh,
    and the capture-surface review reads one of them. A drift means the reviewed
    file is not the shipped file. Same invariant the client script already has.
    """
    source = os.path.join(ROOT, "shared-memory", "SKILL.md")
    shipped = os.path.join(ROOT, "shared-memory-skill", "shared-memory", "SKILL.md")
    with open(source, "rb") as f_src, open(shipped, "rb") as f_ship:
        assert f_src.read() == f_ship.read(), (
            "SKILL.md copies have diverged — agents install the skill copy, so the "
            "source edit is NOT what they get. Run: bash "
            "shared-memory/scripts/sync_skills.sh"
        )


# ── SKILL.md must be refreshed BEFORE the symlink short-circuit ──────────────

def test_skill_md_copy_precedes_the_symlink_short_circuit():
    """The ordering IS the fix. A symlinked memory_bridge.py means the SCRIPT is
    auto-current; it says nothing about SKILL.md, which is copied. So the copy has
    to happen before any `continue` that skips the install.
    """
    text = _script()
    copy_at = text.find('cp "$SKILL_COPY/SKILL.md" "$dir/SKILL.md"')
    skip_at = text.find('[ -L "$dir/scripts/memory_bridge.py" ]')
    assert copy_at != -1, "per-agent SKILL.md copy is missing from sync_skills.sh"
    assert skip_at != -1, "symlink short-circuit is missing from sync_skills.sh"
    assert copy_at < skip_at, (
        "SKILL.md is copied AFTER the symlink short-circuit, so any install with a "
        "repo-linked memory_bridge.py will never receive a SKILL.md update — the "
        "capture surface rots while sync reports success"
    )


def test_symlinked_skill_md_is_not_a_reason_to_skip_the_install():
    """`-L $dir/SKILL.md` used to be one of the short-circuit conditions, which is
    backwards: SKILL.md being a symlink is the one case where it needs no copy, not
    a reason to skip the scripts too. It must not gate the whole install."""
    text = _script()
    skip_line_start = text.find('if [ -L "$dir/scripts/memory_bridge.py" ]')
    assert skip_line_start != -1
    skip_condition = text[skip_line_start:text.find("then", skip_line_start)]
    assert '"$dir/SKILL.md"' not in skip_condition, (
        "a symlinked SKILL.md must not short-circuit the whole install"
    )


def test_skill_md_copy_is_guarded_against_writing_through_a_symlink():
    """If an install DOES symlink SKILL.md, copying over it would write through to
    the repo. The copy is guarded by `! -L` so that case is skipped, not clobbered."""
    text = _script()
    copy_at = text.find('cp "$SKILL_COPY/SKILL.md" "$dir/SKILL.md"')
    guard_at = text.find('[ ! -L "$dir/SKILL.md" ]')
    assert guard_at != -1, "per-agent SKILL.md copy is not guarded against symlinks"
    assert guard_at < copy_at, "the `! -L` guard must precede the copy"


def test_refresh_is_reported_distinctly_from_already_current():
    """The defect was invisible because sync printed only success. A refresh and a
    no-op must read differently, or the next silent drift is equally undetectable."""
    text = _script()
    assert "SKILL.md REFRESHED" in text
    assert "SKILL.md already current" in text
