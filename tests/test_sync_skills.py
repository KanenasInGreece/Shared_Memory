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
import sys

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


# ── update_skill.sh: the REMOTE path had the same hazard, version-gated ───────

UPDATE_SRC = os.path.join(ROOT, "shared-memory", "scripts", "update_skill.sh")
UPDATE_SHIPPED = os.path.join(ROOT, "shared-memory-skill", "shared-memory",
                              "scripts", "update_skill.sh")


def _update_script() -> str:
    with open(UPDATE_SRC, encoding="utf-8") as f:
        return f.read()


def test_tracked_update_skill_copies_are_byte_identical():
    """update_skill.sh ships to clients from the skill copy, so a fix applied only
    to the source reaches nobody."""
    with open(UPDATE_SRC, "rb") as f_src, open(UPDATE_SHIPPED, "rb") as f_ship:
        assert f_src.read() == f_ship.read(), (
            "update_skill.sh copies have diverged — clients fetch the SKILL copy. "
            "Run: bash shared-memory/scripts/sync_skills.sh"
        )


def test_version_equality_does_not_short_circuit_the_update():
    """The remote analogue of the sync bug. update_skill.sh compared
    memory_bridge.py's VERSION and exited 0 with "Already up to date" — so a
    release that changed only SKILL.md never reached any remote client, and
    nothing enforces "if SKILL.md changed, VERSION must bump". The version may be
    read for the message; it must not gate the work.
    """
    text = _update_script()
    gate = text.find('[ "$LOCAL_VERSION" = "$REMOTE_VERSION" ]')
    assert gate != -1, "the version comparison is gone entirely — expected it kept for the message"
    # No early exit may follow the comparison before the fetch loop begins.
    fetch_loop = text.find("while IFS= read -r rel")
    assert fetch_loop > gate
    between = text[gate:fetch_loop]
    assert "exit 0" not in between, (
        "update_skill.sh exits before fetching when versions match — a SKILL.md-only "
        "release would never reach a remote client"
    )


def test_apply_step_compares_content_per_file():
    """Content, not version, is what decides. `cmp -s` per staged file is the
    mechanism; without it the script is back to trusting a version anchor."""
    text = _update_script()
    assert 'cmp -s "$src" "$dst"' in text, (
        "the apply step does not compare content per file"
    )


def test_update_reports_refreshed_distinctly_from_current():
    text = _update_script()
    assert "REFRESHED" in text
    assert "already current" in text


# ── AGENTS.md must not drift from the shipped package ────────────────────────

def test_agents_md_names_every_file_the_manifest_ships():
    """AGENTS.md Phase 8 used to tell the operating agent to install SKILL.md and
    memory_bridge.py only — 2 of 6 shipped files — which broke its OWN later
    phases: 8b copies from CONSTITUTION_SNIPPET.md in the skill dir, and 8c runs
    update_skill.sh from there. If a file joins the manifest, Phase 8 has to know.
    """
    manifest = os.path.join(ROOT, "shared-memory-skill", "shared-memory",
                            "MANIFEST.txt")
    with open(manifest, encoding="utf-8") as f:
        shipped = [ln.strip() for ln in f
                   if ln.strip() and not ln.strip().startswith("#")]
    with open(os.path.join(ROOT, "AGENTS.md"), encoding="utf-8") as f:
        agents = f.read()
    missing = [rel for rel in shipped if os.path.basename(rel) not in agents]
    assert not missing, (
        f"AGENTS.md does not mention shipped skill file(s): {missing} — Phase 8 "
        f"would install an incomplete package"
    )


# ── The REM prompt must state what the gate actually does ─────────────────────

def test_mint_rule_in_prompt_states_the_unconditional_gate():
    """A prompt that contradicts the code teaches the model the wrong contract —
    the old line promised unknown names "will become generic Entity nodes" long
    after they stopped doing so, and was then made to track an env flag. Decision
    978 removed the flag, so the sentence must state the one behaviour there is,
    and must not come back as a conditional.
    """
    import importlib.util as iu
    scripts = os.path.normpath(os.path.join(ROOT, "shared-memory", "scripts"))
    if scripts not in sys.path:
        sys.path.insert(0, scripts)

    os.environ["REM_MAY_MINT_ENTITIES"] = "1"     # retired: must be ignored
    try:
        spec = iu.spec_from_file_location(
            "rem_loop_mint_rule", os.path.join(scripts, "rem_loop.py"))
        mod = iu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert "DROPPED, not created" in mod._ONTOLOGY_VOCAB
        assert "WILL be created" not in mod._ONTOLOGY_VOCAB
        assert not hasattr(mod, "REM_MAY_MINT_ENTITIES")
    finally:
        os.environ.pop("REM_MAY_MINT_ENTITIES", None)
