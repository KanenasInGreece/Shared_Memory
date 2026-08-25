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

def test_the_per_agent_refresh_is_driven_by_the_manifest_not_by_filenames():
    """⛔ THIS TEST REPLACED ONE THAT PASSED THROUGH TWO FAILURES OF THE THING IT
    CLAIMED TO GUARD, and the replacement is the lesson.

    The old test asserted that the literal string `cp "$SKILL_COPY/SKILL.md"
    "$dir/SKILL.md"` appeared before the symlink short-circuit. That was true,
    and the delivery was still broken — because the guarantee needed is not
    "SKILL.md is copied early", it is "EVERY MANIFEST FILE is copied". Hoisting
    one filename above a `continue` fixed one file and left the `continue` in
    place, so `Documentation/schema.md` was added to the manifest later and was
    missing entirely from two of four live installs, with sync reporting success.

    Naming a file in a test is naming the file you already thought of. The real
    behaviour is asserted by executing the script — see test_skill_delivery.py,
    which points it at a temporary tree via SHARED_MEMORY_SYNC_AGENTS and checks
    what actually lands. This test keeps only the structural claim that cannot
    regress into a per-file list.
    """
    text = _script()
    assert 'done < "$SKILL_COPY/MANIFEST.txt"' in text, (
        "sync_skills.sh no longer iterates MANIFEST.txt — a hardcoded file list "
        "is how this defect shipped twice")
    # Both phases must be manifest-driven: phase 1 (source → tracked copy) and
    # phase 2 (tracked copy → each install). One of each is not enough.
    assert text.count('done < "$SKILL_COPY/MANIFEST.txt"') >= 2, (
        "only one phase reads the manifest; the other is back to a fixed list")


def test_no_symlink_short_circuit_survives_anywhere_in_the_per_agent_loop():
    """⛔ The `continue` is GONE, and it must not come back in any form.

    It caused this defect twice — first skipping SKILL.md, then skipping
    Documentation/schema.md — because "the script is a symlink" was read as
    "this whole install is current". Under the copy-only policy there are no
    symlinked installs to short-circuit for, so the condition has no remaining
    justification and its return would be a regression by construction.
    """
    text = _script()
    assert '[ -L "$dir/scripts/memory_bridge.py" ]' not in text, (
        "the symlink short-circuit is back in sync_skills.sh")
    assert "scripts symlinked (repo-linked, auto-current)" not in text, (
        "sync_skills.sh still treats a symlinked install as auto-current")


def test_both_delivery_paths_replace_a_symlink_rather_than_following_it():
    """⛔ COPY-ONLY (Xenofon, 2026-08-04): an installed file is a real copy.

    A link into a checkout binds every agent on the machine to that checkout's
    PATH — move or archive the project and all of them break at once, silently.
    Staleness is the lesser risk because it is detectable on every sync.

    The hazard the policy CREATES is the one pinned here: `cp` follows a symlink
    and would write into the source tree, and `cmp` follows one too, so a link
    pointing at identical content would report "already current" and survive
    forever. Both paths must therefore detect the link rather than compare
    through it. Executable proof is in test_skill_delivery.py.
    """
    sync = _script()
    assert 'rm -f "$dir/$rel"' in sync, (
        "sync_skills.sh would cp THROUGH a symlink into the source tree")
    assert '[ ! -L "$dir/$rel" ] && cmp -s' in sync, (
        "sync_skills.sh compares through the link, so a symlink reads as current")
    upd = _update_script()
    assert 'if [ -L "$dst" ]; then' in upd, (
        "update_skill.sh no longer detects a symlinked destination")
    assert 'REFRESHED (replaced a symlink with a real copy)' in upd


def test_refresh_is_reported_distinctly_from_already_current():
    """The defect was invisible because sync printed only success. A refresh and a
    no-op must read differently, or the next silent drift is equally undetectable."""
    text = _script()
    assert "REFRESHED (was stale or absent)" in text
    assert "already current" in text


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


# ── A skill-kind target that already holds a connector is refused ────────────

def test_skill_kind_target_holding_a_connector_is_refused_before_the_kind_fork():
    """fact:1595: an MCP walled dir registered with the two-field `name:path`
    form parses as kind `skill`; the CLI package then lands on top of the
    connector and the connector never refreshes. The tell is vector-skill.py
    in the target. The refusal must sit BEFORE the kind fork (so the skill
    path never runs for it) and name the `:mcp:` remedy."""
    with open(SCRIPT, encoding="utf-8") as fh:
        text = fh.read()
    guard = text.index("[ -f \"$dir/vector-skill.py\" ]")
    fork = text.index("# ── Kind fork.")
    assert guard < fork, "the connector guard must run before the kind fork"
    refusal = text[guard:fork]
    assert "REFUSING" in refusal
    assert ":mcp:" in refusal, "the refusal must name the name:mcp:path remedy"
    assert "continue" in refusal, "the refusal must skip the target, not fall through"
